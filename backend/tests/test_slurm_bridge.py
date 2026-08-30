"""
Tests for slurm_bridge.py: template filling, submission, status polling,
and cancellation. sbatch/squeue/sacct/scancel don't exist on this dev
machine (or most CI runners), so every test against the "real binary"
functions uses a stub script placed on PATH for that test -- same
technique test_custom_jobs.py's synced_project fixture already uses for
relion_pipeliner (fake_relion_pipeliner.py).
"""
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import slurm_bridge

TEMPLATE = Path(__file__).resolve().parent.parent.parent / "slurm" / "template_relion_job.sbatch"


def _stub(tmp_path, name, script_body):
    """Write an executable stub binary named `name` and put its directory
    first on PATH for this test (monkeypatch reverts it automatically)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    path = bindir / name
    path.write_text(f"#!/usr/bin/env bash\n{script_body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def _put_on_path(monkeypatch, bindir):
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


# ---------------------------------------------------------------------------
# fill_sbatch_template
# ---------------------------------------------------------------------------


def test_fill_sbatch_template_substitutes_every_placeholder():
    text = slurm_bridge.fill_sbatch_template(
        TEMPLATE,
        command="relion_run_motioncorr --i x --o y --j 8",
        job_name="motioncorr_job012",
        account="mygroup",
        partition="gpu",
        ntasks=1,
        cpus_per_task=8,
        mem="32G",
        time_limit="04:00:00",
        gres_line="#SBATCH --gres=gpu:2",
        out_path="/proj/MotionCorrection/job012/run_submit.out",
        err_path="/proj/MotionCorrection/job012/run_submit.err",
    )
    assert "--job-name=motioncorr_job012" in text
    assert "--account=mygroup" in text
    assert "--partition=gpu" in text
    assert "--ntasks=1" in text
    assert "--cpus-per-task=8" in text
    assert "--mem=32G" in text
    assert "--time=04:00:00" in text
    assert "relion_run_motioncorr --i x --o y --j 8" in text
    assert "#SBATCH --gres=gpu:2" in text
    assert "--output=/proj/MotionCorrection/job012/run_submit.out" in text
    assert "--error=/proj/MotionCorrection/job012/run_submit.err" in text
    # No leftover placeholder tokens anywhere in the filled output.
    for token in ("JOB_NAME", "ACCOUNT_NAME", "PARTITION_NAME", "NTASKS",
                  "CPUS_PER_TASK", "MEM_SIZE", "TIME_LIMIT", "RELION_COMMAND",
                  "GRES_LINE", "OUT_PATH", "ERR_PATH"):
        assert token not in text, f"leftover placeholder {token!r} in filled template"


def test_fill_sbatch_template_omits_gres_line_for_cpu_only():
    text = slurm_bridge.fill_sbatch_template(
        TEMPLATE, command="cmd", job_name="j", account="a", partition="p",
        ntasks=1, cpus_per_task=4, mem="8G", time_limit="01:00:00", gres_line="",
        out_path="/proj/j/run_submit.out", err_path="/proj/j/run_submit.err",
    )
    assert "--gres" not in text
    assert "GRES_LINE" not in text


# ---------------------------------------------------------------------------
# submit_sbatch
# ---------------------------------------------------------------------------


def test_submit_sbatch_parses_parsable_job_id(tmp_path, monkeypatch):
    bindir = _stub(tmp_path, "sbatch", 'echo "12345"')
    _put_on_path(monkeypatch, bindir)
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\necho hi\n")
    assert slurm_bridge.submit_sbatch(script) == "12345"


def test_submit_sbatch_parsable_with_cluster_suffix(tmp_path, monkeypatch):
    """--parsable on a federated cluster setup prints "<id>;<cluster>" --
    only the leading job ID should be kept."""
    bindir = _stub(tmp_path, "sbatch", 'echo "12345;mycluster"')
    _put_on_path(monkeypatch, bindir)
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\necho hi\n")
    assert slurm_bridge.submit_sbatch(script) == "12345"


def test_submit_sbatch_falls_back_when_parsable_unsupported(tmp_path, monkeypatch):
    """Simulates an sbatch build where --parsable prints nothing useful (or
    errors), but the plain invocation still prints the standard
    "Submitted batch job N" line."""
    bindir = _stub(tmp_path, "sbatch", '''
if [[ "$*" == *--parsable* ]]; then
  echo "sbatch: unrecognized option" >&2
  exit 1
else
  echo "Submitted batch job 67890"
fi
''')
    _put_on_path(monkeypatch, bindir)
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\necho hi\n")
    assert slurm_bridge.submit_sbatch(script) == "67890"


def test_submit_sbatch_raises_with_stderr_on_failure(tmp_path, monkeypatch):
    bindir = _stub(tmp_path, "sbatch", 'echo "invalid partition: bogus" >&2; exit 1')
    _put_on_path(monkeypatch, bindir)
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\necho hi\n")
    with pytest.raises(RuntimeError, match="invalid partition"):
        slurm_bridge.submit_sbatch(script)


def test_submit_sbatch_raises_clear_error_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\necho hi\n")
    with pytest.raises(RuntimeError, match="sbatch"):
        slurm_bridge.submit_sbatch(script)


def test_submit_sbatch_never_retries_after_a_successful_parsable_call(tmp_path, monkeypatch):
    """Found in code review: the original fallback logic retried the plain
    `sbatch` invocation whenever --parsable's OUTPUT was unparsable, even
    if --parsable itself had already exited 0 -- meaning the job was
    ALREADY submitted, and retrying would submit it a SECOND time, leaving
    the first copy orphaned and untracked on the cluster. A successful
    (exit 0) --parsable call must never be followed by a second sbatch
    invocation, even when its stdout can't be parsed."""
    calls_file = tmp_path / "calls.txt"
    bindir = _stub(tmp_path, "sbatch", f'''
echo "$@" >> {calls_file}
if [[ "$*" == *--parsable* ]]; then
  echo "Some informational banner this site prints"
  exit 0
else
  echo "Submitted batch job 99999"
fi
''')
    _put_on_path(monkeypatch, bindir)
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\necho hi\n")

    with pytest.raises(RuntimeError, match="succeeded but printed no parsable job ID"):
        slurm_bridge.submit_sbatch(script)

    # sbatch must have been invoked exactly once -- not a second time.
    assert len(calls_file.read_text().strip().splitlines()) == 1


# ---------------------------------------------------------------------------
# poll_job_state
# ---------------------------------------------------------------------------


def test_poll_job_state_uses_squeue_while_live(tmp_path, monkeypatch):
    bindir = _stub(tmp_path, "squeue", 'echo "RUNNING"')
    _put_on_path(monkeypatch, bindir)
    result = slurm_bridge.poll_job_state("12345")
    assert result == {"raw_state": "RUNNING", "exit_code": None}


def test_poll_job_state_falls_back_to_sacct_once_squeue_is_empty(tmp_path, monkeypatch):
    """The normal way a wrapper learns a job finished -- squeue ages
    completed jobs out quickly, this must not be treated as an error."""
    bindir = _stub(tmp_path, "squeue", "true")  # prints nothing, exit 0
    _stub(tmp_path, "sacct", '''
echo "12345|COMPLETED|0:0"
echo "12345.batch|COMPLETED|0:0"
''')
    _put_on_path(monkeypatch, bindir)
    result = slurm_bridge.poll_job_state("12345")
    assert result == {"raw_state": "COMPLETED", "exit_code": 0}


def test_poll_job_state_matches_bare_job_id_not_step_rows(tmp_path, monkeypatch):
    """If the .batch step row happened to come first, or report a
    different state, the bare-id row must still be the one returned."""
    bindir = _stub(tmp_path, "squeue", "true")
    _stub(tmp_path, "sacct", '''
echo "12345.extern|COMPLETED|0:0"
echo "12345.batch|FAILED|1:0"
echo "12345|FAILED|1:0"
''')
    _put_on_path(monkeypatch, bindir)
    result = slurm_bridge.poll_job_state("12345")
    assert result == {"raw_state": "FAILED", "exit_code": 1}


def test_poll_job_state_normalizes_cancelled_by_uid(tmp_path, monkeypatch):
    bindir = _stub(tmp_path, "squeue", "true")
    _stub(tmp_path, "sacct", 'echo "12345|CANCELLED by 1000|0:0"')
    _put_on_path(monkeypatch, bindir)
    result = slurm_bridge.poll_job_state("12345")
    assert result["raw_state"] == "CANCELLED"


def test_poll_job_state_raises_when_no_sacct_record_found(tmp_path, monkeypatch):
    bindir = _stub(tmp_path, "squeue", "true")
    _stub(tmp_path, "sacct", "true")  # prints nothing
    _put_on_path(monkeypatch, bindir)
    with pytest.raises(RuntimeError, match="No sacct record"):
        slurm_bridge.poll_job_state("99999")


# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------


def test_cancel_job_calls_scancel_with_the_job_id(tmp_path, monkeypatch):
    calls_file = tmp_path / "calls.txt"
    bindir = _stub(tmp_path, "scancel", f'echo "$@" >> {calls_file}')
    _put_on_path(monkeypatch, bindir)
    slurm_bridge.cancel_job("12345")
    assert calls_file.read_text().strip() == "12345"


def test_cancel_job_raises_on_failure(tmp_path, monkeypatch):
    bindir = _stub(tmp_path, "scancel", 'echo "Invalid job id specified" >&2; exit 1')
    _put_on_path(monkeypatch, bindir)
    with pytest.raises(RuntimeError, match="Invalid job id"):
        slurm_bridge.cancel_job("99999")


def test_slurm_state_to_status_covers_the_real_terminal_states_that_were_missing():
    """Found in code review: BOOT_FAIL/SPECIAL_EXIT/REVOKED are real,
    terminal SLURM states that were absent from the map, silently falling
    back to the default "running" -- which is not one of the poll loop's
    recognized terminal statuses, so a job in one of these states would
    have polled forever, never reaching completed/failed/aborted."""
    for state in ("BOOT_FAIL", "SPECIAL_EXIT", "REVOKED"):
        assert slurm_bridge.SLURM_STATE_TO_STATUS[state] == "failed", state
    # STOPPED (suspended via SIGSTOP) is NOT terminal -- must stay "running"
    # so the poll loop keeps watching it.
    assert slurm_bridge.SLURM_STATE_TO_STATUS["STOPPED"] == "running"
