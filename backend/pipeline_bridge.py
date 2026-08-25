"""
pipeline_bridge.py — registering RELION-US's jobs in RELION's own pipeline, so
the two GUIs can be used on the same project interchangeably.

**Nothing here writes `default_pipeline.star`.** Every change to it goes through
RELION's own `relion_pipeliner` binary. That is not squeamishness — the file is
five linked tables (general, processes, nodes, input edges, output edges), the
node graph is computed by each job's own `getCommands<Job>Job()` C++, and access
is guarded by a `.relion_lock/` mutex directory that RELION's GUI holds while it
is open. Reimplementing any of that in Python would be a new source of truth
for a file this app is only a guest in, and the failure mode is a corrupted
project rather than an error message.

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

Completion is RELION's mechanism too. `prepareFinalCommand` appends
`--pipeline_control <jobdir>/` to every `relion_` command; the program then
writes `RELION_JOB_EXIT_SUCCESS` / `_FAILURE` / `_ABORTED` into that directory
when it ends (`src/pipeline_control.h`), and

    relion_pipeliner --check_job_completion

flips the pipeline's status for any Running process whose exit file has
appeared. RELION-US appends the same flag and calls the same command, so a job
run here reaches "Succeeded" in RELION's GUI by RELION's own route.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
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
    in RELION's GUI rather than sitting at "Running" forever.
    """
    if "relion_" not in command:
        return command
    subdir = job_subdir if job_subdir.endswith("/") else job_subdir + "/"
    flag = (PIPELINE_CONTROL_FLAG_PYTHON if "relion_python_" in command
            else PIPELINE_CONTROL_FLAG)
    if flag in command:
        return command
    return f"{command} {flag} {subdir}"
