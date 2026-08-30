"""
slurm_bridge.py — fill an sbatch template, submit it, poll its status,
cancel it. The single source of truth for both the standalone
`slurm/submit.py` CLI and JobRunManager's "Submit to SLURM cluster" path
(backend/job_runner.py's _run_slurm_job) — one template-filling
implementation, not two that could drift.

Design note (see docs/ARCHITECTURE.md and GitHub issue #1): real RELION's
own "submit to queue" system (src/pipeline_jobs.cpp) is a generic, dumb
XXX-token text-substitution wrapper around a user-supplied template — it
never synthesizes scheduler directives itself. This module does the same:
fill_sbatch_template() does literal placeholder replacement against the
existing, human-editable .sbatch templates in slurm/, rather than
generating #SBATCH lines programmatically. Partition/account names and GPU
GRES syntax are genuinely site-specific (confirmed: some clusters use
--gres=gpu:N, others --gpus=N, GPU type strings are site-defined) — this
module never guesses them; they always come from the caller (the job popup
or slurm/submit.py's CLI flags), sourced ultimately from user input or the
Settings popup's slurm.* defaults.

sbatch/squeue/sacct/scancel are standard, portable SLURM CLI surface
(not site-specific) — verified: `sbatch` prints "Submitted batch job N" to
stdout (or a bare job ID with --parsable); `squeue -j <id> -h -o "%T"`
reports live state but SLURM ages completed jobs out of squeue quickly, so
`sacct -j <id> --format=State,ExitCode -n -P` is the durable fallback for
terminal state (an empty squeue result means "check sacct", not "job
vanished"); `scancel <id>` cancels.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, Path]

# SLURM state strings (squeue's %T / sacct's State column, both use the
# same vocabulary) -> RELION-US's own run-status vocabulary
# (job_runner.STATUS_*). CANCELLED maps to "aborted" since that's the same
# outcome this app's own Abort produces for a local job. Any state NOT in
# this dict falls back to "running" (job_runner.py's poll loop) -- a
# genuinely unrecognized state is far more likely to be a still-active one
# this list hasn't been extended for yet than a terminal one, and treating
# an active state as terminal would end tracking on a job that's still
# consuming allocation. BOOT_FAIL/SPECIAL_EXIT/REVOKED are explicitly
# listed rather than left to that fallback, though: they're real, terminal
# SLURM states (a node failed to boot before the job ever ran; the job
# script itself set a special nonzero exit; an admin revoked the
# association) that would otherwise poll forever, never reaching one of
# the poll loop's terminal statuses.
SLURM_STATE_TO_STATUS = {
    "PENDING": "queued",
    "CONFIGURING": "queued",
    "RESIZING": "queued",
    "RUNNING": "running",
    "COMPLETING": "running",
    "SUSPENDED": "running",
    "STOPPED": "running",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "TIMEOUT": "failed",
    "OUT_OF_MEMORY": "failed",
    "NODE_FAIL": "failed",
    "DEADLINE": "failed",
    "PREEMPTED": "failed",
    "BOOT_FAIL": "failed",
    "SPECIAL_EXIT": "failed",
    "REVOKED": "failed",
    "CANCELLED": "aborted",
}

_SUBMITTED_RE = re.compile(r"Submitted batch job (\d+)")


def _require_binary(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(
            f"'{name}' not found on PATH. This function shells out to SLURM's "
            f"own {name} rather than reimplementing job scheduling — load your "
            f"cluster's SLURM environment first (e.g. run this from a login "
            f"node), or check PATH."
        )
    return resolved


def fill_sbatch_template(
    template_path: PathLike,
    *,
    command: str,
    job_name: str,
    account: str,
    partition: str,
    ntasks: int,
    cpus_per_task: int,
    mem: str,
    time_limit: str,
    gres_line: str,
    out_path: str,
    err_path: str,
) -> str:
    """
    Literal placeholder substitution against an existing .sbatch template
    (slurm/template_relion_job.sbatch). No templating engine — the files
    stay short, readable plain text a user can open and edit by hand.

    out_path/err_path are ABSOLUTE paths for --output/--error, not SLURM's
    own %x-%j pattern -- deterministic and caller-chosen (usually inside
    the job's own tracked output directory) rather than depending on
    knowing the job ID *before* submission (which isn't possible) or on
    the script's submission-time working directory. This also means a
    caller can start tailing immediately after choosing the path, without
    waiting to observe the job actually reach RUNNING first (a fast job
    can go queued -> completed between two polls, skipping RUNNING
    entirely -- see job_runner.py's _run_slurm_job).

    gres_line: pass a full line (e.g. "#SBATCH --gres=gpu:2") for a GPU
    job, or "" for a CPU-only job — the placeholder line is replaced
    outright, including the newline, so "" genuinely removes the line
    rather than leaving an empty #SBATCH directive.
    """
    text = Path(template_path).read_text()
    replacements = {
        "JOB_NAME": job_name,
        "ACCOUNT_NAME": account,
        "PARTITION_NAME": partition,
        "NTASKS": str(ntasks),
        "CPUS_PER_TASK": str(cpus_per_task),
        "MEM_SIZE": mem,
        "TIME_LIMIT": time_limit,
        "OUT_PATH": out_path,
        "ERR_PATH": err_path,
        "RELION_COMMAND": command,
    }
    for token, value in replacements.items():
        text = text.replace(token, value)
    # GRES_LINE is handled separately from the flat substitution above: it
    # replaces the WHOLE line (template has "GRES_LINE\n" on its own line),
    # so an empty gres_line removes the #SBATCH directive entirely instead
    # of leaving a blank/invalid one.
    lines = text.splitlines(keepends=True)
    text = "".join(
        (gres_line + "\n" if gres_line else "") if line.strip() == "GRES_LINE" else line
        for line in lines
    )
    return text


def submit_sbatch(script_path: PathLike, cwd: Optional[PathLike] = None) -> str:
    """
    Submit an already-filled .sbatch script via `sbatch --parsable`
    (prints just the bare job ID, optionally "<id>;<cluster>" on a
    federated setup — only the leading digits are kept). Falls back to
    parsing plain `sbatch`'s "Submitted batch job N" stdout ONLY when the
    --parsable invocation itself failed (nonzero exit) — never after a
    successful (exit 0) --parsable call, even if its output couldn't be
    parsed, because at that point the job has ALREADY been submitted;
    retrying would submit it a second time, leaving the first copy
    orphaned and untracked on the cluster. Raises RuntimeError with
    sbatch's own stderr on failure (or, for the unparsable-success case,
    a message saying the job WAS submitted but couldn't be tracked).
    """
    sbatch = _require_binary("sbatch")
    script_path = Path(script_path)

    result = subprocess.run(
        [sbatch, "--parsable", str(script_path)],
        capture_output=True, text=True, cwd=str(cwd) if cwd else None,
    )
    if result.returncode == 0:
        job_id = result.stdout.strip().split(";", 1)[0]
        if job_id.isdigit():
            return job_id
        match = _SUBMITTED_RE.search(result.stdout)
        if match:
            return match.group(1)
        raise RuntimeError(
            f"sbatch --parsable succeeded but printed no parsable job ID "
            f"(stdout: {result.stdout!r}) for {script_path} -- the job WAS "
            f"submitted; check `squeue`/`sacct` on your cluster to find it "
            f"manually, since this app can't track it without an ID."
        )

    # --parsable itself was rejected/unsupported (nonzero exit) -- nothing
    # was submitted above, so retrying with the plain invocation is safe.
    result = subprocess.run(
        [sbatch, str(script_path)],
        capture_output=True, text=True, cwd=str(cwd) if cwd else None,
    )
    match = _SUBMITTED_RE.search(result.stdout)
    if result.returncode == 0 and match:
        return match.group(1)
    raise RuntimeError(
        f"sbatch failed (exit {result.returncode}) for {script_path}:\n"
        f"{result.stderr or result.stdout}"
    )


def poll_job_state(job_id: str) -> dict:
    """
    Live state via `squeue`; falls back to `sacct` once SLURM has aged
    the job out of squeue's view (an empty squeue result is the NORMAL
    way a wrapper learns a job finished, not an error). Returns
    {"raw_state": <SLURM state string>, "exit_code": Optional[int]}
    (exit_code only meaningful/present once sacct has a terminal record).
    """
    squeue = shutil.which("squeue")
    if squeue is not None:
        result = subprocess.run(
            [squeue, "-h", "-o", "%T", "-j", job_id],
            capture_output=True, text=True,
        )
        state = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
        if state:
            return {"raw_state": state, "exit_code": None}

    sacct = _require_binary("sacct")
    result = subprocess.run(
        [sacct, "-j", job_id, "--format=JobID,State,ExitCode", "-n", "-P"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"sacct failed (exit {result.returncode}) for job {job_id}:\n{result.stderr}")
    # sacct returns one row per job STEP (bare "<id>", plus "<id>.batch",
    # "<id>.extern", etc.) -- only the bare-id row is the overall job's own
    # state; a step row can report a different state/exit code.
    for line in result.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        row_job_id, state_field, exit_field = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if row_job_id != job_id:
            continue
        exit_code = None
        if ":" in exit_field:
            try:
                exit_code = int(exit_field.split(":", 1)[0])
            except ValueError:
                exit_code = None
        # Normalize e.g. "CANCELLED by 1000" -> "CANCELLED"
        raw_state = state_field.split()[0] if state_field else state_field
        return {"raw_state": raw_state, "exit_code": exit_code}
    raise RuntimeError(f"No sacct record found for job {job_id} (not submitted, or purged from accounting).")


def cancel_job(job_id: str) -> None:
    scancel = _require_binary("scancel")
    result = subprocess.run([scancel, job_id], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"scancel failed (exit {result.returncode}) for job {job_id}:\n{result.stderr}")
