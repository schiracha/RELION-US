"""
job_runner.py — executes the exact, user-approved command string for a job
popup and streams stdout/stderr back live, plus a separate errors buffer.

Design principle (this is the whole point of the app): the backend NEVER
re-assembles or "fixes up" the command you approved in the popup. Whatever
string was in the editable command box when you clicked Run is exactly what
gets executed — via the shell, since real RELION commands legitimately
contain shell constructs (e.g. `` `which relion_run_motioncorr_mpi` ``, see
job_registry.py). If that's wrong, it's wrong the way you wrote it, not
because something was silently inserted or duplicated under the hood.

Custom (non-RELION) jobs — the IMOD/Warp-M/DeepETPicker import bridges —
don't spawn a subprocess at all; they call directly into backend/converters/
in a worker thread, and their "live output" is progress text this module
formats from the converter's return value/exception. They share the same
JobRun/streaming interface so the frontend popup code doesn't need to know
the difference.

Command Center (job history) support: this module also owns job
numbering (RELION's own convention — one global, monotonically increasing
counter per project, the same as `rlnPipeLineJobCounter`; see
job_catalog.JOB_DIRNAME for where the per-type directory prefix comes
from), and the job-lifecycle actions the history table/timeline popup
offers: Abort, Alias (rename), Note, Mark as finished/failed, Delete, and
listing/deleting individual output files (used by the Clean / Harsh Clean
flow — see that flow's own docstring below on cleanup_candidates() for why
this app doesn't try to mechanically replicate RELION's own per-job-type
`PipeLine::cleanupJob()` glob-pattern dispatch).
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import job_catalog
import pipeline_bridge
import project_manager
import slurm_bridge

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_ABORTED = "aborted"
# Submitted to SLURM but not yet running there (sbatch succeeded, job is
# sitting in the scheduler's queue) -- see _run_slurm_job. Distinct from
# STATUS_PENDING (the short local window between start_subprocess_job()
# creating the run and its own task actually beginning) since "queued in
# SLURM" can legitimately last hours, not milliseconds.
STATUS_QUEUED = "queued"

# How often _run_slurm_job polls squeue/sacct for a status change. Not
# configurable via Settings (yet) -- a module-level constant so tests can
# monkeypatch it down for a fast poll loop rather than actually sleeping.
SLURM_POLL_INTERVAL_S = 15

# Manual status overrides a user can apply via "Mark as finished"/"Mark as
# failed" (real RELION job actions, see gui_mainwindow.cpp's
# cb_mark_as_finished/cb_mark_as_failed) -- deliberately NOT the full status
# set: you can't manually force a job back to "running"/"pending", and
# "aborted" has its own dedicated action (abort_run) since it also has to
# actually stop the process. resume_run() is the one deliberate, narrower
# exception to "can't go back to running" -- restricted to the picking job
# types (Manualpick/TomoManualPick), which have no real process to have
# stopped in the first place; see its own docstring.
MANUALLY_SETTABLE_STATUSES = {STATUS_COMPLETED, STATUS_FAILED}
# Which statuses resume_run() will move back to "running" -- see its own
# docstring (the toolbar's "Continue" action).
RESUMABLE_STATUSES = {STATUS_COMPLETED, STATUS_FAILED}

# Placeholder "command" recorded for custom (in-process converter) jobs, which
# never spawn a subprocess. Kept as one constant so the marker and the check
# that recognises it can't drift apart.
IN_PROCESS_COMMAND_PREFIX = "<in-process: "

# Extensions this module will look for when best-effort-detecting a job's
# *inputs* (see _detect_inputs) for the Command Center timeline view's
# "connects jobs to their inputs" box, and when listing/downloading a job's
# *outputs* (list_output_files). Deliberately broad rather than clever --
# these are the real RELION/tomography-pipeline STAR and volume/coordinate
# file extensions this app already works with elsewhere (see
# converters/star_io.py, imod_bridge.py).
_PATH_TOKEN_RE = re.compile(
    r"[\w][\w\-./]*\.(?:star|mrc|mrcs|tomostar|mdoc|xf|tlt|mod)\b"
)

# \r\n listed before the bare \r/\n alternatives so a genuine CRLF pair
# (matched leftmost-first by re) consumes both bytes as ONE separator --
# splitting on \r and \n independently instead treats every \r\n pair as
# two separators, inserting a spurious empty line between them that a real
# terminal would never render (job_runner._pump's own reason for treating
# \r as a separator at all is to survive RELION's \r-only progress-bar
# animation without dropping output, not to fragment ordinary CRLF text).
_LINE_SEP_RE = re.compile(rb"\r\n|\r|\n")


def _detect_inputs(text: str, project_dir: Path, own_cwd: Path, limit: int = 8) -> list[str]:
    """Best-effort, NOT ground truth: scans `text` (a run's command string,
    or its field_values joined together for custom jobs) for path-like
    tokens with known STAR/volume/coordinate extensions that already exist
    on disk and aren't inside this job's own (freshly created, empty at
    start time) output directory -- i.e. things that look like they were
    fed INTO this job rather than produced BY it.

    This is deliberately NOT the same thing as RELION's own real
    input/output pipeline graph (`pipeline_nodes`/`pipeline_input_edges` in
    default_pipeline.star), which RELION-US doesn't build (see
    project_manager.py's module docstring: this app never writes
    default_pipeline.star itself). It's a display convenience for the
    Command Center timeline view, presented as "detected inputs," not as
    verified lineage -- a command that mentions a path substring which
    happens to match an existing file, without that file actually being
    read as input (e.g. it's just part of a comment or an unrelated flag
    value), would show up here too. Good enough to make the timeline view
    "connect jobs to their inputs" the way the user asked, honest about
    being best-effort.
    """
    own_cwd_resolved = own_cwd.resolve()
    seen: list[str] = []
    for match in _PATH_TOKEN_RE.finditer(text):
        token = match.group(0)
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = project_dir / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        if resolved == own_cwd_resolved or own_cwd_resolved in resolved.parents:
            continue  # inside this job's own output dir -> an output, not an input
        try:
            display = str(resolved.relative_to(project_dir.resolve()))
        except ValueError:
            display = str(resolved)
        if display not in seen:
            seen.append(display)
        if len(seen) >= limit:
            break
    return seen


def _extract_output_subdir(command: str) -> Optional[str]:
    """Best-effort extraction of the value following RELION's `--o` (most
    programs) or `--output-directory` (the Python tomography tools) flag
    from a raw command string -- used only to validate an Overwrite's
    command against the directory it's actually supposed to reuse (see
    _output_subdir_matches). Not a full shell parser: uses shlex.split for
    the same whitespace/quoting handling RELION commands actually need
    (verified against a real command containing a backtick command
    substitution -- shlex treats it as an inert token, which is fine here,
    since finding "--o" and the token after it doesn't require resolving
    it). Returns None for anything shlex can't tokenize, or if no output
    flag is present -- callers treat that as "can't verify," not "invalid."
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for i, tok in enumerate(tokens):
        if tok in ("--o", "--output-directory") and i + 1 < len(tokens):
            return tokens[i + 1].rstrip("/")
    return None


def _output_subdir_matches(command: str, authoritative_subdir: str) -> bool:
    """Whether `command`'s own --o/--output-directory argument is this job's
    real directory (authoritative_subdir, e.g. "Refine3D/job029") or a path
    inside it (RELION's own convention is "<JobDir>/jobNNN/run", the "run"
    being the output filename PREFIX for that job, not another directory
    level). Used to block an Overwrite whose (freely user-editable) command
    text was pointed at some OTHER job's directory -- see
    start_subprocess_job's overwrite branch for what a mismatch would
    actually do if allowed to run (RELION-US's own tracking, and the
    --pipeline_control exit markers if sync is on, would point at
    authoritative_subdir while the real output lands wherever the command
    says, silently). Returns True (don't block) when extraction found
    nothing to check -- this is a safety net against a specific, confirmed
    failure mode, not a command interpreter this app owns."""
    actual = _extract_output_subdir(command)
    if actual is None:
        return True
    auth = authoritative_subdir.rstrip("/")
    return actual == auth or actual.startswith(auth + "/")


def _rewrite_output_subdir(command: str, old_subdir: str, new_subdir: str) -> tuple[str, bool]:
    """Replace the output directory token in `command` when the prospective
    job number shown in the draft (old_subdir, e.g. "Import/job005") no
    longer matches the number actually allocated at Run time (new_subdir,
    e.g. "Import/job006"). Handles both the trailing-slash form the draft
    uses (`Import/job005/`) and the bare form. Returns (command, changed).
    No-op when old_subdir is empty or already equals new_subdir."""
    if not old_subdir or old_subdir == new_subdir:
        return command, False
    changed = False
    # Longest first so "Import/job005/" is handled before "Import/job005".
    for old, new in ((old_subdir + "/", new_subdir + "/"), (old_subdir, new_subdir)):
        if old in command:
            command = command.replace(old, new)
            changed = True
    return command, changed


@dataclass
class JobRun:
    run_id: str
    internal_name: str
    display_name: str
    command: str
    cwd: str
    project_dir: str
    job_number: int
    status: str = STATUS_PENDING
    alias: str = ""
    note: str = ""
    field_values: Optional[dict] = None
    detected_inputs: list[str] = field(default_factory=list)
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    # The OS process group leader's PID (== proc.pid, since start_new_session
    # =True makes it its own session/group leader -- see _run_subprocess).
    # Unlike proc/task below, this IS persisted (to_summary() includes it):
    # a backend restart loses the live handle but not the fact that some PID
    # was launched, and abort_run()'s fallback for an orphaned "running" run
    # needs it to actually signal the real process rather than just
    # reconciling a status nothing has verified. See _pid_matches_persisted_
    # run for why this alone isn't treated as sufficient without a plausible
    # -match check first (PIDs get reused by the OS).
    pid: Optional[int] = None
    # Set once _run_slurm_job's sbatch submission succeeds -- persisted
    # (to_summary() includes it) the same way pid is, so it survives a
    # backend restart: abort_run/poll logic key off THIS, not pid (a SLURM
    # job has no local pid at all -- see the module docstring's note on
    # start_subprocess_job's slurm_options param).
    slurm_job_id: Optional[str] = None
    # Last raw SLURM state string seen by the poll loop (e.g. "RUNNING",
    # "COMPLETED") -- diagnostic only, not used for any status-mapping
    # decision (that's slurm_bridge.SLURM_STATE_TO_STATUS, applied fresh
    # each poll); surfaced in to_summary() for the Command Center to show.
    slurm_state: Optional[str] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)
    # Runtime-only handles for abort_run(); never serialized (to_summary()
    # builds an explicit dict below and doesn't include them). A
    # persisted-history run loaded back after a restart simply has these as
    # None -- the live asyncio handles are always gone, but the process
    # itself (see `pid` above) may well not be.
    proc: Any = field(default=None, repr=False, compare=False)
    task: Any = field(default=None, repr=False, compare=False)
    # One-off note emitted into the live output at start (currently: the
    # output directory was renumbered because the prospective jobNNN was
    # taken). Declared here rather than set ad hoc so both start paths and
    # the reader agree it exists.
    rewrite_note: Optional[str] = field(default=None, repr=False, compare=False)
    # Set when RELION pipeline sync was on but registering this job with
    # relion_pipeliner failed (pipeline locked, job type not recognised,
    # etc.) -- surfaced into the Errors tab at the start of
    # _run_subprocess, same as rewrite_note is surfaced into stdout. The
    # job still runs under RELION-US's own numbering either way (see
    # start_subprocess_job); this only means it won't show up in RELION's
    # own GUI, which is worth knowing rather than discovering later.
    pipeline_sync_error: Optional[str] = field(default=None, repr=False, compare=False)
    # True once this run was actually registered with relion_pipeliner
    # (register_job succeeded) -- as opposed to pipeline_sync_enabled()
    # merely being on for the project, which register_job can still fail
    # under (locked pipeline, unrecognised job type). Everything that
    # writes directly into RELION's own pipeline afterward (marking
    # Running when work starts, the exit-marker + --check_job_completion
    # handshake when it ends -- see pipeline_bridge.set_process_status's
    # own docstring for why that direct write exists at all) is gated on
    # this, not on pipeline_sync_enabled() again: re-checking the project
    # setting would try to talk to a process this app never actually told
    # relion_pipeliner about. Persisted (to_summary()) so a "Done" click on
    # a run from a previous backend session still knows whether to do the
    # handshake.
    pipeline_registered: bool = False
    # Set when Abort arrives before the subprocess handle exists (see
    # abort_run + _run_subprocess); the launcher honours it as soon as it
    # has a process to signal.
    abort_requested: bool = field(default=False, repr=False, compare=False)

    @property
    def is_custom_job(self) -> bool:
        """True for an in-process converter job (no subprocess to signal);
        see custom_jobs.py and start_custom_job()."""
        return self.command.startswith(IN_PROCESS_COMMAND_PREFIX)

    @property
    def job_name(self) -> str:
        """What the Command Center shows/sorts by: the user's alias if
        they've renamed this job, otherwise RELION's own zero-padded job
        number convention (job001, job002, ...) -- which is why sorting by
        name is "effectively numerical order unless the user has renamed
        some jobs," exactly as asked for."""
        return self.alias.strip() or f"job{self.job_number:03d}"

    def to_summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "internal_name": self.internal_name,
            "display_name": self.display_name,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "pid": self.pid,
            "slurm_job_id": self.slurm_job_id,
            "slurm_state": self.slurm_state,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "project_dir": self.project_dir,
            "cwd": self.cwd,
            "job_number": self.job_number,
            "alias": self.alias,
            "note": self.note,
            "job_name": self.job_name,
            "field_values": self.field_values,
            "detected_inputs": self.detected_inputs,
            "abortable": self.status in (STATUS_RUNNING, STATUS_QUEUED) and (
                self.proc is not None or self.task is not None or self.slurm_job_id is not None
            ),
            "pipeline_registered": self.pipeline_registered,
        }

    async def broadcast(self, message: dict) -> None:
        for q in list(self.subscribers):
            await q.put(message)


class JobRunManager:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.runs: dict[str, JobRun] = {}

    def new_run_id(self) -> str:
        return uuid.uuid4().hex[:12]

    RELION_RUN_PREFIX = "relion:"

    @classmethod
    def is_relion_run(cls, run_id: str) -> bool:
        """A Command Center row imported from RELION's own pipeline rather
        than started here. This app does not own those jobs, so abort /
        delete / status edits are refused outright on them (see main.py's
        _reject_relion_run), and Overwrite is refused unless pipeline sync
        is on (see start_run). alias/note are never refused -- they're
        kept as a purely local overlay for these (see set_alias/set_note,
        project_manager.set_relion_overlay), never written into RELION's
        own files, so there's nothing for them to leave inconsistent."""
        return str(run_id).startswith(cls.RELION_RUN_PREFIX)

    def relion_run_detail(self, run_id: str, project_dir: Optional[Path] = None) -> Optional[dict]:
        """One imported RELION job, with the option values it actually ran
        with and (see project_manager.read_relion_last_command) the exact
        command that produced its current output.

        RELION saves every JobOption into `job.star` in the job's own
        directory when the job runs -- the same file its GUI reads to reopen a
        job -- so reopening one here shows its real settings rather than the
        job type's defaults. Jobs from RELION 3.0 or earlier (a `run.job` in a
        different format) and directories that have since been deleted come
        back with empty values and a note saying so, which is the honest
        answer. Both reads are scoped to this per-job detail lookup, not the
        bulk _relion_pipeline_entries list every Command Center refresh
        polls -- reading and regex-scanning every job's job.star/note.txt on
        every poll would be real, needless I/O for a project with many jobs.
        """
        pd = project_dir if project_dir is not None else self.project_dir
        for entry in self._relion_pipeline_entries(pd):
            if entry["run_id"] != run_id:
                continue
            entry = dict(entry)
            job_dir = Path(entry["cwd"])
            values = project_manager.read_relion_job_options(job_dir)
            entry["field_values"] = values
            entry["command"] = project_manager.read_relion_last_command(job_dir)
            if not entry["exists_on_disk"]:
                entry["import_note"] = (
                    "This job is listed in RELION's pipeline but its directory "
                    "is no longer on disk."
                )
            elif not values:
                entry["import_note"] = (
                    "No job.star in this job's directory, so its settings could "
                    "not be read (RELION 3.0 and earlier wrote a run.job "
                    "instead). The form shows this job type's defaults."
                )
            return entry
        return None

    def get(self, run_id: str) -> Optional[JobRun]:
        return self.runs.get(run_id)

    def set_project_dir(self, project_dir: Path) -> None:
        """Switch the active project directory. In-flight runs already have
        their own `cwd`/`project_dir` baked in (see start_subprocess_job)
        and keep running/streaming exactly as before — they just stop
        showing up in list_runs() for the *new* project, the same way a
        job you started in project A doesn't disappear when you point the
        GUI at project B, it just isn't "in" B."""
        self.project_dir = project_dir

    def list_runs(self, project_dir: Optional[Path] = None) -> list[dict]:
        """Runs for one project: persisted history (past sessions, summary
        only) merged with any still-tracked in-memory runs (current session,
        which may have moved past what was last persisted, e.g. still
        running). In-memory wins on conflict since it's more current."""
        target = str(project_dir if project_dir is not None else self.project_dir)
        own_entries: dict[str, dict] = {}
        own_job_numbers: set[int] = set()
        for entry in project_manager.load_history(Path(target)):
            run_id = entry.get("run_id")
            if run_id:
                own_entries[run_id] = entry
                if entry.get("job_number"):
                    own_job_numbers.add(entry["job_number"])
        for run in self.runs.values():
            if run.project_dir == target:
                own_entries[run.run_id] = run.to_summary()
                if run.job_number:
                    own_job_numbers.add(run.job_number)
        # Jobs RELION itself ran, from its own default_pipeline.star -- but
        # ONLY for job numbers this app has no record of. Once two-way sync
        # registers a job here with RELION's pipeline (see
        # _register_in_relion_pipeline), that job's number shows up in BOTH
        # sources for the SAME underlying job, not two different jobs: a
        # run_id-keyed merge (the previous approach) never notices the
        # collision, since this app's own uuid run_id and the synthetic
        # "relion:jobNNN" placeholder are different strings -- so every
        # synced job silently doubled in the Command Center (confirmed: a
        # 10-job synced project produced 20 rows, one uninformative
        # "source: relion" placeholder per real job, right next to this
        # app's own richer entry for the identical job). Skipping the
        # placeholder whenever this app already has a record for that job
        # number keeps exactly one row per job while still surfacing jobs
        # genuinely run outside this app entirely (a legacy project, or a
        # job launched from RELION's own GUI) that only exist in
        # default_pipeline.star.
        # Job numbers RELION-US itself deleted (Delete on a pipeline-synced
        # job) while relion_pipeliner has no CLI verb to prune the matching
        # process out of default_pipeline.star -- see project_manager.
        # load_relion_deleted_job_numbers's own docstring for the full
        # reasoning and why this is safe against job-number reuse.
        hidden_job_numbers = project_manager.load_relion_deleted_job_numbers(Path(target))
        merged: dict[str, dict] = {}
        for entry in self._relion_pipeline_entries(Path(target)):
            job_number = entry.get("job_number")
            if job_number in own_job_numbers or job_number in hidden_job_numbers:
                continue
            merged[entry["run_id"]] = entry
        merged.update(own_entries)
        # Job number first, timestamp as a tie-break. Jobs imported from
        # RELION's pipeline carry no timestamp, and a project's job counter
        # only ever goes up -- for RELION's jobs and this app's alike -- so the
        # number is the one chronological key that works across both.
        runs = sorted(
            merged.values(),
            key=lambda r: (r.get("job_number") or 0, r.get("started_at") or 0),
        )
        self._attach_input_lineage(runs, Path(target))
        return runs

    @staticmethod
    def _relion_pipeline_entries(project_dir: Path) -> list[dict]:
        """Jobs from RELION's own `default_pipeline.star`, as Command Center
        rows.

        A project built in RELION's GUI has its whole history there and none of
        it in this app's own file, so without this the Command Center is empty
        in exactly the project where it would be most useful.

        These are read-only for anything that would need this app to keep
        RELION's own pipeline record consistent with a change it made:
        `source: "relion"` marks them, and the API refuses abort/delete on
        them outright, and Overwrite unless pipeline sync is on (see
        is_relion_run's own docstring for the full breakdown). alias/note
        are the exception -- a purely local overlay (see set_alias/
        set_note, merged in below), never written into RELION's own files
        either way. Reopening one still works -- the options come from the
        job's own `job.star` (see get_run_detail).

        Timestamps: RELION's own pipeline file records none. Best-effort
        estimates come from project_manager.estimate_job_timestamps
        (specific marker files' mtimes, not the directory's own, which
        changes on any touch) and are always marked `timestamp_estimated`
        so the UI can show them as approximate rather than fact -- see that
        function's docstring for exactly how unreliable "approximate" can
        be when a project's files were copied after the jobs actually ran.
        """
        info = project_manager.read_relion_pipeline(project_dir)
        # Local-only alias/note edits made from here (see project_manager.
        # set_relion_overlay's own module comment for why they're kept
        # local rather than written into RELION's own files) -- applied
        # per-entry below, only for the two fields an overlay actually has.
        overlays = project_manager.load_relion_overlays(project_dir)
        out: list[dict] = []
        by_process_name: dict[str, dict] = {}
        for proc in info["processes"]:
            name = proc["name"]                        # e.g. "Class2D/job005"
            job_dir = project_dir / name
            # relion.motioncorr/relion.ctffind are shared between a SPA and
            # a Tomo menu entry (job_catalog.TOMO_VARIANT_OF) -- the type
            # label alone can't say which, so this reads the job's own
            # job.star ONLY for those two labels (AMBIGUOUS_TOMO_LABELS),
            # not for every job in the project, to keep this per-poll list
            # from doing per-job file I/O the way it deliberately avoids
            # everywhere else (see this method's own docstring).
            is_tomo_hint = (
                project_manager.read_relion_job_is_tomo(job_dir)
                if proc["type_label"] in job_catalog.AMBIGUOUS_TOMO_LABELS else False
            )
            internal = job_catalog.internal_name_for_label(proc["type_label"], is_tomo=is_tomo_hint)
            display = job_catalog.JOB_CATALOG.get(internal, (None, proc["type_label"]))[1] \
                if internal else proc["type_label"]
            try:
                exists = job_dir.exists()
            except OSError:
                exists = False
            # The run_id must survive a URL path segment, so it cannot carry
            # the job's directory name -- an encoded "/" in a path is rejected
            # before the route ever matches. RELION's job number is unique
            # across the project (one counter for every job type), which makes
            # it the natural identifier.
            slug = (f"job{proc['job_number']:03d}" if proc["job_number"]
                    else name.replace("/", "-"))
            status = project_manager.RELION_STATUS_MAP.get(proc["status_label"], "completed")
            started_at = ended_at = None
            timestamp_estimated = False
            if exists:
                started_at, ended_at = project_manager.estimate_job_timestamps(job_dir, status)
                timestamp_estimated = started_at is not None or ended_at is not None
            entry = {
                "run_id": f"relion:{slug}",
                "source": "relion",
                "internal_name": internal or "",
                "display_name": display,
                "command": "",
                "status": status,
                "exit_code": None,
                "started_at": started_at,
                "ended_at": ended_at,
                "timestamp_estimated": timestamp_estimated,
                "project_dir": str(project_dir),
                "cwd": str(job_dir),
                "job_number": proc["job_number"],
                "alias": proc["alias"],
                "note": "",
                # This app's own runs already compute job_name as "alias or
                # job{number}" (see JobRun.job_name) -- the frontend only
                # ever reads job_name, never .alias directly, so a
                # RELION-native job's REAL alias (set in RELION's own GUI,
                # proc["alias"]) needs to land here too, not just an
                # overlay set from here, or it would never actually be
                # shown despite genuinely being there.
                "job_name": proc["alias"] or name.split("/")[-1],
                "field_values": {},
                "detected_inputs": [],
                "abortable": False,
                "relion_type_label": proc["type_label"],
                "exists_on_disk": exists,
            }
            overlay = overlays.get(entry["run_id"])
            if overlay:
                if "alias" in overlay:
                    # An empty override means "explicitly cleared from
                    # here" (see set_alias's own docstring: reverts to the
                    # plain job number) -- falls back to the bare directory
                    # name, NOT proc["alias"], so clearing actually clears
                    # rather than just re-exposing RELION's own real alias
                    # underneath it.
                    entry["alias"] = overlay["alias"]
                    entry["job_name"] = overlay["alias"] or name.split("/")[-1]
                if "note" in overlay:
                    entry["note"] = overlay["note"]
            out.append(entry)
            by_process_name[name] = entry

        # RELION's own computed graph (see read_relion_pipeline's
        # "producers"), wired up as the same input_links shape
        # _attach_input_lineage produces for this app's own runs -- so the
        # Command Center's lineage chips and network view work identically
        # for a job RELION ran and a job run here, even though this side
        # never ran _detect_inputs() on RELION's own jobs.
        for name, entry in by_process_name.items():
            links = []
            for producer_name in info.get("producers", {}).get(name, []):
                producer_entry = by_process_name.get(producer_name)
                if producer_entry is not None:
                    links.append({
                        "path": producer_name,
                        "run_id": producer_entry["run_id"],
                        "job_name": producer_entry["job_name"],
                    })
            if links:
                entry["input_links"] = links
        return out

    @staticmethod
    def _attach_input_lineage(runs: list[dict], project_dir: Path) -> None:
        """Best-effort: for each run, map its detected_inputs (project-root-
        relative file paths) to the earlier job whose output directory
        contains them, so the Command Center timeline can 'connect jobs to
        their inputs'. Adds run['input_links'] = [{path, run_id, job_name}].

        Like _detect_inputs this is a display convenience, not RELION's real
        pipeline graph (which this app doesn't build) -- a file that merely
        lives under a job's output dir is attributed to that job."""
        # Map each run's own output dir (project-relative, e.g. "Import/job001")
        # to its identity.
        by_dir: dict[str, dict] = {}
        pd_resolved = project_dir.resolve()
        for r in runs:
            cwd = r.get("cwd")
            if not cwd:
                continue
            try:
                rel = str(Path(cwd).resolve().relative_to(pd_resolved))
            except (ValueError, OSError):
                continue
            by_dir[rel] = r
        for r in runs:
            links = []
            for inp in r.get("detected_inputs", []) or []:
                # walk up the input path's parents to find a producing job dir
                producer = None
                parent = Path(inp)
                while parent != parent.parent:
                    parent = parent.parent
                    key = str(parent)
                    if key in by_dir and by_dir[key].get("run_id") != r.get("run_id"):
                        producer = by_dir[key]
                        break
                if producer is not None:
                    links.append({
                        "path": inp,
                        "run_id": producer.get("run_id"),
                        "job_name": producer.get("job_name") or f"job{producer.get('job_number', 0):03d}",
                    })
            if links:
                r["input_links"] = links

    # ---- Two-way sync with RELION's own pipeline ------------------------
    #
    # Off unless relion_pipeliner is installed AND the project has opted in
    # (project_manager.pipeline_sync_setting). Both halves matter: without the
    # binary there is no safe way to touch default_pipeline.star, and a project
    # somebody only wants to look at should not gain new entries in RELION's
    # record because a job was run here.

    def pipeline_sync_enabled(self, project_dir: Optional[Path] = None) -> bool:
        pd = Path(project_dir) if project_dir is not None else self.project_dir
        return (project_manager.pipeline_sync_setting(pd)
                and pipeline_bridge.is_available())

    def _register_in_relion_pipeline(
        self, project_dir: Path, internal_name: str, field_values: dict
    ) -> Optional[dict]:
        """Ask RELION to allocate and record this job. None if this job type
        has no real RELION type label to register under (nothing for
        relion_pipeliner to recognise). Raises PipelineBridgeError on
        registration failure -- callers decide how to surface that; see
        start_subprocess_job, which falls back to this app's own numbering
        (a job the user asked to run should still run when the pipeline is
        momentarily locked by an open RELION GUI, or when relion_pipeliner
        errors on a job type it does not recognise) but puts the reason into
        the run's own output rather than swallowing it.

        Checks job_catalog.JOB_CATALOG (real relion_* subprocess jobs) first,
        then job_catalog.CUSTOM_JOBS -- but only a CUSTOM_JOBS entry whose
        label_new is a REAL RELION type label (relion.*) is worth even
        trying: relion_pipeliner would just reject anything else outright
        ("unknown job type label"), and trying anyway would mean a doomed
        --addJobFromStar subprocess call (with its own pipeline-lock wait)
        plus a confusing "could not register" warning on EVERY run of a
        plain custom.* job (the IMOD/Warp/DeepETPicker/AreTomo2 import
        bridges) that was never meant to register at all -- same as before
        this method knew about CUSTOM_JOBS. Manualpick/TomoManualPick (the
        in-browser picking jobs) are the real-label case this exists for:
        relion.manualpick / relion.picktomo, so they show up correctly in
        RELION's own GUI and their output is a valid input to real
        downstream RELION jobs -- see CUSTOM_JOBS's own docstring in
        job_catalog.py.

        TomoMotioncorr/TomoCtffind (job_catalog.TOMO_VARIANT_OF) register
        under the SAME label as their SPA sibling (Motioncorr/Ctffind --
        real RELION has only the one class for either, is_tomo is a runtime
        flag inside it, not a separate label) -- field_values["is_tomo"] is
        set here from internal_name, the same way job_registry._build_draft_
        command sets it for the command this job actually runs, so
        write_job_star's _rlnJobIsTomo (and the output nodes RELION's own
        pipeliner computes from it) matches what really happened.
        """
        meta = job_catalog.JOB_CATALOG.get(internal_name)
        if meta:
            type_label = meta[0]
        else:
            custom_meta = job_catalog.CUSTOM_JOBS.get(internal_name)
            type_label = custom_meta["label_new"] if custom_meta else ""
            if not type_label.startswith("relion."):
                return None
        try:
            import job_registry

            options_by_key = {
                o["key"]: o for o in job_registry.raw_job(internal_name).get("options", [])
            }
        except Exception:
            options_by_key = {}
        field_values = dict(field_values or {})
        field_values["is_tomo"] = internal_name in job_catalog.TOMO_VARIANT_OF
        return pipeline_bridge.register_job(
            project_dir, type_label, field_values, options_by_key)

    def sync_completion_to_relion(self, project_dir: Optional[Path] = None) -> bool:
        """Let RELION notice that a job finished (it reads the exit files the
        `--pipeline_control` flag makes the program write)."""
        pd = Path(project_dir) if project_dir is not None else self.project_dir
        if not self.pipeline_sync_enabled(pd):
            return False
        return pipeline_bridge.check_job_completion(pd)

    @staticmethod
    def _mark_pipeline_running(run: JobRun) -> None:
        """The direct-write half of the fix for jobs sitting forever as
        "Scheduled" in RELION's own GUI -- see pipeline_bridge.
        set_process_status's own docstring for the full story
        (--check_job_completion only ever promotes a process already marked
        "Running", and relion_pipeliner's CLI has no other way to get one
        there without actually re-executing its real command). Called once,
        right when real work starts, for any run that was actually
        registered (run.pipeline_registered) -- best-effort: a failure here
        (lock contention, pipeline file vanished) must never block the real
        work itself from starting, so it's swallowed, the same as _persist's
        own history-write failures.
        """
        if not run.pipeline_registered:
            return
        try:
            process_name = str(Path(run.cwd).relative_to(run.project_dir))
        except ValueError:
            return
        try:
            pipeline_bridge.set_process_status(Path(run.project_dir), process_name, "Running")
        except pipeline_bridge.PipelineBridgeError:
            pass

    def _next_job_number(self, project_dir: Path, internal_name: Optional[str] = None) -> int:
        """RELION's own job numbering is a single counter for the whole
        project, shared across every job type (see job_catalog.py's
        JOB_DIRNAME docstring) -- derived fresh each time from persisted +
        in-memory state rather than kept as separate mutable counter state,
        so it can't drift out of sync across a backend restart.

        **RELION's own numbering counts too.** `rlnPipeLineJobCounter` and the
        per-process numbers in `default_pipeline.star` are read so opening a
        project built in RELION's own GUI continues its numbering instead of
        colliding with job001 and other numbers it already owns.

        As a final backstop, if the directory the number would produce already
        exists on disk (a job RELION ran but later removed from its pipeline,
        say), keep going until it doesn't. Nothing here ever allocates a
        directory that is already there.
        """
        target = str(project_dir)
        numbers = [
            entry.get("job_number", 0) for entry in project_manager.load_history(project_dir)
        ]
        numbers += [run.job_number for run in self.runs.values() if run.project_dir == target]
        numbers += list(project_manager.relion_job_numbers(project_dir))
        n = (max(numbers) if numbers else 0) + 1

        if internal_name:
            prefix = job_catalog.job_dirname(internal_name)
            # Bounded: a project with 10k consecutive existing job dirs is not
            # a real case, and an unbounded loop on a strange filesystem is
            # worse than a number that is merely high.
            for _ in range(10000):
                if not (project_dir / f"{prefix}/job{n:03d}").exists():
                    break
                n += 1
        return n

    def prospective_subdir(self, internal_name: str, project_dir: Optional[Path] = None) -> str:
        """The project-root-relative output directory the NEXT run of this
        job type would use, e.g. "Import/job005" -- RELION's
        `<JobDir>/jobNNN` convention (job_catalog.job_dirname + the shared
        job counter). Used by the API to build a draft command's
        `--o <subdir>/` exactly as RELION does. Best-effort/prospective: the
        authoritative number is finalized at Run time (start_subprocess_job),
        which rewrites the command's output path if another job was recorded
        in between."""
        pd = project_dir if project_dir is not None else self.project_dir
        n = self._next_job_number(pd, internal_name)
        return f"{job_catalog.job_dirname(internal_name)}/job{n:03d}"

    def _resolve_overwrite_target(
        self, overwrite_run_id: str, project_dir: Path, allow_running: bool = False
    ) -> dict:
        """Looks up the run being overwritten, whether it's still live in
        self.runs (this session), only survives in persisted history (a
        previous session), or -- for the read-only callers, i.e. draft
        recompute; see overwrite_target_subdir -- was never in this app's
        own history at all because RELION itself ran it (a synthetic
        "relion:jobNNN" run_id, resolved via relion_run_detail's own read
        of that job's job.star). Overwrite should work either way, the
        same as delete_run()/file operations above. Raises ValueError
        (caller turns this into a 409) if the run can't be found, is still
        running (unless allow_running), or belongs to a different/inactive
        project.

        allow_running: a picker job (Manualpick/TomoManualPick) sits at
        STATUS_RUNNING for the whole picking session (see start_custom_job's
        stays_running) -- for it, "running" doesn't mean "compute in
        progress," it means "picking session open," and Overwrite pressed
        during that session is exactly the "refresh the job directory"
        action the user asked for. Only start_custom_job passes True here,
        and only for stays_running jobs; start_subprocess_job's real
        compute jobs keep the default (a genuinely-running subprocess must
        never be overwritten out from under itself).

        Resolving a RELION-native run here does NOT reopen the door on
        actually overwriting one: start_subprocess_job's overwrite_run_id
        branch (which calls this too) is only ever reached from /api/runs,
        and that endpoint rejects a RELION-native run_id itself, before
        this method runs at all (see main.py's _reject_relion_run). This
        method has no way to distinguish "just show me a draft" from "I'm
        about to actually run this," so that distinction has to live with
        the caller that knows which one it's doing -- it does.
        """
        run = self.get(overwrite_run_id)
        if run is not None:
            info = {
                "status": run.status, "project_dir": run.project_dir, "cwd": run.cwd,
                "job_number": run.job_number, "alias": run.alias, "note": run.note,
            }
        else:
            entry = next(
                (h for h in project_manager.load_history(project_dir) if h.get("run_id") == overwrite_run_id),
                None,
            )
            if entry is None and self.is_relion_run(overwrite_run_id):
                entry = self.relion_run_detail(overwrite_run_id, project_dir)
            if entry is None:
                raise ValueError(f"Unknown run_id to overwrite: {overwrite_run_id}")
            info = {
                "status": entry.get("status"), "project_dir": entry.get("project_dir"),
                "cwd": entry.get("cwd"), "job_number": entry.get("job_number", 0),
                "alias": entry.get("alias", ""), "note": entry.get("note", ""),
            }
        if info["status"] in (STATUS_RUNNING, STATUS_QUEUED) and not allow_running:
            raise ValueError("Cannot overwrite a job that is still running")
        if info["project_dir"] != str(project_dir):
            raise ValueError("Cannot overwrite a job from a different/inactive project")
        if not info["cwd"]:
            raise ValueError(f"Run {overwrite_run_id} has no recorded output directory to overwrite")
        return info

    def overwrite_target_subdir(self, overwrite_run_id: str, project_dir: Optional[Path] = None) -> str:
        """The project-root-relative output directory (e.g. "Import/job005")
        of the run `overwrite_run_id` would overwrite -- what a draft
        recompute should target while the user is editing an existing
        (e.g. failed) job's fields, instead of prospective_subdir's fresh
        "next unused number," which used to make Recompute silently drift
        the command's `--o` onto a NEW job directory and turn "Overwrite"
        into "create job006 next to the job005 I meant to fix." Raises
        ValueError (same cases as _resolve_overwrite_target) if the run
        can't be found or its cwd isn't inside this project."""
        pd = project_dir if project_dir is not None else self.project_dir
        target = self._resolve_overwrite_target(overwrite_run_id, pd)
        cwd = Path(target["cwd"])
        try:
            return str(cwd.relative_to(pd))
        except ValueError:
            raise ValueError(
                f"Run {overwrite_run_id}'s directory ({cwd}) isn't inside this project ({pd})"
            )

    def _persist(self, run: JobRun) -> None:
        """Best-effort: append/update this run's summary in its project's
        on-disk history. A history-file write failure should never take
        down a job run, so failures here are swallowed."""
        try:
            project_dir = Path(run.project_dir)
            history = [h for h in project_manager.load_history(project_dir) if h.get("run_id") != run.run_id]
            history.append(run.to_summary())
            project_manager.save_history(project_dir, history)
        except OSError:
            pass

    async def start_subprocess_job(
        self,
        internal_name: str,
        display_name: str,
        command: str,
        subdir: Optional[str] = None,
        field_values: Optional[dict] = None,
        overwrite_run_id: Optional[str] = None,
        slurm_options: Optional[dict] = None,
    ) -> JobRun:
        """
        Launch `command` (the exact, user-edited string) via the shell,
        FROM THE PROJECT ROOT — exactly like RELION, so project-root-relative
        input patterns and the command's `--o <JobDir>/jobNNN/` output path
        resolve the same way RELION's own GUI resolves them. The job's output
        directory is always RELION's `<JOB_DIRNAME>/job<NNN>` convention (see
        job_catalog.job_dirname() + the shared job counter); `run.cwd` is set
        to that directory (used by the Outputs tab / clean / delete /
        download), even though the process itself runs in the project root.

        `subdir` is the output path the draft already embedded in the
        command's `--o` (the prospective jobNNN shown when the popup opened).
        It is NOT used to choose the directory — the authoritative jobNNN is
        allocated here from the shared counter. It's only used to detect a
        stale prospective number (another job got recorded in between) and
        rewrite the command's output path to match the directory actually
        created, so command and directory never disagree.

        overwrite_run_id: RELION's real "Overwrite" job action re-runs a
        job into its OWN prior output directory under the SAME job slot
        (same job number, same run_id, same alias/note) rather than
        allocating a new one -- see gui_mainwindow.cpp's
        cb_toggle_overwrite_continue, which literally reuses the pipeline's
        existing job-counter entry rather than adding a new one. Mirrored
        here the same way: the Command Center keeps showing ONE row for
        this job (not a confusing second row for the same directory) --
        its history just resets and starts again. Raises ValueError if
        that run can't be found, is still running, or belongs to a
        different project than the one currently active.

        slurm_options: when given (shape: {"account", "partition",
        "time_limit", "mem"}, all optional strings), this run is submitted
        to SLURM (_run_slurm_job) instead of launched as a local subprocess
        (_run_subprocess) -- everything ABOVE this docstring's own concerns
        (job numbering, Overwrite, RELION pipeline registration) is
        identical either way; only the actual execution mechanism differs,
        at the single branch point where `run.task` is created below. See
        _run_slurm_job's own docstring for why local-subprocess abort/
        status-polling logic (deeply PID/process-group-based) isn't reused
        for this path at all.
        """
        project_dir = self.project_dir
        rewrite_note = None
        pipeline_sync_error = None
        pipeline_registered = False

        if overwrite_run_id is not None:
            target = self._resolve_overwrite_target(overwrite_run_id, project_dir)
            run_id = overwrite_run_id
            cwd = Path(target["cwd"])
            job_number = target["job_number"]
            alias, note = target["alias"], target["note"]
            # Overwrite reuses the SAME slot RELION already knows about (see
            # the docstring above) -- unlike a fresh run, there is no new
            # number to allocate, so _register_in_relion_pipeline() (which
            # always calls --addJobFromStar and gets back a NEW number) is
            # deliberately NOT called here; doing so would create a second,
            # mismatched pipeline entry instead of reusing this job's own.
            #
            # Two things this branch used to skip entirely, both fixed here:
            #   1. The command's `--o` path was trusted verbatim from
            #      whatever was passed in, with no check against the actual
            #      `cwd` this run uses -- unlike the fresh-run branch below,
            #      which always re-verifies. Apply the same defensive
            #      rewrite so a stale/mismatched path in the (user-editable)
            #      command box can't send output somewhere `cwd` doesn't
            #      point, which is consistent with a user having needed to
            #      type an absolute path to work around a relative one that
            #      didn't resolve as expected.
            #   2. `--pipeline_control` was never appended, even when sync
            #      is on -- this is what makes a relion_ program write the
            #      exit-status files `--check_job_completion` reads (see
            #      pipeline_bridge.pipeline_control_args), and its absence
            #      is why an overwritten job could sit stuck in RELION's own
            #      GUI. This only takes effect for a job RELION's pipeline
            #      already knows about, matching RELION's real "Overwrite"
            #      semantics (gui_mainwindow.cpp's cb_toggle_overwrite_
            #      continue reuses the existing pipeline entry, it never
            #      adds a new one).
            try:
                authoritative_subdir = str(cwd.relative_to(project_dir))
            except ValueError:
                authoritative_subdir = None
            if authoritative_subdir:
                proposed = (subdir or "").rstrip("/")
                command, rewritten = _rewrite_output_subdir(command, proposed, authoritative_subdir)
                if rewritten:
                    rewrite_note = (
                        f"[RELION-US] Output directory in the command didn't match "
                        f"this job's actual directory ({authoritative_subdir}/); "
                        f"command's output path updated to match."
                    )
                # The rewrite above only catches drift between the PROSPECTIVE
                # path the popup opened with and this job's real directory --
                # both are pinned to authoritative_subdir for an Overwrite, so
                # it's a no-op here. It does NOT catch a user manually
                # retyping --o in the (freely editable) command box to point
                # somewhere else entirely, which is a real, confirmed failure
                # mode: RELION-US's own tracking and --pipeline_control's exit
                # markers would stay pinned to authoritative_subdir while the
                # actual command output lands wherever the edited --o says,
                # silently. Block it outright rather than warn -- there is no
                # legitimate reason an Overwrite's command should target a
                # different directory than the job it's overwriting; Clone
                # (a fresh, newly-numbered job) is the right tool for that.
                if not _output_subdir_matches(command, authoritative_subdir):
                    raise ValueError(
                        f"This command's output directory doesn't match the job "
                        f"being overwritten ({authoritative_subdir}/) -- Overwrite "
                        f"must reuse the SAME directory, or this app's tracking "
                        f"would point at one directory while the command actually "
                        f"writes to another. Use Clone to run this as a new job "
                        f"instead."
                    )
                if self.pipeline_sync_enabled(project_dir):
                    command = pipeline_bridge.pipeline_control_args(command, authoritative_subdir)
                    pipeline_registered = True
        else:
            run_id = self.new_run_id()
            registered = None
            if self.pipeline_sync_enabled(project_dir):
                # Two-way mode: RELION's own pipeliner allocates the job number,
                # creates the directory and records the process (with its node
                # graph) in default_pipeline.star. Whatever slot it gives back is
                # authoritative from here on -- guessing our own number while the
                # pipeline is also allocating them is how the two records drift.
                try:
                    registered = self._register_in_relion_pipeline(
                        project_dir, internal_name, field_values or {})
                except pipeline_bridge.PipelineBridgeError as exc:
                    pipeline_sync_error = str(exc)
            if registered:
                job_number = registered["job_number"]
                authoritative_subdir = registered["process_name"]
            else:
                job_number = self._next_job_number(project_dir, internal_name)
                authoritative_subdir = f"{job_catalog.job_dirname(internal_name)}/job{job_number:03d}"
            # `subdir` is the output path the draft embedded in the command's
            # `--o`/`--output-directory` (the prospective number shown when the
            # popup opened). If another job got recorded between then and now,
            # the authoritative number differs -- rewrite the command's output
            # path to match the directory we actually create, so the command
            # and the tracked/created directory never disagree. This is the
            # one transparent adjustment (logged to the run's output).
            proposed = (subdir or "").rstrip("/")
            command, rewritten = _rewrite_output_subdir(command, proposed, authoritative_subdir)
            cwd = project_dir / authoritative_subdir
            alias, note = "", ""
            if rewritten:
                rewrite_note = (
                    f"[RELION-US] Output directory advanced to {authoritative_subdir}/ "
                    f"({proposed} was already taken); command's output path updated to match."
                )
            if registered:
                # RELION's own completion mechanism: the program writes an exit
                # file into this directory, which `relion_pipeliner
                # --check_job_completion` reads to move the process out of
                # "Running". Without the flag the job would sit as Running in
                # RELION's GUI forever.
                command = pipeline_bridge.pipeline_control_args(command, authoritative_subdir)
                pipeline_registered = True
                pipeline_note = (
                    f"[RELION-US] Registered in RELION's pipeline as "
                    f"{authoritative_subdir}/ — it will appear in RELION's own GUI."
                )
                rewrite_note = (rewrite_note + "\n" + pipeline_note) if rewrite_note else pipeline_note
            elif pipeline_sync_error:
                pipeline_sync_error = (
                    f"[RELION-US] Could not register this job in RELION's own "
                    f"pipeline ({pipeline_sync_error}). It still ran under "
                    f"RELION-US's own numbering, but won't show up in RELION's "
                    f"own GUI."
                )
        cwd.mkdir(parents=True, exist_ok=True)

        detect_text = command + " " + " ".join(str(v) for v in (field_values or {}).values())
        run = JobRun(
            run_id=run_id,
            internal_name=internal_name,
            display_name=display_name,
            command=command,
            cwd=str(cwd),
            project_dir=str(project_dir),
            job_number=job_number,
            alias=alias,
            note=note,
            field_values=field_values,
            detected_inputs=_detect_inputs(detect_text, project_dir, cwd),
            pipeline_registered=pipeline_registered,
        )
        run.rewrite_note = rewrite_note
        run.pipeline_sync_error = pipeline_sync_error
        self.runs[run_id] = run
        self._persist(run)
        if slurm_options is not None:
            run.task = asyncio.create_task(self._run_slurm_job(run, slurm_options))
        else:
            run.task = asyncio.create_task(self._run_subprocess(run))
        return run

    async def _run_subprocess(self, run: JobRun) -> None:
        if run.abort_requested:
            # Aborted before we even got scheduled -- don't spawn a process
            # just to kill it. abort_run() already set the status, persisted
            # and broadcast it.
            return
        run.status = STATUS_RUNNING
        run.started_at = time.time()
        self._persist(run)
        await run.broadcast({"type": "status", "status": run.status})
        # Off the event loop: this may wait on the pipeline lock -- see
        # _mark_pipeline_running's own docstring.
        await asyncio.to_thread(self._mark_pipeline_running, run)

        # If start_subprocess_job had to advance the output directory to
        # avoid a job-number collision, surface that one adjustment in the
        # live output before anything else.
        if run.rewrite_note:
            run.stdout_lines.append(run.rewrite_note)
            await run.broadcast({"type": "stdout", "line": run.rewrite_note})

        # Pipeline-sync registration failures go to stderr specifically (not
        # rewrite_note's stdout channel) so they land in the Errors tab,
        # where a failure is actually noticed -- see JobRun.pipeline_sync_error.
        if run.pipeline_sync_error:
            run.stderr_lines.append(run.pipeline_sync_error)
            await run.broadcast({"type": "stderr", "line": run.pipeline_sync_error})

        try:
            proc = await asyncio.create_subprocess_shell(
                run.command,
                # Run from the PROJECT ROOT, exactly like RELION -- so
                # project-root-relative input patterns (e.g. `frames/*.mrc`)
                # and the `--o <JobDir>/jobNNN/` output path resolve the same
                # way they do under RELION's own GUI. run.cwd remains the
                # job's OWN output directory (used by the Outputs tab, clean,
                # delete, download); only the process's working directory is
                # the project root.
                cwd=run.project_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # New session (setsid) so this shell becomes its own process
                # group leader -- real RELION commands are frequently a
                # shell pipeline that spawns further children (MPI launchers,
                # `` `which relion_run_x_mpi` `` substitutions, etc; see
                # job_registry.py). Without this, terminate()/abort_run()
                # below would only signal the /bin/sh wrapper itself and
                # leave those children running orphaned -- verified this
                # actually happens (a plain `sleep 30` child survived a
                # terminate() of its parent shell) before adding this.
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            run.status = STATUS_FAILED
            run.ended_at = time.time()
            run.stderr_lines.append(f"Failed to launch: {exc}")
            self._persist(run)
            await run.broadcast({"type": "stderr", "line": f"Failed to launch: {exc}"})
            await run.broadcast({"type": "status", "status": run.status})
            return

        run.proc = proc
        run.pid = proc.pid
        # Persisted immediately, not batched with a later write: if the
        # backend dies moments from now, this PID is the only way
        # abort_run() will ever be able to find and signal the real process
        # again (see JobRun.pid's docstring).
        self._persist(run)
        if run.abort_requested:
            # Abort arrived while this process was still being spawned.
            self._terminate_process_group(run)

        # RELION's own GUI always tees each job's stdout/stderr into
        # run.out/run.err inside the job's output directory -- it's not
        # something relion_refine (etc.) writes itself, it's shell
        # redirection RELION's GUI appends to the command before running it
        # (RelionJob::prepareFinalCommand, src/pipeline_jobs.cpp ~line 760:
        # `one_command += " >> " + outputname + "run.out 2>> " + outputname
        # + "run.err";`, unconditional whenever the command doesn't already
        # contain a redirect). RELION-US instead streams stdout/stderr live
        # over the websocket via asyncio PIPEs -- shell-redirecting the
        # command itself would swallow that live view -- so mirror the same
        # convention here by teeing each line to a file as it's read, in
        # append mode (">>"), matching RELION's own append (relevant for
        # Overwrite: a re-run's output accumulates, it doesn't replace).
        # Best-effort: a logging failure must never take down the job.
        def _open_log(name: str):
            try:
                return open(Path(run.cwd) / name, "a", buffering=1, errors="replace")
            except OSError:
                return None

        stdout_log = _open_log("run.out")
        stderr_log = _open_log("run.err")

        async def pump(stream, sink: list[str], msg_type: str, logfile):
            # Chunk-based, not stream.readline(): readline() raises
            # ValueError once a single line exceeds its 64 KiB limit, which
            # RELION's own in-place progress-bar animation (`~~(,_,"> yum!`,
            # printed via bare \r with no real \n between updates -- seen in
            # the run.out of literally every job this session) can and does
            # exceed on a long-running step. The old code caught that
            # exception and stopped THIS pump() coroutine cleanly, but nothing
            # else was left draining the pipe -- once the OS pipe's own
            # buffer then filled, the child's next write() call blocked
            # forever, silently hanging the whole job (not just logging)
            # with the run stuck in "running" permanently. Confirmed for
            # real: a Class2D job hung 17+ minutes at ~0% CPU, its only
            # threads parked on blocked write/futex syscalls, immediately
            # after this exact "[RELION-US] output stream error: Separator
            # is not found, and chunk exceed the limit" appeared in its
            # Errors tab. stream.read(n) has no such per-line limit --
            # \r is treated as a line terminator alongside \n (matching how
            # a real terminal would render the animation) so a single
            # in-place-updating spinner still surfaces as many short lines
            # instead of one unbounded one.
            buf = b""
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    m = _LINE_SEP_RE.search(buf)
                    if m is None:
                        break
                    raw_line, buf = buf[:m.start()], buf[m.end():]
                    decoded = raw_line.decode(errors="replace")
                    sink.append(decoded)
                    if logfile is not None:
                        try:
                            logfile.write(decoded + "\n")
                        except OSError:
                            pass
                    await run.broadcast({"type": msg_type, "line": decoded})
            if buf:
                decoded = buf.decode(errors="replace")
                sink.append(decoded)
                if logfile is not None:
                    try:
                        logfile.write(decoded + "\n")
                    except OSError:
                        pass
                await run.broadcast({"type": msg_type, "line": decoded})

        # try/finally so the run ALWAYS reaches a terminal status and is
        # persisted -- otherwise an unexpected error here leaves the Command
        # Center showing a job that runs forever, with an unreaped child.
        exit_code = None
        try:
            await asyncio.gather(
                pump(proc.stdout, run.stdout_lines, "stdout", stdout_log),
                pump(proc.stderr, run.stderr_lines, "stderr", stderr_log),
            )
            exit_code = await proc.wait()
        except Exception as exc:  # noqa: BLE001
            msg = f"[RELION-US] error while streaming output: {type(exc).__name__}: {exc}"
            run.stderr_lines.append(msg)
            await run.broadcast({"type": "stderr", "line": msg})
        finally:
            for logfile in (stdout_log, stderr_log):
                if logfile is not None:
                    try:
                        logfile.close()
                    except OSError:
                        pass
            if exit_code is None:
                # never got a clean wait() -- make sure the child is reaped
                try:
                    exit_code = await proc.wait()
                except Exception:  # noqa: BLE001
                    exit_code = proc.returncode
            run.proc = None
            await self._finalize_run(run, exit_code)

    async def _finalize_run(self, run: JobRun, exit_code: Optional[int]) -> None:
        """
        The completion tail every execution path (local subprocess,
        SLURM) shares once a run has genuinely reached a terminal state:
        record the exit code/status/end time, persist, let RELION's own
        pipeline catch up if two-way sync is on, and broadcast the final
        status. Extracted from _run_subprocess's own finally-block so
        _run_slurm_job gets the exact same RELION-pipeline-sync handshake
        a local job already gets, without duplicating it.
        """
        run.exit_code = exit_code
        # abort_run() may already have set this to STATUS_ABORTED (and
        # requested termination) -- don't let the process's exit code
        # (non-zero after a SIGTERM, or SLURM's CANCELLED-job exit code)
        # overwrite that with STATUS_FAILED.
        if run.status != STATUS_ABORTED:
            run.status = STATUS_COMPLETED if exit_code == 0 else STATUS_FAILED
        run.ended_at = time.time()
        self._persist(run)
        # Let RELION update its own record of this job, if two-way sync is
        # on. Off the event loop: relion_pipeliner takes the pipeline lock
        # and can wait on an open RELION GUI for up to a minute, which would
        # otherwise stall every other websocket in the app.
        if self.pipeline_sync_enabled(Path(run.project_dir)):
            synced = await asyncio.to_thread(
                self.sync_completion_to_relion, Path(run.project_dir))
            if not synced:
                note = ("[RELION-US] Could not update RELION's pipeline with this "
                        "job's final status. The job itself is unaffected; run "
                        "`relion_pipeliner --check_job_completion` in the project, "
                        "or open RELION's GUI, to refresh it.")
                run.stdout_lines.append(note)
                await run.broadcast({"type": "stdout", "line": note})
        await run.broadcast(
            {"type": "status", "status": run.status, "exit_code": exit_code}
        )

    SLURM_TEMPLATE = Path(__file__).resolve().parent.parent / "slurm" / "template_relion_job.sbatch"

    async def _tail_new_lines(
        self, run: JobRun, path: Path, pos: int, msg_type: str, flush_partial: bool = False
    ) -> int:
        """Append any bytes written to `path` since `pos`, broadcast each
        new line, and return the new read position. `path` may not exist
        yet (SLURM can take a moment to actually create the output file,
        and a fast job can reach a terminal state without ever being
        observed as RUNNING at all -- see _run_slurm_job) -- that's
        normal, not an error.

        Splits on the raw NEWLINE BYTE (0x0A) before ever decoding, not
        by decoding the whole chunk first and re-encoding a leftover
        remainder to figure out how many bytes to roll back: 0x0A can
        never be a continuation byte of a multi-byte UTF-8 sequence, so
        slicing at a real newline is always character-boundary-safe. The
        earlier decode-then-reencode approach could permanently corrupt a
        multi-byte character if a poll happened to land mid-character --
        this can't.

        flush_partial: when True (the caller's final pass once the job
        has reached a terminal state, so no more data is coming), a
        trailing chunk with NO newline at all is still emitted rather
        than held back forever -- otherwise a job's last line (a common
        case: a final status message with no trailing newline) would be
        silently dropped, since no later poll will ever complete it.
        """
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
                new_pos = f.tell()
        except FileNotFoundError:
            return pos
        if not chunk:
            return pos

        nl_idx = chunk.rfind(b"\n")
        if nl_idx == -1:
            if not flush_partial:
                return pos  # no complete line yet; wait for one
            complete_bytes, end_pos = chunk, new_pos
        else:
            complete_bytes, end_pos = chunk[:nl_idx], pos + nl_idx + 1

        sink = run.stdout_lines if msg_type == "stdout" else run.stderr_lines
        for line in complete_bytes.decode(errors="replace").split("\n"):
            sink.append(line)
            await run.broadcast({"type": msg_type, "line": line})
        return end_pos

    async def _run_slurm_job(self, run: JobRun, slurm_options: dict) -> None:
        """
        SLURM counterpart to _run_subprocess -- a deliberately SEPARATE
        execution path, not a variant of it. _run_subprocess's whole shape
        (live PIPE streaming, a real local `proc` handle, signal-based
        abort via process groups) assumes this app directly owns an OS
        process; a SLURM job has none of that -- no local PID, no pipes
        until the job actually starts on a compute node it was never told
        about directly. This method instead: submits via sbatch, tracks
        the job by SLURM's own job ID (run.slurm_job_id), polls
        squeue/sacct on an interval instead of watching a process exit,
        and tails the job's own output/error files (written into run.cwd
        at deterministic, pre-chosen paths -- not SLURM's %x-%j pattern,
        which this app can't resolve until AFTER submission and which
        would also land in the submission cwd rather than the job's
        tracked output directory, out of Delete's reach) rather than
        reading a live pipe. Tailing starts from the moment a job ID
        exists, not gated on ever observing the job reach RUNNING first --
        a fast job can go queued -> completed between two polls, skipping
        RUNNING entirely, and would otherwise lose 100% of its output.
        Shares only JobRun's shape, run.broadcast()'s WebSocket interface
        (so the frontend needs no changes to display either kind of run),
        and _finalize_run's completion tail with _run_subprocess.

        Exactly one call to _finalize_run happens per invocation of this
        method -- via the try/except/finally structure below, mirroring
        _run_subprocess's own try/except/finally shape, so an exception
        raised BY _finalize_run's own body (pipeline sync, etc.) can never
        trigger a second, contradictory call to it the way an inner
        try/except around just the success path once did.

        abort_run() cancels a SLURM run via `scancel` directly (not by
        cancelling run.task, unlike a custom job) -- this loop just keeps
        polling afterward and picks up the resulting CANCELLED state on
        its own next iteration, finalizing through the normal path below.
        The one gap that closes: abort_run() can only reach `scancel` once
        run.slurm_job_id is set, so an abort click that lands DURING the
        submit_sbatch() call below (the only real await point before that)
        falls through to abort_run()'s "not spawned yet" branch instead,
        setting run.status=ABORTED with nothing to cancel yet -- this
        method checks for exactly that once submission returns, and
        cancels the now-live job immediately rather than silently
        overwriting ABORTED back to QUEUED and letting it run unmanaged.
        """
        exit_code = None
        try:
            field_values = run.field_values or {}
            ntasks = int(float(field_values.get("nr_mpi", 1) or 1))
            cpus_per_task = int(float(field_values.get("nr_threads", 1) or 1))
            gres_line = ""
            if field_values.get("use_gpu"):
                gpu_ids = str(field_values.get("gpu_ids", "") or "")
                # A concrete id list ("0,1") implies 2 devices; blank means
                # "let RELION auto-allocate" (see job_registry.py's own
                # --gpu "" convention) -- request 1 GPU as a sane default
                # in that case, since SLURM (unlike RELION) has no
                # equivalent "figure it out yourself" option.
                n_gpus = len([g for g in gpu_ids.replace(":", ",").split(",") if g.strip()]) or 1
                gres_line = f"#SBATCH --gres=gpu:{n_gpus}"

            job_name = f"relion_us_job{run.job_number:03d}"
            out_path = Path(run.cwd) / "run_submit.out"
            err_path = Path(run.cwd) / "run_submit.err"
            filled = slurm_bridge.fill_sbatch_template(
                self.SLURM_TEMPLATE,
                command=run.command,
                job_name=job_name,
                account=str(slurm_options.get("account", "") or ""),
                partition=str(slurm_options.get("partition", "") or ""),
                ntasks=ntasks,
                cpus_per_task=cpus_per_task,
                mem=str(slurm_options.get("mem", "") or "4G"),
                time_limit=str(slurm_options.get("time_limit", "") or "24:00:00"),
                gres_line=gres_line,
                out_path=str(out_path),
                err_path=str(err_path),
            )
            script_path = Path(run.cwd) / "run_submit.sbatch"
            script_path.write_text(filled)

            # The only real await point before a job ID exists -- see this
            # method's own docstring on the abort-during-submission race.
            job_id = await asyncio.to_thread(
                slurm_bridge.submit_sbatch, script_path, cwd=Path(run.project_dir))
            run.slurm_job_id = job_id

            if run.status == STATUS_ABORTED:
                note = f"[RELION-US] Abort arrived during submission; cancelling SLURM job {job_id}."
                run.stdout_lines.append(note)
                await run.broadcast({"type": "stdout", "line": note})
                await asyncio.to_thread(slurm_bridge.cancel_job, job_id)
                exit_code = 1
            else:
                run.status = STATUS_QUEUED
                self._persist(run)
                note = f"[RELION-US] Submitted to SLURM as job {job_id}."
                run.stdout_lines.append(note)
                await run.broadcast({"type": "stdout", "line": note})
                await run.broadcast({"type": "status", "status": run.status})

                last_status = STATUS_QUEUED
                out_pos = err_pos = 0
                new_status = STATUS_QUEUED
                info = {"exit_code": None}

                while True:
                    await asyncio.sleep(SLURM_POLL_INTERVAL_S)
                    try:
                        info = await asyncio.to_thread(slurm_bridge.poll_job_state, job_id)
                    except Exception as exc:  # noqa: BLE001
                        # Transient polling failure (scheduler momentarily
                        # unreachable, etc.) -- log it but keep polling
                        # rather than failing a job that may well still be
                        # running.
                        msg = f"[RELION-US] SLURM status check failed: {exc}"
                        run.stderr_lines.append(msg)
                        await run.broadcast({"type": "stderr", "line": msg})
                        continue

                    run.slurm_state = info["raw_state"]
                    new_status = slurm_bridge.SLURM_STATE_TO_STATUS.get(info["raw_state"], STATUS_RUNNING)
                    is_terminal = new_status in (STATUS_COMPLETED, STATUS_FAILED, STATUS_ABORTED)

                    if new_status != last_status:
                        if run.status != STATUS_ABORTED:
                            run.status = new_status
                        self._persist(run)
                        await run.broadcast({"type": "status", "status": run.status})
                        last_status = new_status

                    out_pos = await self._tail_new_lines(
                        run, out_path, out_pos, "stdout", flush_partial=is_terminal)
                    err_pos = await self._tail_new_lines(
                        run, err_path, err_pos, "stderr", flush_partial=is_terminal)

                    if is_terminal:
                        break

                exit_code = info.get("exit_code")
                if exit_code is None:
                    exit_code = 0 if new_status == STATUS_COMPLETED else 1
            await self._finalize_run(run, exit_code)
        except asyncio.CancelledError:
            # This task being cancelled (app shutdown/reload -- abort_run()
            # deliberately never cancels it, see this method's own
            # docstring) has no effect on the actual SLURM job, which keeps
            # existing on the scheduler independent of this app's process
            # lifetime. Finalizing here with no real exit code would
            # incorrectly mark a job that's still genuinely queued/running
            # on the cluster as STATUS_FAILED. Just stop tracking it and
            # let it be: a future abort_run() (including its persisted-
            # history restart-recovery path, keyed on the already-set
            # run.slurm_job_id) can still act on it correctly later.
            raise
        except Exception as exc:  # noqa: BLE001
            # Same guarantee _run_subprocess's try/finally gives: an
            # unexpected error here must not strand the run "queued"/
            # "running" forever with nothing tracking it.
            msg = f"[RELION-US] error while managing SLURM job: {type(exc).__name__}: {exc}"
            run.stderr_lines.append(msg)
            await run.broadcast({"type": "stderr", "line": msg})
            if exit_code is None:
                exit_code = 1
            await self._finalize_run(run, exit_code)

    async def start_custom_job(
        self,
        internal_name: str,
        display_name: str,
        runner_coro_factory,
        field_values: Optional[dict] = None,
        overwrite_run_id: Optional[str] = None,
        stays_running: bool = False,
    ) -> JobRun:
        """
        runner_coro_factory: a callable taking the job's own output directory
        and returning a coroutine that does the actual work and returns a
        human-readable summary string (or raises). Used by custom_jobs.py for
        the IMOD/Warp/DeepETPicker/AreTomo2/manual-picking bridges, which
        call converters/ (or the picker) directly instead of spawning a
        subprocess. It receives the job dir so its outputs land in
        `<JobDir>/jobNNN/` -- the same directory the Outputs tab, Clean and
        Delete operate on -- rather than the project root, which would leave
        the tracked job dir empty and let successive runs silently overwrite
        each other's results.

        overwrite_run_id: same "Overwrite" semantics as
        start_subprocess_job() -- reuses the original run's run_id/cwd/
        job_number/alias/note (same job slot) instead of allocating new
        ones. Same restrictions (must exist, not still running, same
        project) apply.

        stays_running: the runner_coro_factory only VALIDATES its inputs and
        returns immediately (the Manualpick/TomoManualPick picking jobs --
        the real work is picking, done afterward through the Picker button
        against this job's own directory, not by this coroutine). When True,
        a successful return leaves the run at STATUS_RUNNING instead of
        completing it -- see set_status(), which is what actually finishes
        it (the "Done" button) once the user says so. A raised exception
        still fails/aborts the run normally either way; only the success
        path is affected.

        Registers in RELION's own pipeline the same way start_subprocess_job
        does (when two-way sync is on) -- a custom job is real work with real
        outputs, and a user who turned sync on wants to see ALL their jobs in
        RELION's own GUI, not just the ones that happen to shell out to a
        real relion_* binary. There is no real subprocess here for
        `--pipeline_control` to instrument, so _run_custom marks the process
        "Running" itself the moment work starts, and (unless stays_running)
        writes the RELION_JOB_EXIT_* marker file once the coroutine finishes
        -- see pipeline_bridge.set_process_status's own docstring for why
        both of those are needed, not just the latter.
        """
        project_dir = self.project_dir
        pipeline_sync_error = None

        if overwrite_run_id is not None:
            target = self._resolve_overwrite_target(
                overwrite_run_id, project_dir, allow_running=stays_running)
            run_id = overwrite_run_id
            cwd = Path(target["cwd"])
            job_number = target["job_number"]
            alias, note = target["alias"], target["note"]
            registered = self.pipeline_sync_enabled(project_dir)
        else:
            run_id = self.new_run_id()
            registered = None
            if self.pipeline_sync_enabled(project_dir):
                try:
                    registered = self._register_in_relion_pipeline(
                        project_dir, internal_name, field_values or {})
                except pipeline_bridge.PipelineBridgeError as exc:
                    pipeline_sync_error = str(exc)
            if registered:
                job_number = registered["job_number"]
                cwd = project_dir / registered["process_name"]
            else:
                job_number = self._next_job_number(project_dir, internal_name)
                cwd = project_dir / f"{job_catalog.job_dirname(internal_name)}/job{job_number:03d}"
            alias, note = "", ""
            if registered:
                pipeline_sync_error = None
            elif pipeline_sync_error:
                pipeline_sync_error = (
                    f"[RELION-US] Could not register this job in RELION's own "
                    f"pipeline: {pipeline_sync_error} Continuing with this app's "
                    f"own job numbering instead -- the job itself is unaffected."
                )
        cwd.mkdir(parents=True, exist_ok=True)

        detect_text = " ".join(str(v) for v in (field_values or {}).values())
        run = JobRun(
            run_id=run_id,
            internal_name=internal_name,
            display_name=display_name,
            command=f"{IN_PROCESS_COMMAND_PREFIX}{internal_name}>",
            cwd=str(cwd),
            alias=alias,
            note=note,
            project_dir=str(project_dir),
            job_number=job_number,
            field_values=field_values,
            pipeline_sync_error=pipeline_sync_error,
            detected_inputs=_detect_inputs(detect_text, project_dir, cwd),
            pipeline_registered=bool(registered),
        )
        self.runs[run_id] = run
        self._persist(run)
        run.task = asyncio.create_task(
            self._run_custom(run, runner_coro_factory, cwd, stays_running=stays_running))
        return run

    async def _run_custom(
        self, run: JobRun, runner_coro_factory, job_dir: Path, stays_running: bool = False
    ) -> None:
        if run.abort_requested:
            return  # aborted before this task got scheduled; see _run_subprocess
        run.status = STATUS_RUNNING
        run.started_at = time.time()
        self._persist(run)
        await run.broadcast({"type": "status", "status": run.status})
        # Off the event loop: this may wait on the pipeline lock -- see
        # _mark_pipeline_running's own docstring.
        await asyncio.to_thread(self._mark_pipeline_running, run)
        if run.pipeline_sync_error:
            run.stderr_lines.append(run.pipeline_sync_error)
            await run.broadcast({"type": "stderr", "line": run.pipeline_sync_error})
        stayed_running = False
        try:
            result = await runner_coro_factory(job_dir)
            for line in str(result).splitlines() or ["(no output)"]:
                run.stdout_lines.append(line)
                await run.broadcast({"type": "stdout", "line": line})
            if stays_running:
                # Validated, not finished -- see start_custom_job's own
                # docstring. set_status() (the Done button) finishes it.
                stayed_running = True
            else:
                run.status = STATUS_COMPLETED
                run.exit_code = 0
        except asyncio.CancelledError:
            run.status = STATUS_ABORTED
            run.stderr_lines.append("Aborted by user.")
            await run.broadcast({"type": "stderr", "line": "Aborted by user."})
            raise
        except Exception as exc:  # noqa: BLE001
            run.stderr_lines.append(f"{type(exc).__name__}: {exc}")
            await run.broadcast({"type": "stderr", "line": f"{type(exc).__name__}: {exc}"})
            run.status = STATUS_FAILED
            run.exit_code = 1
        finally:
            if not stayed_running:
                run.ended_at = time.time()
            self._persist(run)
            if run.pipeline_registered and not stayed_running:
                await self._finish_pipeline_registration(run, job_dir)
            await run.broadcast({"type": "status", "status": run.status, "exit_code": run.exit_code})

    async def _write_exit_marker_and_sync(self, status: str, project_dir: Path, job_dir: Path) -> bool:
        """The exit-marker + --check_job_completion handshake for a
        registered run reaching a terminal status -- writes the
        RELION_JOB_EXIT_* file a real relion_* program would have written
        (see pipeline_control.h) into job_dir, then calls relion_pipeliner
        --check_job_completion off the event loop. Shared by every path
        that finishes a registered run: _run_custom's own completion,
        set_status()'s "Done"/"Mark as failed" (including a job that
        deliberately stayed running until then -- see start_custom_job's
        stays_running docstring), and the persisted-only fallback for a run
        from a previous backend session. Returns whether the sync call
        itself succeeded (the marker file is written either way)."""
        marker = {
            STATUS_COMPLETED: "RELION_JOB_EXIT_SUCCESS",
            STATUS_FAILED: "RELION_JOB_EXIT_FAILURE",
            STATUS_ABORTED: "RELION_JOB_EXIT_ABORTED",
        }.get(status)
        if marker:
            try:
                (job_dir / marker).touch()
            except OSError:
                pass
        return await asyncio.to_thread(self.sync_completion_to_relion, project_dir)

    async def _finish_pipeline_registration(self, run: JobRun, job_dir: Path) -> None:
        """_write_exit_marker_and_sync for a LIVE run -- also surfaces a
        sync failure into the run's own Errors tab, which the persisted
        -only fallback (nothing subscribed to broadcast to) can't do."""
        synced = await self._write_exit_marker_and_sync(run.status, Path(run.project_dir), job_dir)
        if not synced:
            note = ("[RELION-US] Could not update RELION's pipeline with this "
                    "job's final status. The job itself is unaffected; run "
                    "`relion_pipeliner --check_job_completion` in the project, "
                    "or open RELION's GUI, to refresh it.")
            run.stdout_lines.append(note)
            await run.broadcast({"type": "stdout", "line": note})

    # --- Command Center job actions -----------------------------------
    # Real RELION job actions this mirrors (see gui_mainwindow.cpp's "Job
    # actions" menu, ~line 703): Alias, Overwrite (see
    # start_subprocess_job's overwrite_run_id), Abort running, Mark as
    # finished, Mark as failed, Delete. "Edit Note" is real RELION too
    # (a free-text annotation per job) -- exposed here as set_note().

    @staticmethod
    def _pgids_in_session(sid: int) -> set[int]:
        """Every distinct process group id among processes belonging to
        session `sid`. start_new_session=True (see _run_subprocess) makes
        the launched shell both the session id and its own initial process
        group -- but that is not the only process group in the session by
        the time a real job is running: MPI launchers (prterun/orted/
        mpirun, which every multi-rank RELION command uses) commonly move
        their worker ranks into process groups of their own while staying
        in the same session, specifically so a signal sent to the launcher's
        group doesn't automatically reach them. Confirmed live against a
        real relion_refine_mpi run: its two worker ranks -- the processes
        actually using the CPU/GPU -- had PGIDs distinct from the /bin/sh
        wrapper's, sharing only the session, so killing only that one group
        left them running untouched. Linux-only (/proc), matching the
        killpg/start_new_session=True this module already assumes."""
        pgids: set[int] = set()
        try:
            candidates = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return pgids
        for pid_str in candidates:
            try:
                stat = Path(f"/proc/{pid_str}/stat").read_text()
                # "pid (comm) state ppid pgrp session ..." -- comm can itself
                # contain spaces or parens, so split after the LAST ')'
                # rather than on whitespace from the start.
                fields = stat.rsplit(")", 1)[1].split()
                pgrp, session = int(fields[2]), int(fields[3])
            except (OSError, IndexError, ValueError):
                continue
            if session == sid:
                pgids.add(pgrp)
        return pgids

    @staticmethod
    def _kill_session(leader_pid: int, sig: int) -> bool:
        """Signal every process group in the session `leader_pid` leads (see
        _pgids_in_session), not just its own -- falls back to the leader's
        own process group alone, then to the bare pid, if session
        inspection finds nothing (process already gone, or /proc
        unavailable). Returns whether any kill signal was actually sent."""
        pgids = JobRunManager._pgids_in_session(leader_pid)
        if not pgids:
            try:
                pgids = {os.getpgid(leader_pid)}
            except (ProcessLookupError, OSError):
                pgids = {leader_pid}
        sent = False
        for pgid in pgids:
            try:
                os.killpg(pgid, sig)
                sent = True
            except (ProcessLookupError, PermissionError, OSError):
                continue
        return sent

    @staticmethod
    def _terminate_process_group(run: JobRun) -> None:
        """Signal the whole session (see _kill_session), not just the
        /bin/sh wrapper's own process group -- a plain terminate() only
        reaches the shell itself and can leave its actual children (the
        real relion_* command, and any MPI worker ranks it spawned into
        their own process groups) running orphaned. Falls back to
        proc.terminate() if session-wide signalling found nothing to kill."""
        if run.proc is None:
            return
        if not JobRunManager._kill_session(run.proc.pid, signal.SIGTERM):
            try:
                run.proc.terminate()
            except ProcessLookupError:
                pass

    @staticmethod
    def _pid_matches_persisted_run(pid: int, entry: dict) -> bool:
        """Safety check before signalling a PID recovered from persisted
        history rather than a live handle: PIDs get reused by the OS, so
        blindly killing "whatever is running at that number now" risks
        killing a completely unrelated process that happens to have started
        later with the same number. Reads /proc/<pid>/cmdline (Linux-only,
        matching the os.killpg/start_new_session=True this module already
        assumes) and looks for this run's own project-relative output
        directory -- unique per job, and always part of the real command's
        --o/--output-directory argument -- rather than trusting the PID
        alone. False (not raising) for any read failure, including the
        process simply no longer existing."""
        cwd, project_dir = entry.get("cwd"), entry.get("project_dir")
        if not cwd or not project_dir:
            return False
        try:
            subdir = str(Path(cwd).relative_to(Path(project_dir)))
        except ValueError:
            return False
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return False
        return subdir in cmdline

    async def abort_run(self, run_id: str) -> bool:
        """Real RELION 'Abort running' (gui_mainwindow.cpp's cb_abort ->
        pipeline's kill-the-running-process path). Returns False if the run
        doesn't exist or isn't currently running. Sets status to ABORTED
        immediately (optimistic -- see _run_subprocess's guard against this
        being overwritten by the process's own non-zero exit code from the
        termination signal) before actually asking the process/task to
        stop, so the UI reflects the abort right away rather than racing
        the process's own shutdown.

        If the run isn't live in THIS session (self.runs is in-memory only,
        so a backend restart loses every handle) but persisted history still
        says pending/running, there is no live asyncio handle here to signal
        -- but the real OS process may well still be running regardless
        (confirmed live: a backend restart orphans the process, it just
        keeps going, invisible to this app until something notices). Using
        the PID persisted at launch (see JobRun.pid), verified against
        _pid_matches_persisted_run before touching it, actually signals that
        process rather than only updating a status nothing has verified --
        the earlier version of this fallback did the latter, which left the
        real compute running indefinitely with the UI confidently, wrongly,
        showing the job as aborted. Reconciles the persisted status to
        aborted either way (verified-killed or nothing left to find),
        because leaving it at "running" forever blocks Overwrite and Mark as
        finished/failed too, both of which refuse to touch a "running" job
        by design."""
        run = self.get(run_id)
        if run is None:
            history = project_manager.load_history(self.project_dir)
            entry = next((h for h in history if h.get("run_id") == run_id), None)
            if entry is None or entry.get("status") not in (STATUS_PENDING, STATUS_RUNNING, STATUS_QUEUED):
                return False
            slurm_job_id = entry.get("slurm_job_id")
            if slurm_job_id is not None:
                # Unlike a local PID (meaningless after a restart -- no live
                # handle, no process-group membership to walk), a SLURM job
                # ID is still directly actionable: the scheduler tracks it
                # independently of this app's own process lifetime.
                # Best-effort, same as the PID path below: scancel raises
                # if the job already finished naturally (a real race --
                # nothing stops that between the last poll and this click)
                # or the scheduler is briefly unreachable; either way, the
                # persisted status is reconciled to aborted regardless, so
                # a raised RuntimeError here must not propagate and leave
                # this run stuck un-reconciled (or 500 the HTTP endpoint).
                try:
                    await asyncio.to_thread(slurm_bridge.cancel_job, slurm_job_id)
                except Exception:  # noqa: BLE001
                    pass
            else:
                pid = entry.get("pid")
                if pid is not None and self._pid_matches_persisted_run(pid, entry):
                    self._kill_session(pid, signal.SIGTERM)
            self._update_persisted_only(run_id, status=STATUS_ABORTED, ended_at=time.time())
            return True
        # PENDING counts: there is a real window between start_*_job() creating
        # the run and its task setting status to RUNNING, so a click landing in
        # that window must still abort cleanly. The abort_requested flag below
        # covers the process handle not existing yet. QUEUED (SLURM, sitting
        # in the scheduler) is abortable too -- see the slurm_job_id branch.
        if run.status not in (STATUS_PENDING, STATUS_RUNNING, STATUS_QUEUED):
            return False
        run.status = STATUS_ABORTED
        run.ended_at = time.time()
        self._persist(run)
        await run.broadcast({"type": "stderr", "line": "Aborted by user."})
        await run.broadcast({"type": "status", "status": run.status})
        if run.slurm_job_id is not None:
            # _run_slurm_job's poll loop is still running as run.task -- it
            # is NOT cancelled here (unlike the custom-job branch below):
            # scancel is fire-and-forget from SLURM's side, so the loop's
            # own next poll is what confirms the cancellation actually
            # landed (via squeue/sacct reporting CANCELLED) and finalizes
            # through the normal path, exit code included. This preserves
            # STATUS_ABORTED (just set above) rather than letting a later
            # poll overwrite it -- see _run_slurm_job's own status-update
            # guard. Best-effort: scancel can raise (job already finished
            # naturally, scheduler briefly unreachable) -- status was
            # already optimistically set and broadcast above regardless,
            # matching the local-subprocess path's own "reconcile either
            # way" philosophy; must not 500 the abort HTTP endpoint over
            # what's already, functionally, a successful abort from the
            # user's perspective.
            try:
                await asyncio.to_thread(slurm_bridge.cancel_job, run.slurm_job_id)
            except Exception as exc:  # noqa: BLE001
                msg = f"[RELION-US] scancel failed (job may have already finished): {exc}"
                run.stderr_lines.append(msg)
                await run.broadcast({"type": "stderr", "line": msg})
        elif run.proc is not None:
            self._terminate_process_group(run)
        elif run.is_custom_job:
            # In-process converter job: the task IS the work, so cancelling it
            # is the abort.
            if run.task is not None:
                run.task.cancel()
        else:
            # Subprocess job whose handle doesn't exist YET -- we're inside the
            # short window between "status = running" and create_subprocess_shell
            # returning. Cancelling the launcher here could kill it mid-spawn and
            # leave a process group running with nothing tracking it (exactly what
            # start_new_session=True + killpg exist to prevent). Record the intent
            # instead; _run_subprocess signals the group the moment it has one.
            run.abort_requested = True
        return True

    def backfill_missing_timestamps(self, project_dir: Optional[Path] = None) -> list[dict]:
        """One-time repair for run_history.json entries with no
        started_at/ended_at recorded -- e.g. a run from before abort_run()
        could reconcile an orphaned "running" status (see its docstring),
        or one whose backend crashed before a single _persist() ever ran.
        Fills in whatever project_manager.estimate_job_timestamps can find
        from the job directory's own files, marks every filled entry
        `timestamp_estimated` so the UI never confuses a guess for a
        recorded fact (see that function's docstring for exactly how wrong
        an estimate can be if the project was copied after the jobs ran),
        and leaves anything it can't estimate untouched. A currently-live
        run in self.runs is skipped -- its own status/timing win, this only
        ever touches history nothing in this session is tracking.

        Returns the updated entries (empty if there was nothing to do).
        """
        pd = Path(project_dir) if project_dir is not None else self.project_dir
        history = project_manager.load_history(pd)
        updated = []
        for entry in history:
            if entry.get("run_id") in self.runs:
                continue
            cwd = entry.get("cwd")
            status = entry.get("status")
            needs_start = entry.get("started_at") is None
            needs_end = (
                entry.get("ended_at") is None
                and status not in (STATUS_PENDING, STATUS_RUNNING, STATUS_QUEUED)
            )
            if not cwd or not (needs_start or needs_end):
                continue
            started_at, ended_at = project_manager.estimate_job_timestamps(Path(cwd), status)
            changed = False
            if needs_start and started_at is not None:
                entry["started_at"] = started_at
                changed = True
            if needs_end and ended_at is not None:
                entry["ended_at"] = ended_at
                changed = True
            if changed:
                entry["timestamp_estimated"] = True
                updated.append(entry)
        if updated:
            project_manager.save_history(pd, history)
        return updated

    def _update_persisted_only(self, run_id: str, **fields: Any) -> Optional[dict]:
        """Fallback for set_alias()/set_note()/set_status()/abort_run() when
        a run only survives in persisted history (a previous backend
        session -- self.runs is in-memory and empty again after every
        restart). Editing metadata, overriding a stuck terminal status, and
        reconciling a run whose "running" status outlived the session that
        would have updated it all need to work the same whether the run is
        still live in this session's memory or not."""
        history = project_manager.load_history(self.project_dir)
        entry = next((h for h in history if h.get("run_id") == run_id), None)
        if entry is None:
            return None
        entry.update(fields)
        project_manager.save_history(self.project_dir, history)
        return entry

    def set_alias(self, run_id: str, alias: str) -> Optional[dict]:
        """Real RELION 'Alias' job action (gui_mainwindow.cpp's
        cb_set_alias) for a run this app tracks itself. An empty string
        clears the alias, reverting display to the plain job number. A
        RELION-native run_id (source: "relion") goes through
        set_relion_overlay instead -- see project_manager.set_relion_
        overlay's own module comment for why alias/note stay a purely
        local overlay for those rather than touching RELION's own files."""
        alias = alias.strip()
        if self.is_relion_run(run_id):
            entry = next(
                (e for e in self._relion_pipeline_entries(self.project_dir) if e["run_id"] == run_id),
                None,
            )
            if entry is None:
                return None
            project_manager.set_relion_overlay(self.project_dir, run_id, alias=alias)
            entry["alias"] = alias
            # An empty alias clears back to the plain job number (Path(cwd).name,
            # e.g. "job042"), NOT whatever RELION's own real alias underneath
            # it says -- same "explicitly cleared" semantic _relion_pipeline_
            # entries' own overlay merge uses.
            entry["job_name"] = alias or Path(entry["cwd"]).name
            return entry
        run = self.get(run_id)
        if run is None:
            history = project_manager.load_history(self.project_dir)
            entry = next((h for h in history if h.get("run_id") == run_id), None)
            job_name = alias or f"job{entry.get('job_number', 0):03d}" if entry else alias
            return self._update_persisted_only(run_id, alias=alias, job_name=job_name)
        run.alias = alias
        self._persist(run)
        return run.to_summary()

    def set_note(self, run_id: str, note: str) -> Optional[dict]:
        """Real RELION 'Edit Note' job action for a run this app tracks
        itself. A RELION-native run_id goes through set_relion_overlay
        instead -- see set_alias's own docstring for why."""
        if self.is_relion_run(run_id):
            entry = next(
                (e for e in self._relion_pipeline_entries(self.project_dir) if e["run_id"] == run_id),
                None,
            )
            if entry is None:
                return None
            project_manager.set_relion_overlay(self.project_dir, run_id, note=note)
            entry["note"] = note
            return entry
        run = self.get(run_id)
        if run is None:
            return self._update_persisted_only(run_id, note=note)
        run.note = note
        self._persist(run)
        return run.to_summary()

    async def set_status(self, run_id: str, status: str) -> Optional[dict]:
        """Real RELION 'Mark as finished' / 'Mark as failed' job actions
        (gui_mainwindow.cpp's cb_mark_as_finished/cb_mark_as_failed) — a
        manual override for when a run's tracked status doesn't match what
        actually happened (e.g. the backend was restarted mid-run and the
        process kept going/died outside this app's view). Restricted to
        MANUALLY_SETTABLE_STATUSES; raises ValueError otherwise so the API
        layer can turn that into a clean 400 rather than silently no-op'ing
        or allowing a nonsensical manual "running"/"pending" override.

        This is also the ONLY route to a terminal status for a run that
        deliberately stays "running" until the user says otherwise (the
        Manualpick/TomoManualPick picking jobs -- see start_custom_job's
        stays_running parameter): its "Done" button calls this exactly like
        "Mark as finished" does, which is what actually performs the
        RELION-pipeline completion handshake (_write_exit_marker_and_sync)
        for a registered run, not just a local status flip. Async now for
        that reason -- the handshake calls out to relion_pipeliner.
        """
        if status not in MANUALLY_SETTABLE_STATUSES:
            raise ValueError(f"status must be one of {sorted(MANUALLY_SETTABLE_STATUSES)}")
        run = self.get(run_id)
        if run is None:
            history = project_manager.load_history(self.project_dir)
            entry = next((h for h in history if h.get("run_id") == run_id), None)
            if entry is None:
                return None
            if entry.get("pipeline_registered") and entry.get("project_dir") and entry.get("cwd"):
                await self._write_exit_marker_and_sync(
                    status, Path(entry["project_dir"]), Path(entry["cwd"]))
            return self._update_persisted_only(run_id, status=status, ended_at=time.time())
        run.status = status
        if run.ended_at is None:
            run.ended_at = time.time()
        self._persist(run)
        if run.pipeline_registered:
            await self._finish_pipeline_registration(run, Path(run.cwd))
        await run.broadcast({"type": "status", "status": run.status, "exit_code": run.exit_code})
        return run.to_summary()

    async def resume_run(self, run_id: str) -> Optional[dict]:
        """The picking jobs' "Continue" toolbar action -- the non
        -destructive counterpart to Overwrite (which clears the job's
        existing picks first, see custom_jobs.run_manual_pick's own
        docstring): puts a finished run back to "running" WITHOUT touching
        anything in its directory, so reopening the Picker shows exactly
        the picks that were already there. Only meaningful for a run that
        deliberately stays "running" until told otherwise (start_custom_
        job's stays_running) rather than a real subprocess/compute job,
        which genuinely finished and has nothing left to "resume" --
        restricted to the picking job types at the API layer (main.py),
        same as stays_running itself.

        Raises ValueError if the run isn't in a resumable status.
        Mirrors RelionJob's own re-run preamble
        (pipeline_control_delete_exit_files, src/pipeline_control.cpp) --
        clears any stale RELION_JOB_EXIT_*/ABORT_NOW files left over from
        the previous session before marking it "Running" again, so a
        --check_job_completion firing later (for this job or completely
        unrelated ones sharing the same poll) can't immediately re-finish
        it on a leftover marker nothing actually produced this time.
        """
        run = self.get(run_id)
        if run is None:
            history = project_manager.load_history(self.project_dir)
            entry = next((h for h in history if h.get("run_id") == run_id), None)
            if entry is None:
                return None
            if entry.get("status") not in RESUMABLE_STATUSES:
                raise ValueError(f"status must be one of {sorted(RESUMABLE_STATUSES)} to resume")
            if entry.get("cwd"):
                self._delete_pipeline_control_files(Path(entry["cwd"]))
            if entry.get("pipeline_registered") and entry.get("project_dir") and entry.get("cwd"):
                try:
                    process_name = str(Path(entry["cwd"]).relative_to(entry["project_dir"]))
                    pipeline_bridge.set_process_status(
                        Path(entry["project_dir"]), process_name, "Running")
                except (ValueError, pipeline_bridge.PipelineBridgeError):
                    pass
            return self._update_persisted_only(
                run_id, status=STATUS_RUNNING, ended_at=None, exit_code=None)
        if run.status not in RESUMABLE_STATUSES:
            raise ValueError(f"status must be one of {sorted(RESUMABLE_STATUSES)} to resume")
        self._delete_pipeline_control_files(Path(run.cwd))
        run.status = STATUS_RUNNING
        run.ended_at = None
        run.exit_code = None
        self._persist(run)
        await asyncio.to_thread(self._mark_pipeline_running, run)
        await run.broadcast({"type": "status", "status": run.status, "exit_code": run.exit_code})
        return run.to_summary()

    @staticmethod
    def _delete_pipeline_control_files(job_dir: Path) -> None:
        """Same files pipeline_control_delete_exit_files() removes at the
        start of a real RELION run -- see resume_run's own docstring for
        why stale ones matter here."""
        for name in (
            "RELION_JOB_ABORT_NOW", "RELION_JOB_EXIT_SUCCESS",
            "RELION_JOB_EXIT_FAILURE", "RELION_JOB_EXIT_ABORTED",
        ):
            try:
                (job_dir / name).unlink(missing_ok=True)
            except OSError:
                pass

    def delete_run(self, run_id: str, remove_files: bool) -> tuple[bool, str]:
        """Real RELION 'Delete' job action. Always removes the run from
        this app's tracked history (in-memory + persisted). If
        remove_files, moves the run's own output directory into Trash/
        instead of destroying it (see project_manager.move_to_trash --
        mirrors RELION's own Delete exactly, which is a move-to-Trash, not
        a permanent removal; see issue #2) -- always safe to do
        unconditionally here (unlike Clean/Harsh Clean, see
        cleanup_candidates() below) because that directory is one
        RELION-US itself created exclusively for this run; nothing else
        can be living in it. Refuses (returns False, reason) rather than
        touching anything outside that directory, or a still-running job's
        directory out from under it. remove_files=False (keep the
        directory exactly where it is, just stop tracking it) is a
        deliberately separate, non-Trash action -- out of Trash's scope,
        unchanged by issue #2.

        Works the same whether this run is still live in self.runs (this
        session) or only survives in persisted history (a run from a
        previous backend session -- self.runs is in-memory and empty again
        after every restart) -- cwd/project_dir/status are read from
        whichever source has them, since both need to support "delete this
        old job's output directory," not just ones started this session."""
        run = self.get(run_id)
        if run is not None:
            status, cwd, project_dir = run.status, run.cwd, run.project_dir
            summary = run.to_summary()
        else:
            history = project_manager.load_history(self.project_dir)
            entry = next((h for h in history if h.get("run_id") == run_id), None)
            if entry is None:
                return False, "Unknown run_id"
            status, cwd, project_dir = entry.get("status"), entry.get("cwd"), entry.get("project_dir")
            summary = entry

        if status in (STATUS_RUNNING, STATUS_QUEUED):
            return False, "Cannot delete a job that is still running -- abort it first"

        if remove_files and cwd and project_dir:
            try:
                trashed_dir = project_manager.move_to_trash(Path(project_dir), Path(cwd))
            except (ValueError, FileExistsError, OSError) as exc:
                return False, str(exc)
            try:
                project_manager.write_trash_sidecar(trashed_dir, summary)
            except OSError as exc:
                # The move already succeeded; without a sidecar the
                # directory would be invisible to list_trash (nothing
                # globs for it) -- an orphan nobody can Restore or
                # permanently delete except by wiping ALL of Trash/. Found
                # in code review: roll back rather than leave that.
                try:
                    shutil.move(str(trashed_dir), cwd)
                except OSError:
                    pass  # best-effort; report the original failure regardless
                return False, f"Could not record trash metadata, so the move was rolled back: {exc}"

        self.runs.pop(run_id, None)
        target_dir = Path(project_dir) if project_dir else self.project_dir
        try:
            history = project_manager.load_history(target_dir)
            project_manager.save_history(
                target_dir, [h for h in history if h.get("run_id") != run_id]
            )
        except OSError as exc:
            # The trash move (if any) already succeeded and is fully
            # recoverable via its own sidecar regardless of what happens
            # to history here -- report the failure rather than let an
            # OSError escape this method's documented (bool, str) contract
            # uncaught into an unhandled 500 (found in code review).
            note = " (its files were already moved to Trash and remain recoverable there)" if remove_files else ""
            return False, f"Could not update job history: {exc}{note}"
        if summary.get("pipeline_registered") and summary.get("job_number"):
            # This app's own record is gone, but relion_pipeliner has no CLI
            # verb to remove the matching process from RELION's own
            # default_pipeline.star (see project_manager.
            # load_relion_deleted_job_numbers's docstring) -- without this,
            # that untouched process reappears next refresh as a read-only
            # "relion:jobNNN" ghost row. Marking it hidden is purely local
            # bookkeeping, not a write to RELION's file.
            project_manager.mark_relion_job_number_deleted(target_dir, summary["job_number"])
        return True, "Moved to Trash" if remove_files else "Deleted"

    def restore_from_trash(self, trash_id: str) -> Optional[JobRun]:
        """The "Restore" trash action -- mirrors RELION's own PipeLine::
        undeleteJob (moves the directory back, re-adds a pipeline/history
        record). Returns the restored run (freshly re-added to both
        self.runs and persisted history, exactly as it was recorded at
        delete time), or None if trash_id doesn't resolve to a real
        trashed job. Raises FileExistsError (propagated from
        project_manager.restore_from_trash) if something already occupies
        the original slot -- a real, reachable case: job numbers CAN be
        reused once a job's history entry is gone (see that function's
        own docstring), so a later, different job may legitimately have
        taken the trashed job's old slot by the time someone restores it."""
        try:
            summary = project_manager.restore_from_trash(self.project_dir, trash_id)
        except ValueError:
            return None
        run = JobRun(
            run_id=summary["run_id"], internal_name=summary["internal_name"],
            display_name=summary["display_name"], command=summary["command"],
            cwd=summary["cwd"], project_dir=summary["project_dir"],
            job_number=summary["job_number"], status=summary["status"],
            alias=summary.get("alias", ""), note=summary.get("note", ""),
            field_values=summary.get("field_values"),
            detected_inputs=summary.get("detected_inputs", []),
            exit_code=summary.get("exit_code"), pid=summary.get("pid"),
            slurm_job_id=summary.get("slurm_job_id"), slurm_state=summary.get("slurm_state"),
            started_at=summary.get("started_at"), ended_at=summary.get("ended_at"),
            pipeline_registered=summary.get("pipeline_registered", False),
        )
        # Live stdout/stderr lines were never part of the persisted summary
        # (to_summary() doesn't carry them, same as any other reopened
        # history entry) -- a restored run's Outputs/Errors tabs work from
        # its files on disk, same as before it was ever trashed.
        self.runs[run.run_id] = run
        history = project_manager.load_history(self.project_dir)
        project_manager.save_history(
            self.project_dir, [h for h in history if h.get("run_id") != run.run_id] + [run.to_summary()]
        )
        if run.pipeline_registered and run.job_number:
            # Undo delete_run's own hide -- own_job_numbers in list_runs
            # already excludes RELION's duplicate for a tracked job number,
            # this just stops the hidden set accumulating restored jobs.
            project_manager.unmark_relion_job_number_deleted(self.project_dir, run.job_number)
        return run

    def _safe_rmtree(self, cwd_str: str, project_dir_str: str) -> tuple[bool, str]:
        cwd = Path(cwd_str).resolve()
        project_dir = Path(project_dir_str).resolve()
        if project_dir not in cwd.parents:
            # Should be unreachable (this app always allocates cwd under
            # project_dir) -- refuse rather than risk deleting outside the
            # project on some future code path that doesn't.
            return False, f"Refusing to delete a directory outside the project: {cwd}"
        if cwd.exists():
            shutil.rmtree(cwd, ignore_errors=False)
        return True, "ok"

    # --- Output files: listing, download, Clean / Harsh Clean ----------
    #
    # RELION's own Gentle/Harsh Clean (PipeLine::cleanupJob in
    # pipeliner.cpp, do_harsh flag) is a per-job-TYPE dispatch table of
    # exact glob patterns -- e.g. MotionCorr's gentle clean removes
    # *.com/*.err/*.out/*.log, its harsh clean removes the entire movies
    # subdirectory; Class2D/Class3D/Refine3D gentle-clean removes
    # intermediate-iteration files that aren't referenced elsewhere in
    # RELION's own pipeline graph. RELION-US does NOT mechanically
    # reimplement that per-type dispatch table here, for two concrete
    # reasons: (1) RELION-US jobs are never registered in RELION's own
    # default_pipeline.star (project_manager.py's module docstring --
    # writing that file is deliberately left to RELION's own tools only),
    # so `relion_pipeliner --gentle_clean`/`--harsh_clean`, which look up a
    # job by its index in that file, can't be used against them; and (2)
    # porting ~10 branches of C++ glob-pattern logic by hand risks exactly
    # the "something happened that you didn't ask for and can't see" class
    # of bug this whole app exists to avoid -- for a file *execution*
    # command that's recoverable (edit and re-run); for a file *deletion*
    # it's not.
    #
    # So Clean / Harsh Clean here are transparent and manual instead:
    # list_output_files() shows every file actually in the job's own
    # output directory with its size, cleanup_candidates() pre-checks a
    # suggested subset (see its own docstring for exactly what), and
    # nothing is deleted until the user reviews and confirms via
    # delete_output_files() -- the same "you approve the exact thing that
    # happens" principle this app already applies to the command box,
    # applied to deletion instead of execution.

    def _resolve_run_cwd(self, run_id: str) -> Optional[Path]:
        """cwd for a run whether it's still live in self.runs (this
        session) or only survives in persisted history (a previous
        session) -- see delete_run()'s docstring for why both need to be
        supported for file operations, not just in-memory ones."""
        run = self.get(run_id)
        if run is not None:
            return Path(run.cwd)
        if self.is_relion_run(run_id):
            # A job RELION itself ran. Its directory is real and full of real
            # output, so browsing files and reading its per-iteration progress
            # work exactly as they do for this app's own runs -- an old
            # classification's resolution curve is worth seeing.
            detail = self.relion_run_detail(run_id)
            return Path(detail["cwd"]) if detail else None
        entry = next(
            (h for h in project_manager.load_history(self.project_dir) if h.get("run_id") == run_id),
            None,
        )
        if entry is None or not entry.get("cwd"):
            return None
        return Path(entry["cwd"])

    def list_output_files(self, run_id: str) -> Optional[list[dict]]:
        cwd = self._resolve_run_cwd(run_id)
        if cwd is None:
            return None
        if not cwd.is_dir():
            return []
        out = []
        for p in sorted(cwd.rglob("*")):
            if p.is_file():
                try:
                    stat = p.stat()
                except OSError:
                    continue
                out.append(
                    {
                        "path": str(p.relative_to(cwd)),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )
        return out

    # Generic (not per-job-type) housekeeping patterns that DO appear
    # verbatim, repeatedly, across RELION's own real gentle-clean branches
    # (verified against pipeliner.cpp's cleanupJob(): MotionCorr's gentle
    # branch is exactly *.com/*.err/*.out/*.log; CTF refine and polish
    # branches remove their own *_fit.star/*_fit.eps-style intermediates).
    # Used only as a pre-checked SUGGESTION in the review list below --
    # the user can uncheck or add anything before confirming.
    _GENTLE_CLEAN_SUFFIXES = (".com", ".err", ".out", ".log", ".old")
    # Harsh clean, per RELION's own real branches, generally escalates to
    # "remove the large binary intermediates too" (e.g. MotionCorr's harsh
    # branch removes the whole raw-movies subdirectory; Polish's harsh
    # branch removes *_shiny.mrcs). Rather than guess which specific large
    # files are safe to remove per job type, harsh here pre-checks every
    # file over `harsh_size_threshold` bytes in addition to the gentle
    # patterns -- still just a suggestion, reviewed before deletion.
    _HARSH_SIZE_THRESHOLD_DEFAULT = 100 * 1024 * 1024  # 100 MB

    def cleanup_candidates(
        self, run_id: str, harsh: bool, harsh_size_threshold: int = _HARSH_SIZE_THRESHOLD_DEFAULT
    ) -> Optional[list[dict]]:
        """Returns list_output_files() output with a `suggested` bool added
        per file -- pre-checked for the Clean ("gentle") or Harsh Clean
        review list. See the class-level docstring above this method for
        why this is a generic size/extension suggestion rather than a port
        of RELION's own per-job-type cleanup dispatch table."""
        files = self.list_output_files(run_id)
        if files is None:
            return None
        for f in files:
            suggested = f["path"].endswith(self._GENTLE_CLEAN_SUFFIXES)
            if harsh:
                suggested = suggested or f["size"] >= harsh_size_threshold
            f["suggested"] = suggested
        return files

    def resolve_output_file(self, run_id: str, relative_path: str) -> Optional[Path]:
        """Safety-checked join: resolves `relative_path` against the run's
        cwd and refuses (returns None) if the result would land outside
        that directory (e.g. a `../../` traversal attempt) or doesn't
        exist as a file. Used by both the single-file download endpoint
        and delete_output_files() below."""
        run_cwd = self._resolve_run_cwd(run_id)
        if run_cwd is None:
            return None
        cwd = run_cwd.resolve()
        candidate = (cwd / relative_path).resolve()
        if cwd != candidate and cwd not in candidate.parents:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def delete_output_files(self, run_id: str, relative_paths: list[str]) -> dict:
        """Deletes exactly the files the user checked and confirmed (the
        Clean / Harsh Clean review list) -- one at a time, so one bad path
        doesn't abort the rest. Every path is safety-checked the same way
        as resolve_output_file() before anything is removed."""
        deleted, errors = [], []
        for rel in relative_paths:
            resolved = self.resolve_output_file(run_id, rel)
            if resolved is None:
                errors.append({"path": rel, "error": "not found or outside the job's output directory"})
                continue
            try:
                resolved.unlink()
                deleted.append(rel)
            except OSError as exc:
                errors.append({"path": rel, "error": str(exc)})
        return {"deleted": deleted, "errors": errors}
