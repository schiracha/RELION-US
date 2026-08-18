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

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_ABORTED = "aborted"

# Manual status overrides a user can apply via "Mark as finished"/"Mark as
# failed" (real RELION job actions, see gui_mainwindow.cpp's
# cb_mark_as_finished/cb_mark_as_failed) -- deliberately NOT the full status
# set: you can't manually force a job back to "running"/"pending", and
# "aborted" has its own dedicated action (abort_run) since it also has to
# actually stop the process.
MANUALLY_SETTABLE_STATUSES = {STATUS_COMPLETED, STATUS_FAILED}

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
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)
    # Runtime-only handles for abort_run(); never serialized (to_summary()
    # builds an explicit dict below and doesn't include them), so a
    # persisted-history run loaded back after a restart simply has these as
    # None -- correctly making it non-abortable, since the real process is
    # long gone anyway.
    proc: Any = field(default=None, repr=False, compare=False)
    task: Any = field(default=None, repr=False, compare=False)
    # One-off note emitted into the live output at start (currently: the
    # output directory was renumbered because the prospective jobNNN was
    # taken). Declared here rather than set ad hoc so both start paths and
    # the reader agree it exists.
    rewrite_note: Optional[str] = field(default=None, repr=False, compare=False)
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
            "abortable": self.status == STATUS_RUNNING and (self.proc is not None or self.task is not None),
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
        than started here. This app does not own those jobs and does not write
        RELION's pipeline state, so abort / overwrite / delete / status edits
        are refused on them."""
        return str(run_id).startswith(cls.RELION_RUN_PREFIX)

    def relion_run_detail(self, run_id: str, project_dir: Optional[Path] = None) -> Optional[dict]:
        """One imported RELION job, with the option values it actually ran
        with.

        RELION saves every JobOption into `job.star` in the job's own
        directory when the job runs -- the same file its GUI reads to reopen a
        job -- so reopening one here shows its real settings rather than the
        job type's defaults. Jobs from RELION 3.0 or earlier (a `run.job` in a
        different format) and directories that have since been deleted come
        back with empty values and a note saying so, which is the honest
        answer.
        """
        pd = project_dir if project_dir is not None else self.project_dir
        for entry in self._relion_pipeline_entries(pd):
            if entry["run_id"] != run_id:
                continue
            entry = dict(entry)
            job_dir = Path(entry["cwd"])
            values = project_manager.read_relion_job_options(job_dir)
            entry["field_values"] = values
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
        merged: dict[str, dict] = {}
        # Jobs RELION itself ran, from its own default_pipeline.star. First, so
        # anything this app also has a record of overrides them.
        for entry in self._relion_pipeline_entries(Path(target)):
            merged[entry["run_id"]] = entry
        for entry in project_manager.load_history(Path(target)):
            run_id = entry.get("run_id")
            if run_id:
                merged[run_id] = entry
        for run in self.runs.values():
            if run.project_dir == target:
                merged[run.run_id] = run.to_summary()
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

        These are **read-only**: `source: "relion"` marks them, and the API
        refuses abort/overwrite/delete on them. This app does not write
        RELION's pipeline state, so it must not offer actions that imply it
        owns these jobs. Reopening one still works -- the options come from the
        job's own `job.star` (see get_run_detail).

        Timestamps are left empty: RELION's pipeline file records none, and a
        job directory's mtime is when it was last touched, not when the job
        ran. A blank Started column is honest; a plausible-looking wrong
        timestamp is not.
        """
        info = project_manager.read_relion_pipeline(project_dir)
        out: list[dict] = []
        by_process_name: dict[str, dict] = {}
        for proc in info["processes"]:
            name = proc["name"]                        # e.g. "Class2D/job005"
            job_dir = project_dir / name
            internal = job_catalog.internal_name_for_label(proc["type_label"])
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
            entry = {
                "run_id": f"relion:{slug}",
                "source": "relion",
                "internal_name": internal or "",
                "display_name": display,
                "command": "",
                "status": project_manager.RELION_STATUS_MAP.get(
                    proc["status_label"], "completed"),
                "exit_code": None,
                "started_at": None,
                "ended_at": None,
                "project_dir": str(project_dir),
                "cwd": str(job_dir),
                "job_number": proc["job_number"],
                "alias": proc["alias"],
                "note": "",
                "job_name": name.split("/")[-1],
                "field_values": {},
                "detected_inputs": [],
                "abortable": False,
                "relion_type_label": proc["type_label"],
                "exists_on_disk": exists,
            }
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
        """Ask RELION to allocate and record this job. None on any failure.

        Failing back to this app's own numbering is deliberate: a job the user
        asked to run should still run when the pipeline is momentarily locked
        by an open RELION GUI, or when relion_pipeliner errors on a job type it
        does not recognise. The run then simply isn't in RELION's record, which
        is the behaviour without two-way sync at all, and the reason is put in
        the run's own output rather than swallowed.
        """
        meta = job_catalog.JOB_CATALOG.get(internal_name)
        if not meta:
            return None
        type_label = meta[0]
        try:
            import job_registry

            options_by_key = {
                o["key"]: o for o in job_registry.raw_job(internal_name).get("options", [])
            }
        except Exception:
            options_by_key = {}
        try:
            return pipeline_bridge.register_job(
                project_dir, type_label, field_values or {}, options_by_key)
        except pipeline_bridge.PipelineBridgeError as exc:
            self._pipeline_sync_error = str(exc)
            return None

    def sync_completion_to_relion(self, project_dir: Optional[Path] = None) -> bool:
        """Let RELION notice that a job finished (it reads the exit files the
        `--pipeline_control` flag makes the program write)."""
        pd = Path(project_dir) if project_dir is not None else self.project_dir
        if not self.pipeline_sync_enabled(pd):
            return False
        return pipeline_bridge.check_job_completion(pd)

    def _next_job_number(self, project_dir: Path, internal_name: Optional[str] = None) -> int:
        """RELION's own job numbering is a single counter for the whole
        project, shared across every job type (see job_catalog.py's
        JOB_DIRNAME docstring) -- derived fresh each time from persisted +
        in-memory state rather than kept as separate mutable counter state,
        so it can't drift out of sync across a backend restart.

        **RELION's own numbering counts too.** Opening a project that was built
        in RELION's GUI, this app used to start again at job001 -- and job001
        in such a project is somebody's existing results. `rlnPipeLineJobCounter`
        and the per-process numbers in `default_pipeline.star` are read so the
        numbering continues the project instead of colliding with it.

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

    def _resolve_overwrite_target(self, overwrite_run_id: str, project_dir: Path) -> dict:
        """Looks up the run being overwritten, whether it's still live in
        self.runs (this session) or only survives in persisted history (a
        previous session) -- Overwrite should work either way, the same as
        delete_run()/file operations above. Raises ValueError (caller turns
        this into a 409) if the run can't be found, is still running, or
        belongs to a different/inactive project."""
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
            if entry is None:
                raise ValueError(f"Unknown run_id to overwrite: {overwrite_run_id}")
            info = {
                "status": entry.get("status"), "project_dir": entry.get("project_dir"),
                "cwd": entry.get("cwd"), "job_number": entry.get("job_number", 0),
                "alias": entry.get("alias", ""), "note": entry.get("note", ""),
            }
        if info["status"] == STATUS_RUNNING:
            raise ValueError("Cannot overwrite a job that is still running")
        if info["project_dir"] != str(project_dir):
            raise ValueError("Cannot overwrite a job from a different/inactive project")
        if not info["cwd"]:
            raise ValueError(f"Run {overwrite_run_id} has no recorded output directory to overwrite")
        return info

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
        """
        project_dir = self.project_dir
        rewrite_note = None

        if overwrite_run_id is not None:
            target = self._resolve_overwrite_target(overwrite_run_id, project_dir)
            run_id = overwrite_run_id
            cwd = Path(target["cwd"])
            job_number = target["job_number"]
            alias, note = target["alias"], target["note"]
        else:
            run_id = self.new_run_id()
            registered = None
            if self.pipeline_sync_enabled(project_dir):
                # Two-way mode: RELION's own pipeliner allocates the job number,
                # creates the directory and records the process (with its node
                # graph) in default_pipeline.star. Whatever slot it gives back is
                # authoritative from here on -- guessing our own number while the
                # pipeline is also allocating them is how the two records drift.
                registered = self._register_in_relion_pipeline(
                    project_dir, internal_name, field_values or {})
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
                pipeline_note = (
                    f"[RELION-US] Registered in RELION's pipeline as "
                    f"{authoritative_subdir}/ — it will appear in RELION's own GUI."
                )
                rewrite_note = (rewrite_note + "\n" + pipeline_note) if rewrite_note else pipeline_note
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
        )
        run.rewrite_note = rewrite_note
        self.runs[run_id] = run
        self._persist(run)
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

        # If start_subprocess_job had to advance the output directory to
        # avoid a job-number collision, surface that one adjustment in the
        # live output before anything else.
        if run.rewrite_note:
            run.stdout_lines.append(run.rewrite_note)
            await run.broadcast({"type": "stdout", "line": run.rewrite_note})

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
        if run.abort_requested:
            # Abort arrived while this process was still being spawned.
            self._terminate_process_group(run)

        async def pump(stream, sink: list[str], msg_type: str):
            while True:
                try:
                    line = await stream.readline()
                except ValueError as exc:
                    # StreamReader raises on a single line longer than its
                    # 64 KiB limit -- real for tools that emit one huge line.
                    # Report it and stop pumping rather than letting it
                    # escape and strand the run in "running" forever.
                    msg = f"[RELION-US] output stream error: {exc}"
                    run.stderr_lines.append(msg)
                    await run.broadcast({"type": "stderr", "line": msg})
                    break
                if not line:
                    break
                decoded = line.decode(errors="replace").rstrip("\n")
                sink.append(decoded)
                await run.broadcast({"type": msg_type, "line": decoded})

        # try/finally so the run ALWAYS reaches a terminal status and is
        # persisted -- otherwise an unexpected error here leaves the Command
        # Center showing a job that runs forever, with an unreaped child.
        # (_run_custom already had this; the two paths now match.)
        exit_code = None
        try:
            await asyncio.gather(
                pump(proc.stdout, run.stdout_lines, "stdout"),
                pump(proc.stderr, run.stderr_lines, "stderr"),
            )
            exit_code = await proc.wait()
        except Exception as exc:  # noqa: BLE001
            msg = f"[RELION-US] error while streaming output: {type(exc).__name__}: {exc}"
            run.stderr_lines.append(msg)
            await run.broadcast({"type": "stderr", "line": msg})
        finally:
            if exit_code is None:
                # never got a clean wait() -- make sure the child is reaped
                try:
                    exit_code = await proc.wait()
                except Exception:  # noqa: BLE001
                    exit_code = proc.returncode
            run.exit_code = exit_code
            # abort_run() may already have set this to STATUS_ABORTED (and
            # requested termination) -- don't let the process's exit code
            # (non-zero after a SIGTERM) overwrite that with STATUS_FAILED.
            if run.status != STATUS_ABORTED:
                run.status = STATUS_COMPLETED if exit_code == 0 else STATUS_FAILED
            run.ended_at = time.time()
            run.proc = None
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

    async def start_custom_job(
        self,
        internal_name: str,
        display_name: str,
        runner_coro_factory,
        field_values: Optional[dict] = None,
        overwrite_run_id: Optional[str] = None,
    ) -> JobRun:
        """
        runner_coro_factory: a callable taking the job's own output directory
        and returning a coroutine that does the actual work and returns a
        human-readable summary string (or raises). Used by custom_jobs.py for
        the IMOD/Warp/DeepETPicker/AreTomo2 bridges, which call converters/
        directly instead of spawning a subprocess. It receives the job dir so
        its outputs land in `<JobDir>/jobNNN/` -- the same directory the
        Outputs tab, Clean and Delete operate on -- rather than the project
        root, which would leave the tracked job dir empty and let successive
        runs silently overwrite each other's results.

        overwrite_run_id: same "Overwrite" semantics as
        start_subprocess_job() -- reuses the original run's run_id/cwd/
        job_number/alias/note (same job slot) instead of allocating new
        ones. Same restrictions (must exist, not still running, same
        project) apply.
        """
        project_dir = self.project_dir

        if overwrite_run_id is not None:
            target = self._resolve_overwrite_target(overwrite_run_id, project_dir)
            run_id = overwrite_run_id
            cwd = Path(target["cwd"])
            job_number = target["job_number"]
            alias, note = target["alias"], target["note"]
        else:
            run_id = self.new_run_id()
            job_number = self._next_job_number(project_dir, internal_name)
            cwd = project_dir / f"{job_catalog.job_dirname(internal_name)}/job{job_number:03d}"
            alias, note = "", ""
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
            detected_inputs=_detect_inputs(detect_text, project_dir, cwd),
        )
        self.runs[run_id] = run
        self._persist(run)
        run.task = asyncio.create_task(self._run_custom(run, runner_coro_factory, cwd))
        return run

    async def _run_custom(self, run: JobRun, runner_coro_factory, job_dir: Path) -> None:
        if run.abort_requested:
            return  # aborted before this task got scheduled; see _run_subprocess
        run.status = STATUS_RUNNING
        run.started_at = time.time()
        self._persist(run)
        await run.broadcast({"type": "status", "status": run.status})
        try:
            result = await runner_coro_factory(job_dir)
            for line in str(result).splitlines() or ["(no output)"]:
                run.stdout_lines.append(line)
                await run.broadcast({"type": "stdout", "line": line})
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
            run.ended_at = time.time()
            self._persist(run)
            await run.broadcast({"type": "status", "status": run.status, "exit_code": run.exit_code})

    # --- Command Center job actions -----------------------------------
    # Real RELION job actions this mirrors (see gui_mainwindow.cpp's "Job
    # actions" menu, ~line 703): Alias, Overwrite (see
    # start_subprocess_job's overwrite_run_id), Abort running, Mark as
    # finished, Mark as failed, Delete. "Edit Note" is real RELION too
    # (a free-text annotation per job) -- exposed here as set_note().

    @staticmethod
    def _terminate_process_group(run: JobRun) -> None:
        """Signal the whole process group (see start_new_session=True in
        _run_subprocess), not just the /bin/sh wrapper -- a plain terminate()
        only reaches the shell itself and can leave its actual child (the real
        relion_* command) running orphaned. Falls back to terminate() if the
        process somehow isn't its own group leader (shouldn't happen given
        start_new_session=True, but a crashed/reaped process could make
        getpgid raise)."""
        if run.proc is None:
            return
        try:
            os.killpg(os.getpgid(run.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                run.proc.terminate()
            except ProcessLookupError:
                pass

    async def abort_run(self, run_id: str) -> bool:
        """Real RELION 'Abort running' (gui_mainwindow.cpp's cb_abort ->
        pipeline's kill-the-running-process path). Returns False if the run
        doesn't exist or isn't currently running. Sets status to ABORTED
        immediately (optimistic -- see _run_subprocess's guard against this
        being overwritten by the process's own non-zero exit code from the
        termination signal) before actually asking the process/task to
        stop, so the UI reflects the abort right away rather than racing
        the process's own shutdown."""
        run = self.get(run_id)
        # PENDING counts: there is a real window between start_*_job() creating
        # the run and its task setting status to RUNNING, and a fast click
        # landing in that window used to return False while the job carried on
        # running. The abort_requested flag below covers the process handle not
        # existing yet.
        if run is None or run.status not in (STATUS_PENDING, STATUS_RUNNING):
            return False
        run.status = STATUS_ABORTED
        run.ended_at = time.time()
        self._persist(run)
        await run.broadcast({"type": "stderr", "line": "Aborted by user."})
        await run.broadcast({"type": "status", "status": run.status})
        if run.proc is not None:
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

    def _update_persisted_only(self, run_id: str, **fields: Any) -> Optional[dict]:
        """Fallback for set_alias()/set_note() when a run only survives in
        persisted history (a previous backend session -- self.runs is
        in-memory and empty again after every restart). Renaming/annotating
        an old job shouldn't require it still being in this session's
        memory, unlike abort (nothing real left to stop) or Mark as
        finished/failed (nothing to override once it's already final and
        persisted honestly)."""
        history = project_manager.load_history(self.project_dir)
        entry = next((h for h in history if h.get("run_id") == run_id), None)
        if entry is None:
            return None
        entry.update(fields)
        project_manager.save_history(self.project_dir, history)
        return entry

    def set_alias(self, run_id: str, alias: str) -> Optional[dict]:
        """Real RELION 'Alias' job action (gui_mainwindow.cpp's
        cb_set_alias). An empty string clears the alias, reverting display
        to the plain job number."""
        alias = alias.strip()
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
        """Real RELION 'Edit Note' job action."""
        run = self.get(run_id)
        if run is None:
            return self._update_persisted_only(run_id, note=note)
        run.note = note
        self._persist(run)
        return run.to_summary()

    def set_status(self, run_id: str, status: str) -> Optional[dict]:
        """Real RELION 'Mark as finished' / 'Mark as failed' job actions
        (gui_mainwindow.cpp's cb_mark_as_finished/cb_mark_as_failed) — a
        manual override for when a run's tracked status doesn't match what
        actually happened (e.g. the backend was restarted mid-run and the
        process kept going/died outside this app's view). Restricted to
        MANUALLY_SETTABLE_STATUSES; raises ValueError otherwise so the API
        layer can turn that into a clean 400 rather than silently no-op'ing
        or allowing a nonsensical manual "running"/"pending" override."""
        if status not in MANUALLY_SETTABLE_STATUSES:
            raise ValueError(f"status must be one of {sorted(MANUALLY_SETTABLE_STATUSES)}")
        run = self.get(run_id)
        if run is None:
            return self._update_persisted_only(run_id, status=status)
        run.status = status
        if run.ended_at is None:
            run.ended_at = time.time()
        self._persist(run)
        return run.to_summary()

    def delete_run(self, run_id: str, remove_files: bool) -> tuple[bool, str]:
        """Real RELION 'Delete' job action. Always removes the run from
        this app's tracked history (in-memory + persisted). If
        remove_files, also removes the run's own output directory --
        always safe to do unconditionally here (unlike Clean/Harsh Clean,
        see cleanup_candidates() below) because that directory is one
        RELION-US itself created exclusively for this run; nothing else
        can be living in it. Refuses (returns False, reason) rather than
        deleting anything outside that directory, or a still-running job's
        directory out from under it.

        Works the same whether this run is still live in self.runs (this
        session) or only survives in persisted history (a run from a
        previous backend session -- self.runs is in-memory and empty again
        after every restart) -- cwd/project_dir/status are read from
        whichever source has them, since both need to support "delete this
        old job's output directory," not just ones started this session."""
        run = self.get(run_id)
        if run is not None:
            status, cwd, project_dir = run.status, run.cwd, run.project_dir
        else:
            history = project_manager.load_history(self.project_dir)
            entry = next((h for h in history if h.get("run_id") == run_id), None)
            if entry is None:
                return False, "Unknown run_id"
            status, cwd, project_dir = entry.get("status"), entry.get("cwd"), entry.get("project_dir")

        if status == STATUS_RUNNING:
            return False, "Cannot delete a job that is still running -- abort it first"

        if remove_files and cwd and project_dir:
            ok, reason = self._safe_rmtree(cwd, project_dir)
            if not ok:
                return False, reason

        self.runs.pop(run_id, None)
        target_dir = Path(project_dir) if project_dir else self.project_dir
        history = project_manager.load_history(target_dir)
        project_manager.save_history(
            target_dir, [h for h in history if h.get("run_id") != run_id]
        )
        return True, "Deleted"

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
