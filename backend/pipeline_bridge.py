"""
pipeline_bridge.py — registering RELION-US's jobs in RELION's own pipeline, so
the two GUIs can be used on the same project interchangeably.

**Almost nothing here writes `default_pipeline.star` directly.** Every change
to it goes through RELION's own `relion_pipeliner` binary, with ONE narrow,
deliberate exception (set_process_status, below). That is not squeamishness —
the file is five linked tables (general, processes, nodes, input edges,
output edges), the node graph is computed by each job's own
`getCommands<Job>Job()` C++, and access is guarded by a `.relion_lock/` mutex
directory that RELION's GUI holds while it is open. Reimplementing any of
that in Python would be a new source of truth for a file this app is only a
guest in, and the failure mode is a corrupted project rather than an error
message.

The relevant entry point, from `src/apps/pipeliner.cpp`:

    relion_pipeliner --addJobFromStar <job.star>

which reads the job type and options from a `job.star`, then calls
`PipeLine::addScheduledJob` -> `runJob(..., only_schedule=true, ...)`. RELION
therefore does all of this itself:

  * allocates the job number and creates `<JobDir>/jobNNN/`,
  * runs the job's real command builder, so `inputNodes`/`outputNodes` — and
    hence the pipeline's node and edge tables — are exactly what RELION would
    have recorded,
  * writes `job.star` into the job directory,
  * adds the process with status `Scheduled`,
  * takes and releases the `.relion_lock` mutex around the read/write.

`only_schedule=true` means it registers the job **without running it**, which is
precisely the division of labour we want: RELION owns the bookkeeping, RELION-US
runs the command the user approved.

**Completion used to be documented as "RELION's mechanism too" here — that
turned out to be untested and wrong.** `prepareFinalCommand` appends
`--pipeline_control <jobdir>/` to every `relion_` command; the program writes
`RELION_JOB_EXIT_SUCCESS` / `_FAILURE` / `_ABORTED` into that directory when
it ends (`src/pipeline_control.h`), and

    relion_pipeliner --check_job_completion

reads those files back -- but only for a process whose STATUS IS ALREADY
"Running" (`PipeLine::checkProcessCompletion`, `src/pipeliner.cpp`: "Only
check running processes for file existence"). Confirmed live against the
real 5.0.1 binary: registering a job (always "Scheduled" -- there is no
`--addJobFromStar` variant that adds it as "Running") and then touching its
exit-success file does NOT flip its status; `--check_job_completion` silently
ignores it. And there is no other CLI path to "Running" that doesn't mean
actually re-executing the job's real command (`--RunJobs` -- wrong for
RELION-US, which already ran the real work itself; for the in-browser
picking jobs specifically, that "real command" is a desktop GUI that can't
run headlessly, which is the whole reason they exist). So every job
RELION-US ever registered was permanently stuck showing "Scheduled" in
RELION's own GUI, regardless of whether it had actually finished.

`set_process_status()` is the fix: a narrow, surgical exception that writes
directly to the `pipeline_processes` block's status column for one process,
under the SAME `.relion_lock` mutex `relion_pipeliner` itself takes. It only
ever changes one whitespace-delimited token on one line -- never node/edge
tables, never anything `relion_pipeliner`'s own command-building or graph
logic is the authority for -- so this cannot desync the pipeline the way
reimplementing registration or node computation would. Called once to mark
"Running" right when real work starts (job_runner.py), after which
`--check_job_completion` (still called exactly as before) can actually do
its job and promote Running -> Succeeded/Failed/Aborted through RELION's own
route, or in one flow (picking jobs, which stay "Running" indefinitely) is
called directly to set the terminal status when the user clicks Done. This
is safe against RELION-US's own concurrent operations (the lock), but NOT a
substitute for closing any native RELION GUI that already has the project
open -- a live GUI process holds its own in-memory copy of the pipeline this
write can't coordinate with; see the app's startup warning.

Deleting a job does NOT call into this module at all, for the same reason:
`relion_pipeliner`'s CLI (src/apps/pipeliner.cpp) has no verb for removing
one process from `default_pipeline.star`. The real operation --
`PipeLine::deleteNodesAndProcesses`, src/pipeliner.cpp -- is called only
from `gui_mainwindow.cpp`, and unlike set_process_status's one-token edit
it rewrites all five linked tables at once (a `write(...)` overload that
filters processes/nodes/edges together), which is exactly the multi-table
graph surgery this module exists to avoid reimplementing. So a
pipeline-synced job's process entry is left untouched in
default_pipeline.star when RELION-US deletes its own tracked copy;
job_runner.delete_run/restore_from_trash instead maintain a small local
hide-list (project_manager.load_relion_deleted_job_numbers) so the
now-orphaned entry doesn't reappear as a ghost row in the Command Center
-- see that function's docstring for why keying it by RELION's own
job_number is safe against number reuse. Nothing about this writes to
default_pipeline.star; it only changes what RELION-US chooses to display.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import project_manager

PIPELINER_BINARY = "relion_pipeliner"
# PipeLine::read/write take this directory as a mutex (mkdir is atomic), and
# hold it for the duration of an update. RELION waits 3s a try, 20 tries.
LOCK_DIRNAME = ".relion_lock"
# Long enough for RELION's own lock wait (~60 s) plus the command's own work.
PIPELINER_TIMEOUT_SECONDS = 120

# RELION appends this to every command containing "relion_" (see
# RelionJob::prepareFinalCommand); it is what makes a program write the exit
# files --check_job_completion looks for. The Python tomo tools take the
# hyphenated spelling.
PIPELINE_CONTROL_FLAG = "--pipeline_control"
PIPELINE_CONTROL_FLAG_PYTHON = "--pipeline-control"


class PipelineBridgeError(Exception):
    """relion_pipeliner is missing, or refused to do what was asked."""


# --------------------------------------------------------------------------
# Bootstrapping a brand-new project's default_pipeline.star
# --------------------------------------------------------------------------
#
# relion_pipeliner (the CLI binary) cannot create this file from nothing:
# every single code path in src/apps/pipeliner.cpp does `pipeline.read(
# DO_LOCK); pipeline.write(DO_LOCK);` unconditionally, and PipeLine::read()
# takes the .relion_lock directory FIRST, then REPORT_ERRORs ("File
# default_pipeline.star cannot be read") the moment it tries to open a file
# that isn't there yet -- which exits the process while still holding the
# lock (REPORT_ERROR prints and calls exit(), bypassing whatever would
# normally remove .relion_lock). Confirmed for real: enabling pipeline sync
# on a project that had never been opened in RELION's own GUI orphaned
# .relion_lock on its first job, permanently blocking every later sync
# attempt (relion_pipeliner's own retry-and-give-up warns "Perhaps the GUI
# or one of RELION's programs crashed unexpectedly?", misdiagnosing its own
# CLI limitation as user error) -- until a human manually removed it.
#
# RELION's own native GUI sidesteps this by never calling read() on a
# missing file to begin with (src/gui_mainwindow.cpp:349-361):
#
#   pipeline.name = fn_pipe;
#   if (exists(pipeline.name + "_pipeline.star"))
#   {
#       pipeline.read(DO_LOCK, lock_message);
#       pipeline.write(DO_LOCK);
#   }
#   else
#   {
#       pipeline.write();   // <-- bootstraps a fresh, empty pipeline
#   }
#
# PipeLine::write() on a freshly-constructed PipeLine (job_counter=0, no
# processes/nodes/edges) always produces the exact same fixed, empty
# skeleton -- no node-graph computation involved, unlike everything else
# this module deliberately leaves to the real binary. Reproducing that one
# static skeleton here is doing what the native GUI does on first launch,
# not reimplementing pipeline logic: every byte written after this one-time
# bootstrap still goes through real relion_pipeliner, exactly as before.
#
# Just the pipeline_general block, NOT all five tables: MetaDataTable::write
# (src/metadata_table.cpp:1369-1372) skips a table entirely -- no data_
# header, no loop_, nothing -- whenever it has zero rows ("Only write
# tables that have something in them"). A first attempt at this skeleton
# that spelled out all five tables (including four empty `loop_` blocks)
# was WRONG and crashed relion_pipeliner's reader: label-parsing
# (metadata_table.cpp:1045-1076) skips blank lines while still hunting for
# more `_label #N` declarations, so an empty loop's very next non-blank
# line -- the following block's `data_` header -- gets consumed as a bogus
# first data row ("fewer columns than the number of labels"), which is
# exactly what orphaned the lock in the first place. Confirmed for real:
# this minimal general-only version round-trips cleanly through
# `relion_pipeliner --check_job_completion` on a from-scratch project
# (exit 0, no lock left behind, and relion_pipeliner's own rewrite of the
# file afterward is byte-identical in shape to this skeleton).
_EMPTY_PIPELINE_SKELETON = """
# version 50001

data_pipeline_general

_rlnPipeLineJobCounter                      0

"""


def _ensure_pipeline_bootstrapped(project_dir: Path) -> None:
    """Write an empty default_pipeline.star if this project doesn't have one
    yet -- see _EMPTY_PIPELINE_SKELETON. A no-op (checked first, so no
    write racing a concurrent RELION process) once the file exists; every
    later change to it still goes exclusively through relion_pipeliner."""
    star_path = Path(project_dir) / "default_pipeline.star"
    if star_path.exists():
        return
    try:
        # Exclusive create: if another process wins the race, its file (also
        # this exact skeleton, or a real one from a job it just registered)
        # stands and this one backs off rather than clobbering it.
        with open(star_path, "x", encoding="utf-8") as f:
            f.write(_EMPTY_PIPELINE_SKELETON)
    except FileExistsError:
        pass


def pipeliner_path() -> Optional[str]:
    return shutil.which(PIPELINER_BINARY)


def is_available() -> bool:
    """Whether two-way sync is possible at all on this machine."""
    return pipeliner_path() is not None


def is_locked(project_dir: Path) -> bool:
    """True while some RELION process holds the pipeline mutex.

    Not an error on its own — RELION's own tools wait for it — but worth
    surfacing, because a stale `.relion_lock/` left by a crashed GUI makes every
    pipeline operation hang for a minute and then fail.
    """
    return (Path(project_dir) / LOCK_DIRNAME).is_dir()


# --------------------------------------------------------------------------
# The one direct write -- see module docstring for why this exists and what
# it deliberately does NOT touch.
# --------------------------------------------------------------------------

# The five real RELION status labels (pipeline_jobs.h's procstatus_type2label)
# -- kept here rather than re-deriving from project_manager.RELION_STATUS_MAP,
# which maps the OTHER direction (RELION's labels -> this app's own status
# vocabulary) and is lossy for that purpose (several RELION labels can map to
# one app status).
RELION_STATUS_LABELS = frozenset({"Running", "Scheduled", "Succeeded", "Failed", "Aborted"})

# RELION waits 3s a try, 20 tries (~60s) before giving up on the lock -- see
# LOCK_DIRNAME's own comment. Matched here so a concurrent relion_pipeliner
# invocation (this app's own registration/completion-check calls) gets the
# same grace period rather than losing a race to an artificially short wait.
_LOCK_TIMEOUT_SECONDS = 60.0
_LOCK_POLL_SECONDS = 0.2


def _acquire_pipeline_lock(project_dir: Path, timeout: Optional[float] = None) -> None:
    """mkdir is atomic -- the same mutex idiom PipeLine::read/write use
    against LOCK_DIRNAME. Raises PipelineBridgeError on timeout, the same
    way a real relion_pipeliner invocation eventually gives up and errors
    (see _run_pipeliner's own timeout).

    timeout defaults to the module-level _LOCK_TIMEOUT_SECONDS, looked up
    here (at call time) rather than as this parameter's own default value,
    so a test monkeypatching that module attribute actually takes effect --
    a default argument's value is bound once, at function-definition time,
    and would otherwise ignore the patch entirely.
    """
    if timeout is None:
        timeout = _LOCK_TIMEOUT_SECONDS
    lock_dir = Path(project_dir) / LOCK_DIRNAME
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock_dir.mkdir()
            return
        except FileExistsError:
            if time.monotonic() > deadline:
                raise PipelineBridgeError(
                    f"Could not acquire the pipeline lock ({lock_dir}) within "
                    f"{timeout:.0f}s -- another process (this app, or a RELION "
                    f"GUI) is using the pipeline right now. If a RELION GUI "
                    f"crashed, a stale {LOCK_DIRNAME}/ directory may need to "
                    f"be removed by hand."
                )
            time.sleep(_LOCK_POLL_SECONDS)


def _release_pipeline_lock(project_dir: Path) -> None:
    try:
        (Path(project_dir) / LOCK_DIRNAME).rmdir()
    except OSError:
        pass


def _rewrite_process_status(text: str, process_name: str, status_label: str) -> tuple[str, bool]:
    """Replace the status column of ONE row in the `pipeline_processes`
    loop_ block, byte-for-byte everywhere else -- see module docstring for
    why this is a text-level edit rather than a full STAR parse/rewrite
    (smaller, more predictable blast radius: this cannot touch a table it
    doesn't understand, because it never looks at one).

    A `pipeline_processes` data row is exactly 4 whitespace-separated
    tokens (name, alias, type label, status -- confirmed against real
    relion_pipeliner output); every other block's rows have a different
    column count, so scoping to `data_pipeline_processes` specifically
    (rather than just "4 tokens, ends in a known status word") is
    extra insurance, not the only thing preventing a wrong-block match.
    """
    target = process_name.rstrip("/")
    lines = text.split("\n")
    in_processes_block = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("data_"):
            in_processes_block = stripped == "data_pipeline_processes"
            continue
        if not in_processes_block or not stripped or stripped.startswith(("_", "loop_", "#")):
            continue
        parts = stripped.split()
        if len(parts) != 4 or parts[0].rstrip("/") != target:
            continue
        old_status = parts[3]
        idx = line.rfind(old_status)
        lines[i] = line[:idx] + status_label + line[idx + len(old_status):]
        return "\n".join(lines), True
    return text, False


def set_process_status(project_dir: Path, process_name: str, status_label: str) -> bool:
    """Directly set one process's status in RELION's own
    default_pipeline.star -- see module docstring for why this exists (in
    short: relion_pipeliner's CLI has no way to mark a job "Running" short
    of actually re-executing its real command, and --check_job_completion
    only ever promotes a process already marked "Running").

    process_name: RELION's own process name, project-relative, e.g.
    "MotionCorr/job007" (trailing slash optional -- normalized either way).
    status_label: one of RELION_STATUS_LABELS.

    Returns False (not an error) if default_pipeline.star doesn't exist or
    the process isn't found in it -- nothing to update, not a failure this
    app should block a real job over. Raises PipelineBridgeError if the
    lock can't be acquired (see _acquire_pipeline_lock) or the file can't be
    read/written.
    """
    if status_label not in RELION_STATUS_LABELS:
        raise ValueError(f"status_label must be one of {sorted(RELION_STATUS_LABELS)}")
    star_path = Path(project_dir) / "default_pipeline.star"
    if not star_path.exists():
        return False
    _acquire_pipeline_lock(project_dir)
    try:
        try:
            text = star_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PipelineBridgeError(f"could not read {star_path}: {exc}") from exc
        new_text, changed = _rewrite_process_status(text, process_name, status_label)
        if changed:
            try:
                star_path.write_text(new_text, encoding="utf-8")
            except OSError as exc:
                raise PipelineBridgeError(f"could not write {star_path}: {exc}") from exc
        return changed
    finally:
        _release_pipeline_lock(project_dir)


# --------------------------------------------------------------------------
# job.star
# --------------------------------------------------------------------------


def _job_option_value(value: Any, option: Optional[dict]) -> str:
    """One field's value in RELION's own job.star spelling.

    Booleans are the only real translation: RELION stores them as the strings
    "Yes"/"No" and `JobOption::getBoolean()` literally tests `value == "Yes"`
    (src/pipeline_jobs.cpp), so writing True/true/1 silently reads back as
    false — the option would appear set in the file and be off in the job.
    """
    field_type = (option or {}).get("field_type")
    if field_type == "boolean" or isinstance(value, bool):
        truthy = value if isinstance(value, bool) else str(value).strip().lower() in (
            "yes", "true", "1")
        return "Yes" if truthy else "No"
    if value is None:
        return ""
    return str(value)


def _is_tomo_job(values: dict[str, Any]) -> bool:
    """Whether this job instance is using RELION's tomo optimisation-set
    input convention (RelionJob::addTomoInputOptions/getTomoInputCommmand,
    src/pipeline_jobs.cpp) -- the shared input block Class3D/Inimodel/
    Autorefine/MultiBody all use in RELION's own tomo GUI mode: an
    `in_optimisation` STAR file, or (if "OR: use direct entries?" is
    ticked) the direct in_particles/in_tomograms/in_trajectories fields
    instead. Motioncorr/Ctffind take a third route, below.

    Why this matters here: RelionJob's own is_tomo is normally fixed at GUI
    *launch* time (`relion` vs `relion --tomo`) and RELION-US has no
    equivalent GUI-mode concept -- but the _rlnJobIsTomo this function feeds
    into is read back by RELION's own pipeliner to decide which of TWO
    ENTIRELY DIFFERENT validation/command-building code paths a job class
    uses (confirmed via RelionJob::getCommandsAutorefineJob -- the tomo
    branch checks in_optimisation; the non-tomo branch checks fn_img, and
    rejects the job with "empty field for input STAR file" if that's blank,
    which it always is for a job actually using in_optimisation). Registering
    a real tomo job with is_tomo hardcoded to 0 means the pipeliner validates
    it against the wrong job class's rules and rejects it outright --
    confirmed live against a real 3D Auto-refine (tomo) job. Inferring
    is_tomo from whether these fields are actually populated is the closest
    equivalent RELION-US has to "which GUI mode created this job," for the
    Class3D/Inimodel/Autorefine/MultiBody family.

    Motioncorr/Ctffind have no in_optimisation-style field at all -- real
    RELION has exactly one RelionJob class for each, with is_tomo a runtime
    flag inside it rather than a field a user fills in (see
    initialiseMotioncorrJob/initialiseCtffindJob's own is_tomo-conditioned
    JobOption sets). RELION-US gives each its own menu entry instead of a
    same-popup toggle (job_catalog.TOMO_VARIANT_OF: TomoMotioncorr/
    TomoCtffind share their SPA sibling's label_new, since it's genuinely
    the same real job type either way) -- job_runner._register_in_relion_
    pipeline sets field_values["is_tomo"] from which menu entry was picked
    before calling register_job, which is what write_job_star below reads.
    The pipeliner's output-node bookkeeping for these two (corrected_tilt_
    series.star + LABEL_MOCORR_TOMOGRAMS vs corrected_micrographs.star +
    LABEL_MOCORR_MICS, and similarly for Ctffind's tilt_series_ctf.star)
    depends on the same _rlnJobIsTomo flag, so it needs the same real
    answer. job_registry.py's draft command builder derives is_tomo the same
    way, from internal_name, for the identical reason -- see
    _build_draft_command and _evaluate_condition.
    """
    return (
        bool(values.get("in_optimisation"))
        or bool(values.get("use_direct_entries"))
        or bool(values.get("is_tomo"))
    )


def write_job_star(
    path: Path,
    type_label: str,
    values: dict[str, Any],
    options_by_key: Optional[dict[str, dict]] = None,
) -> Path:
    """Write a `job.star` in RELION's own format (RelionJob::write).

    Two blocks: `job` (type label, is_continue, is_tomo -- see
    _is_tomo_job for how the latter is determined) and `joboptions_values`
    (rlnJobOptionVariable / rlnJobOptionValue). Values are quoted, since
    RELION's STAR reader is whitespace-separated and paths, additional
    arguments and help-ish strings all contain spaces.
    """
    options_by_key = options_by_key or {}
    is_tomo_flag = "1" if _is_tomo_job(values) else "0"
    lines = [
        "",
        "# version 30001",
        "",
        "data_job",
        "",
        f"_rlnJobTypeLabel                     {type_label}",
        "_rlnJobIsContinue                             0",
        f"_rlnJobIsTomo                                 {is_tomo_flag}",
        "",
        "",
        "# version 30001",
        "",
        "data_joboptions_values",
        "",
        "loop_",
        "_rlnJobOptionVariable #1",
        "_rlnJobOptionValue #2",
    ]
    for key, raw in values.items():
        text = _job_option_value(raw, options_by_key.get(key))
        lines.append(f'{key} "{text}"')
    lines.append("")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Driving relion_pipeliner
# --------------------------------------------------------------------------


def _run_pipeliner(project_dir: Path, args: list[str]) -> subprocess.CompletedProcess:
    exe = pipeliner_path()
    if exe is None:
        raise PipelineBridgeError(
            f"{PIPELINER_BINARY} is not on this machine's PATH. It is part of a "
            "normal RELION install, and RELION-US needs it to record jobs in "
            "RELION's own pipeline."
        )
    _ensure_pipeline_bootstrapped(project_dir)
    try:
        return subprocess.run(
            [exe, *args],
            cwd=str(project_dir),          # the pipeline is per project directory
            capture_output=True,
            text=True,
            timeout=PIPELINER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise PipelineBridgeError(
            f"{PIPELINER_BINARY} did not finish within {PIPELINER_TIMEOUT_SECONDS}s. "
            f"If a RELION GUI crashed, a stale {LOCK_DIRNAME}/ directory in the "
            "project may be holding the pipeline lock."
        )
    except OSError as exc:
        raise PipelineBridgeError(f"could not run {PIPELINER_BINARY}: {exc}")


def register_job(
    project_dir: Path,
    type_label: str,
    values: dict[str, Any],
    options_by_key: Optional[dict[str, dict]] = None,
    alias: str = "",
) -> dict[str, Any]:
    """Register a job in RELION's pipeline and return the slot RELION gave it.

    Returns {"process_name": "Class2D/job012", "job_number": 12}. RELION
    allocates the number and creates the directory, so the caller must use what
    comes back rather than what it guessed — the pipeline is the authority the
    moment it is in play.

    The new process is found by diffing the pipeline before and after rather
    than by parsing stdout: `relion_pipeliner` prints nothing reliable, and the
    diff is true regardless of RELION version.
    """
    project_dir = Path(project_dir)
    before = {p["name"] for p in project_manager.read_relion_pipeline(project_dir)["processes"]}

    with tempfile.TemporaryDirectory(dir=str(project_dir)) as tmp:
        star = write_job_star(Path(tmp) / "job.star", type_label, values, options_by_key)
        args = ["--addJobFromStar", str(star)]
        if alias:
            # RELION only accepts an alias alongside an add -- there is no
            # standalone alias command in relion_pipeliner.
            args += ["--setJobAlias", alias]
        proc = _run_pipeliner(project_dir, args)

    after_info = project_manager.read_relion_pipeline(project_dir)
    new = [p for p in after_info["processes"] if p["name"] not in before]

    if not new:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise PipelineBridgeError(
            "RELION's pipeliner did not add the job to the pipeline"
            + (f": {detail.splitlines()[-1]}" if detail else ".")
        )
    if len(new) > 1:
        # Someone else added a job at the same moment. Take the highest number,
        # which is the one RELION just allocated to us.
        new.sort(key=lambda p: p["job_number"])
    entry = new[-1]
    return {
        "process_name": entry["name"],
        "job_number": entry["job_number"],
        "stdout": (proc.stdout or "").strip(),
    }


def check_job_completion(project_dir: Path) -> bool:
    """Ask RELION to update the status of any process whose job has ended.

    Best-effort by design: the job itself has already finished either way, and
    the Command Center's own record is unaffected. Returns False rather than
    raising so a failure to sync can be reported without turning a completed
    job into an error.
    """
    try:
        proc = _run_pipeliner(project_dir, ["--check_job_completion"])
    except PipelineBridgeError:
        return False
    return proc.returncode == 0


def pipeline_control_args(command: str, job_subdir: str) -> str:
    """`--pipeline_control <jobdir>/`, appended the way RELION appends it.

    RELION adds this to every command containing "relion_" (and only those) in
    `RelionJob::prepareFinalCommand`, using the hyphenated spelling for its
    Python tomo tools. It is what makes the program write the exit files that
    `--check_job_completion` reads, so a job run from here reaches "Succeeded"
    in RELION's GUI rather than sitting at "Running" forever. It is also what
    lets a real relion_refine's own long-running optimiser loop notice a
    RELION_JOB_ABORT_NOW file mid-run and exit gracefully (ml_optimiser.cpp's
    own pipeline_control_check_abort_job() calls) -- an abort clicked in real
    RELION's own GUI (concurrent two-way sync) depends on this, independent of
    RELION-US's own Abort button, which signals the process group directly.

    A multi-command draft (issue #56 -- e.g. Inimodel's relion_refine
    followed by relion_align_symmetry, joined with real shell " && ") needs
    this on EACH qualifying command, not just wherever it lands in the
    string: real RELION's own prepareFinalCommand loops over every entry in
    its `commands` vector and adds the flag to each one containing
    "relion_" BEFORE joining them with " && " (src/pipeline_jobs.cpp
    ~708-718) -- this mirrors that loop instead of treating the whole
    already-joined string as one opaque command, which would silently leave
    every command except the last one (arbitrarily) missing this flag."""
    subdir = job_subdir if job_subdir.endswith("/") else job_subdir + "/"

    def _add(segment: str) -> str:
        if "relion_" not in segment:
            return segment
        flag = (PIPELINE_CONTROL_FLAG_PYTHON if "relion_python_" in segment
                else PIPELINE_CONTROL_FLAG)
        if flag in segment:
            return segment
        return f"{segment} {flag} {subdir}"

    return " && ".join(_add(segment) for segment in command.split(" && "))
