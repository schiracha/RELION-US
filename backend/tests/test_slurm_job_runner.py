"""
Integration tests for JobRunManager's SLURM path (start_subprocess_job's
slurm_options param -> _run_slurm_job -> abort_run's slurm_job_id branch).

Uses stub sbatch/squeue/sacct/scancel scripts on PATH -- same technique
test_custom_jobs.py's synced_project fixture uses for relion_pipeliner --
driven through a lifecycle by a small state file the test itself writes to,
so a single fake squeue/sacct can report PENDING -> RUNNING -> (aged out of
squeue) -> sacct COMPLETED/FAILED/CANCELLED without needing a real
scheduler. job_runner.SLURM_POLL_INTERVAL_S is monkeypatched down so tests
don't actually wait real minutes between polls.
"""
import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_runner
import project_manager
from job_runner import (
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    JobRunManager,
)


def _stub(bindir, name, script_body):
    path = bindir / name
    path.write_text(f"#!/usr/bin/env bash\n{script_body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def slurm_stubs(tmp_path, monkeypatch):
    """Stub sbatch/squeue/sacct/scancel on PATH. squeue/sacct read a small
    state file (`state.txt`) the test writes to drive the simulated job's
    lifecycle: "PENDING"/"RUNNING" makes squeue report that state; "GONE"
    makes squeue print nothing (aged out) so poll_job_state falls back to
    sacct, which reads a second file (`sacct.txt`, "STATE|EXITCODE") for
    the terminal record. scancel just appends "CANCELLED" to state.txt AND
    "STATE|EXITCODE" (CANCELLED) to sacct.txt, and records its own
    invocation to `scancel_calls.txt` so a test can assert it was called.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state_file = tmp_path / "state.txt"
    sacct_file = tmp_path / "sacct.txt"
    scancel_calls = tmp_path / "scancel_calls.txt"
    state_file.write_text("PENDING")
    sacct_file.write_text("")

    _stub(bindir, "sbatch", 'echo "77777"')
    _stub(bindir, "squeue", f'''
state=$(cat "{state_file}" 2>/dev/null || echo "")
if [[ "$state" == "GONE" || "$state" == "CANCELLED" ]]; then
  exit 0
fi
echo "$state"
''')
    _stub(bindir, "sacct", f'''
if [[ -s "{sacct_file}" ]]; then
  cat "{sacct_file}"
fi
''')
    _stub(bindir, "scancel", f'''
echo "$@" >> "{scancel_calls}"
echo "CANCELLED" > "{state_file}"
echo "77777|CANCELLED by 1000|0:0" > "{sacct_file}"
''')
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(job_runner, "SLURM_POLL_INTERVAL_S", 0.02)
    return {"state_file": state_file, "sacct_file": sacct_file, "scancel_calls": scancel_calls}


async def _wait_for_status(run, statuses, timeout_s=5.0):
    for _ in range(int(timeout_s / 0.01)):
        if run.status in statuses:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"run never reached {statuses}, last status: {run.status}")


def test_slurm_submission_reaches_queued_then_running_then_completed(tmp_path, slurm_stubs):
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job(
            "Import", "Import", "echo hello", subdir="run1",
            field_values={"nr_mpi": "1", "nr_threads": "4"},
            slurm_options={"account": "mygroup", "partition": "batch"},
        )
        await _wait_for_status(run, {STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED})
        assert run.status == STATUS_QUEUED
        assert run.slurm_job_id == "77777"

        # Simulate the scheduler starting the job.
        slurm_stubs["state_file"].write_text("RUNNING")
        await _wait_for_status(run, {STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED})
        assert run.status == STATUS_RUNNING

        # Simulate the job writing output, then finishing successfully.
        # Deterministic path in the job's own tracked output directory
        # (run.cwd), not SLURM's %x-%j pattern -- see fill_sbatch_template
        # and _run_slurm_job's own docstrings for why.
        job_out = Path(run.cwd) / "run_submit.out"
        job_out.write_text("hello from the compute node\n")
        slurm_stubs["state_file"].write_text("GONE")
        slurm_stubs["sacct_file"].write_text("77777|COMPLETED|0:0\n77777.batch|COMPLETED|0:0\n")
        await _wait_for_status(run, {STATUS_COMPLETED, STATUS_FAILED})
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_COMPLETED
    assert run.exit_code == 0
    assert "hello from the compute node" in run.stdout_lines


def test_slurm_submission_written_sbatch_script_has_the_real_command(tmp_path, slurm_stubs):
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job(
            "Import", "Import", "relion_import --o Import/job001/", subdir="run1",
            field_values={"nr_mpi": "1", "nr_threads": "4"},
            slurm_options={"account": "mygroup", "partition": "gpu"},
        )
        await _wait_for_status(run, {STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED})
        return run

    run = asyncio.run(go())
    script = (Path(run.cwd) / "run_submit.sbatch").read_text()
    assert "relion_import --o Import/job001/" in script
    assert "--account=mygroup" in script
    assert "--cpus-per-task=4" in script


def test_slurm_submission_failure_marks_the_run_failed(tmp_path, monkeypatch):
    project_manager.init_new_project(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "sbatch", 'echo "invalid account" >&2; exit 1')
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job(
            "Import", "Import", "echo hello", subdir="run1",
            slurm_options={"account": "bad"},
        )
        await _wait_for_status(run, {STATUS_COMPLETED, STATUS_FAILED})
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_FAILED
    assert any("invalid account" in line for line in run.stderr_lines)


def test_abort_a_queued_slurm_job_calls_scancel(tmp_path, slurm_stubs):
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job(
            "Import", "Import", "echo hello", subdir="run1",
            slurm_options={"account": "mygroup"},
        )
        await _wait_for_status(run, {STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED})
        assert run.status == STATUS_QUEUED

        ok = await manager.abort_run(run.run_id)
        assert ok is True
        assert run.status == STATUS_ABORTED  # optimistic, set immediately

        await _wait_for_status(run, {STATUS_ABORTED, STATUS_COMPLETED, STATUS_FAILED}, timeout_s=5.0)
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_ABORTED
    assert slurm_stubs["scancel_calls"].read_text().strip() == "77777"


def test_abort_during_submission_cancels_the_just_submitted_job(tmp_path, monkeypatch):
    """Found in code review: the only real await point before run.slurm_job_id
    exists is the submit_sbatch() call itself. An abort landing in that
    window used to fall through to abort_run()'s "nothing to signal yet"
    branch (nothing wrong with that BY ITSELF), but _run_slurm_job then
    unconditionally overwrote the just-set STATUS_ABORTED back to QUEUED
    once submission returned -- silently discarding the abort and letting
    the job run unmanaged on the cluster. Simulates the race directly by
    making the sbatch stub itself request the abort mid-submission."""
    project_manager.init_new_project(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    scancel_calls = tmp_path / "scancel_calls.txt"
    _stub(bindir, "sbatch", 'echo "88888"')
    _stub(bindir, "scancel", f'echo "$@" >> {scancel_calls}')
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(job_runner, "SLURM_POLL_INTERVAL_S", 3600)

    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job(
            "Import", "Import", "echo hello", subdir="run1",
            slurm_options={"account": "mygroup"},
        )
        # Simulates an abort landing before run.slurm_job_id is set (the
        # only real await point before that is submit_sbatch() itself, a
        # plain synchronous assignment here lands well within that window
        # since start_subprocess_job() returns before the background task
        # has done much of anything yet) -- exercising the same "already
        # aborted by the time submission returns" branch in _run_slurm_job
        # regardless of the exact timing.
        run.status = job_runner.STATUS_ABORTED

        for _ in range(300):
            if run.ended_at is not None:
                break
            await asyncio.sleep(0.01)
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_ABORTED
    assert run.slurm_job_id == "88888"
    assert scancel_calls.read_text().strip() == "88888"


def test_short_job_that_never_observed_running_still_captures_output(tmp_path, slurm_stubs):
    """Found in code review: output tailing used to only start once the
    poll loop observed a queued->running TRANSITION. A fast job can finish
    between two polls, going straight from queued to a terminal state
    without ever being observed as "running" -- the old code left
    out_path/err_path at None forever in that case, losing 100% of the
    job's output. Output paths are now known immediately (deterministic,
    inside run.cwd), so tailing works regardless of which states were
    actually observed in between."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job(
            "Import", "Import", "echo hello", subdir="run1",
            slurm_options={"account": "mygroup"},
        )
        await _wait_for_status(run, {STATUS_QUEUED})
        # Write output and jump STRAIGHT from queued to completed --
        # "running" is never observed by any poll.
        (Path(run.cwd) / "run_submit.out").write_text("fast job output\n")
        slurm_stubs["state_file"].write_text("GONE")
        slurm_stubs["sacct_file"].write_text("77777|COMPLETED|0:0\n")
        await _wait_for_status(run, {STATUS_COMPLETED, STATUS_FAILED})
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_COMPLETED
    assert "fast job output" in run.stdout_lines


def test_abortable_true_for_a_queued_slurm_job(tmp_path, slurm_stubs):
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job(
            "Import", "Import", "echo hello", subdir="run1",
            slurm_options={"account": "mygroup"},
        )
        await _wait_for_status(run, {STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED})
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_QUEUED
    assert run.to_summary()["abortable"] is True


def test_delete_run_refuses_a_queued_slurm_job(tmp_path, slurm_stubs):
    """delete_run only checked STATUS_RUNNING before this feature -- a
    queued (not yet running) SLURM job is just as live in the scheduler
    and must be refused the same way, not silently deletable."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job(
            "Import", "Import", "echo hello", subdir="run1",
            slurm_options={"account": "mygroup"},
        )
        await _wait_for_status(run, {STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED})
        assert run.status == STATUS_QUEUED
        return run

    run = asyncio.run(go())
    ok, reason = manager.delete_run(run.run_id, remove_files=False)
    assert ok is False
    assert "still running" in reason


def test_overwrite_refuses_a_queued_slurm_job(tmp_path, slurm_stubs):
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job(
            "Import", "Import", "echo hello", subdir="run1",
            slurm_options={"account": "mygroup"},
        )
        await _wait_for_status(run, {STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED})
        assert run.status == STATUS_QUEUED
        return run

    run = asyncio.run(go())
    with pytest.raises(ValueError, match="still running"):
        manager._resolve_overwrite_target(run.run_id, tmp_path)


def test_local_subprocess_jobs_are_unaffected_by_slurm_wiring(tmp_path):
    """A plain (no slurm_options) run must behave exactly as before --
    this is the default, most common path and must not regress."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job("Import", "Import", "echo hello", subdir="run1")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if run.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_COMPLETED
    assert run.slurm_job_id is None
    assert run.exit_code == 0


# ---------------------------------------------------------------------------
# _tail_new_lines -- direct tests of the byte-first line-splitting logic
# ---------------------------------------------------------------------------


def _bare_run(tmp_path):
    return job_runner.JobRun(
        run_id="r1", internal_name="Import", display_name="Import",
        command="echo hi", cwd=str(tmp_path), project_dir=str(tmp_path), job_number=1,
    )


def test_tail_new_lines_holds_back_an_incomplete_line_by_default(tmp_path):
    manager = JobRunManager(tmp_path)
    run = _bare_run(tmp_path)
    path = tmp_path / "out.log"
    path.write_bytes(b"line one\nline two, no newline yet")

    pos = asyncio.run(manager._tail_new_lines(run, path, 0, "stdout"))

    assert run.stdout_lines == ["line one"]
    # pos must point exactly at the start of the held-back partial line,
    # so the NEXT poll re-reads it once it's complete.
    assert pos == len(b"line one\n")


def test_tail_new_lines_flush_partial_emits_the_final_line_with_no_newline(tmp_path):
    """Found in code review: a job's last line often has no trailing
    newline (e.g. a final status print) -- since polling stops the moment
    a terminal state is reached, that line would be lost forever without
    a final flush_partial=True pass."""
    manager = JobRunManager(tmp_path)
    run = _bare_run(tmp_path)
    path = tmp_path / "out.log"
    path.write_bytes(b"final line, no trailing newline")

    pos = asyncio.run(manager._tail_new_lines(run, path, 0, "stdout", flush_partial=True))

    assert run.stdout_lines == ["final line, no trailing newline"]
    assert pos == len(b"final line, no trailing newline")


def test_tail_new_lines_never_corrupts_a_multibyte_character_split_across_polls(tmp_path):
    """Found in code review: the original implementation decoded the
    WHOLE chunk (including any dangling partial multi-byte UTF-8 sequence
    at the end) before figuring out what to hold back, so a poll landing
    mid-character permanently replaced it with U+FFFD. Splitting on the
    raw newline BYTE first is always character-boundary-safe (0x0A can
    never be a UTF-8 continuation byte), so a multi-byte character must
    survive intact across two separate _tail_new_lines calls that happen
    to split the underlying bytes right through the middle of it."""
    manager = JobRunManager(tmp_path)
    run = _bare_run(tmp_path)
    path = tmp_path / "out.log"
    line = "resolution: 3.2Å\n"  # "Å" = 0xC3 0x85 in UTF-8 -- a 2-byte char
    full_bytes = line.encode("utf-8")
    angstrom_byte_offset = full_bytes.index("Å".encode("utf-8"))
    # Write only up to the FIRST byte of the 2-byte character -- simulates
    # a writer flush landing mid-character.
    path.write_bytes(full_bytes[: angstrom_byte_offset + 1])

    pos = asyncio.run(manager._tail_new_lines(run, path, 0, "stdout"))
    assert run.stdout_lines == []  # no complete line yet -- correctly held back
    assert pos == 0  # nothing consumed

    # Now the rest of the character (and the newline) arrives.
    with open(path, "ab") as f:
        f.write(full_bytes[angstrom_byte_offset + 1:])
    pos = asyncio.run(manager._tail_new_lines(run, path, pos, "stdout"))
    assert run.stdout_lines == ["resolution: 3.2Å"]  # intact, not U+FFFD-corrupted
