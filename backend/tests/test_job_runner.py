"""
Tests for job_runner.JobRunManager's project-switching + history-persistence
behavior (see project_manager.py and the "Change Project" feature). Uses
asyncio.run() directly rather than pytest-asyncio, since that plugin isn't a
project dependency and these tests don't need anything fancier.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import project_manager
from job_runner import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    JobRunManager,
    _extract_output_subdir,
    _output_subdir_matches,
)


def test_subprocess_run_persists_to_its_own_project_history(tmp_path):
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job("Import", "Import", "echo hello", subdir="run1")
        # give the background task a moment to finish a trivial `echo`
        for _ in range(50):
            await asyncio.sleep(0.02)
            if run.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_COMPLETED
    assert run.exit_code == 0

    history = project_manager.load_history(tmp_path)
    assert any(h["run_id"] == run.run_id and h["status"] == STATUS_COMPLETED for h in history)


def test_switching_project_scopes_list_runs(tmp_path):
    project_a = tmp_path / "proj_a"
    project_b = tmp_path / "proj_b"
    project_manager.init_new_project(project_a)
    project_manager.init_new_project(project_b)

    manager = JobRunManager(project_a)

    async def go():
        run_a = await manager.start_subprocess_job("Import", "Import", "echo a", subdir="run_a")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if run_a.status in (STATUS_COMPLETED, STATUS_FAILED):
                break

        manager.set_project_dir(project_b)
        run_b = await manager.start_subprocess_job("Import", "Import", "echo b", subdir="run_b")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if run_b.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return run_a, run_b

    run_a, run_b = asyncio.run(go())

    ids_in_a = {r["run_id"] for r in manager.list_runs(project_a)}
    ids_in_b = {r["run_id"] for r in manager.list_runs(project_b)}
    assert run_a.run_id in ids_in_a
    assert run_a.run_id not in ids_in_b
    assert run_b.run_id in ids_in_b
    assert run_b.run_id not in ids_in_a


def test_job_runs_from_project_root_relative_paths_resolve(tmp_path):
    """RELION-matching execution model: the subprocess runs from the PROJECT
    ROOT, so a project-root-relative input (like RELION's `frames/*.mrc`)
    resolves, and the command's `--o <JobDir>/jobNNN/` output lands in the
    job directory (run.cwd)."""
    project_manager.init_new_project(tmp_path)
    (tmp_path / "frames").mkdir()
    (tmp_path / "frames" / "a.txt").write_text("hello")
    manager = JobRunManager(tmp_path)

    async def go():
        sub = manager.prospective_subdir("Import")
        assert sub == "Import/job001"
        run = await manager.start_subprocess_job(
            "Import", "Import", f"cat frames/a.txt > {sub}/copied.txt", subdir=sub
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_COMPLETED, run.stderr_lines
    # run.cwd is the RELION-style job dir, and the output landed there
    assert Path(run.cwd) == tmp_path / "Import" / "job001"
    copied = Path(run.cwd) / "copied.txt"
    assert copied.is_file() and copied.read_text().strip() == "hello"


def test_stale_prospective_job_number_is_renumbered_and_command_rewritten(tmp_path):
    """If the prospective jobNNN in the draft is already taken by the time
    Run is clicked, the runner allocates the next free number, creates that
    dir, and rewrites the command's --o to match (with a note)."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        r1 = await manager.start_subprocess_job(
            "Import", "Import", "echo one > Import/job001/x.txt", subdir="Import/job001"
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if r1.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        # second job still proposes job001 (stale) -> must become job002
        r2 = await manager.start_subprocess_job(
            "Import", "Import", "echo two > Import/job001/y.txt", subdir="Import/job001"
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if r2.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return r1, r2

    r1, r2 = asyncio.run(go())
    assert Path(r1.cwd).name == "job001"
    assert Path(r2.cwd).name == "job002"
    assert "Import/job002/y.txt" in r2.command
    assert "Import/job001" not in r2.command
    assert r2.rewrite_note and "job002" in r2.rewrite_note
    assert (Path(r2.cwd) / "y.txt").is_file()


def test_input_lineage_links_job_to_producer(tmp_path):
    """Command Center 'connect jobs to their inputs': a run whose detected
    input lives under an earlier job's output dir gets an input_links entry
    pointing at that producing job."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)
    runs = [
        {
            "run_id": "prod", "job_number": 1, "job_name": "job001",
            "display_name": "Import", "status": "completed",
            "cwd": str(tmp_path / "Import" / "job001"),
            "project_dir": str(tmp_path), "started_at": 1.0, "detected_inputs": [],
        },
        {
            "run_id": "cons", "job_number": 2, "job_name": "job002",
            "display_name": "Extract", "status": "completed",
            "cwd": str(tmp_path / "Extract" / "job002"),
            "project_dir": str(tmp_path), "started_at": 2.0,
            "detected_inputs": ["Import/job001/tilt_series.star"],
        },
    ]
    manager._attach_input_lineage(runs, tmp_path)
    cons = next(r for r in runs if r["run_id"] == "cons")
    assert "input_links" in cons
    assert cons["input_links"][0]["run_id"] == "prod"
    assert cons["input_links"][0]["job_name"] == "job001"
    # the producer itself has no inputs -> no links key
    prod = next(r for r in runs if r["run_id"] == "prod")
    assert "input_links" not in prod


def test_list_runs_merges_persisted_history_from_a_prior_session(tmp_path):
    """Simulate a restart: history on disk from a run no in-memory
    JobRunManager ever tracked should still show up."""
    project_manager.init_new_project(tmp_path)
    project_manager.save_history(tmp_path, [
        {"run_id": "old123", "display_name": "MotionCorr", "status": "completed",
         "started_at": 1.0, "ended_at": 2.0, "command": "relion_run_motioncorr", "project_dir": str(tmp_path)},
    ])

    fresh_manager = JobRunManager(tmp_path)  # nothing in .runs
    runs = fresh_manager.list_runs()
    assert any(r["run_id"] == "old123" for r in runs)


def test_abort_before_process_exists_does_not_orphan(tmp_path):
    """Aborting in the window between start_subprocess_job() returning and the
    launcher task spawning the process must abort cleanly, with no process
    spawned at all."""
    import subprocess
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)
    marker = "relion_us_pending_abort_test"

    async def go():
        run = await manager.start_subprocess_job(
            "Import", "Import", f"sleep 30 # {marker}", subdir="Import/job001"
        )
        # no await in between -> the launcher hasn't run, run.proc is None
        aborted = await manager.abort_run(run.run_id)
        # abort_run already completed synchronously above; this is just a
        # margin in case a late spawn slips in after it.
        await asyncio.sleep(0.3)
        return run, aborted

    run, aborted = asyncio.run(go())
    assert aborted is True
    assert run.status == "aborted"
    leftover = subprocess.run(["pgrep", "-fc", marker], capture_output=True, text=True)
    assert (leftover.stdout.strip() or "0") == "0", "abort left an orphaned process"


def test_abort_kills_the_whole_process_group(tmp_path):
    """A shell command's children must die too, not just the /bin/sh wrapper."""
    import subprocess
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)
    marker = "relion_us_group_abort_test"

    def count(marker):
        return int(subprocess.run(["pgrep", "-fc", marker],
                                   capture_output=True, text=True).stdout.strip() or "0")

    async def poll_until(cond, cap_s=1.0, step_s=0.05):
        elapsed = 0.0
        while elapsed < cap_s:
            if cond():
                return True
            await asyncio.sleep(step_s)
            elapsed += step_s
        return cond()

    async def go():
        run = await manager.start_subprocess_job(
            "Import", "Import", f"sleep 30 # {marker}", subdir="Import/job001"
        )
        await poll_until(lambda: count(marker) >= 1)
        before = count(marker)
        aborted = await manager.abort_run(run.run_id)
        await poll_until(lambda: count(marker) == 0)
        after = count(marker)
        return run, aborted, before, after

    run, aborted, before, after = asyncio.run(go())
    assert aborted is True
    assert before >= 1, "test process never started"
    assert after == 0, f"{after} process(es) survived the abort"
    assert run.status == "aborted"


def test_abort_reaches_a_child_in_its_own_process_group(tmp_path):
    """MPI launchers (prterun/orted/mpirun -- what every multi-rank RELION
    command actually uses) commonly move their worker ranks into a process
    group of their own while staying in the parent's session, specifically
    so a signal sent to the launcher's own group doesn't reach them --
    confirmed against a real relion_refine_mpi run, whose worker ranks (the
    processes actually using the CPU/GPU) survived an abort that only
    killed the /bin/sh wrapper's own group. Reproduced here with a child
    that calls os.setpgid(0, 0) (new group, same session -- it never calls
    setsid()) before sleeping, the same shape without needing MPI
    installed."""
    import subprocess
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)
    marker = "relion_us_session_abort_test"

    def count(marker):
        return int(subprocess.run(["pgrep", "-fc", marker],
                                   capture_output=True, text=True).stdout.strip() or "0")

    async def poll_until(cond, cap_s=1.0, step_s=0.05):
        elapsed = 0.0
        while elapsed < cap_s:
            if cond():
                return True
            await asyncio.sleep(step_s)
            elapsed += step_s
        return cond()

    async def go():
        command = (
            f"python3 -c \"import os,time; os.setpgid(0, 0); "
            f"time.sleep(30)  # {marker}\""
        )
        run = await manager.start_subprocess_job(
            "Import", "Import", command, subdir="Import/job001"
        )
        await poll_until(lambda: count(marker) >= 1)
        before = count(marker)
        aborted = await manager.abort_run(run.run_id)
        await poll_until(lambda: count(marker) == 0)
        after = count(marker)
        return aborted, before, after

    aborted, before, after = asyncio.run(go())
    assert aborted is True
    assert before >= 1, "test process never started"
    assert after == 0, f"{after} process(es) in a separate group survived the abort"


def test_abort_reconciles_a_run_orphaned_by_a_backend_restart(tmp_path):
    """A run stuck at status "running" in persisted history with no live
    JobRun in this session (the backend restarted or crashed while it was
    still going, or its process was killed outside this app entirely) has
    no process left here to signal -- but leaving it at "running" forever
    would also permanently block Overwrite and Mark as finished/failed,
    both of which refuse to touch a "running" job by design. Abort must
    reconcile the stale status instead of just failing with nothing the
    user can do about it."""
    project_manager.init_new_project(tmp_path)
    project_manager.save_history(tmp_path, [
        {"run_id": "orphaned", "display_name": "Import", "status": "running",
         "started_at": 1.0, "command": "sleep 30", "project_dir": str(tmp_path),
         "cwd": str(tmp_path / "Import" / "job001"), "job_number": 1},
    ])

    fresh_manager = JobRunManager(tmp_path)  # nothing in .runs, like after a restart
    aborted = asyncio.run(fresh_manager.abort_run("orphaned"))
    assert aborted is True

    entry = next(h for h in project_manager.load_history(tmp_path) if h["run_id"] == "orphaned")
    assert entry["status"] == "aborted"
    assert entry["ended_at"] is not None


def test_abort_on_an_orphaned_run_also_kills_the_still_alive_process(tmp_path):
    """The exact real-world scenario this fallback exists for: the backend
    restarted (or, as here, a fresh JobRunManager is used) while a job's
    process was actually still running -- not finished, not already killed
    by anyone. Reconciling only the persisted status without also finding
    and signalling the real process would just make the UI say "aborted"
    while the compute kept going forever, which is precisely what a bare
    status-only fallback (no persisted pid) used to do."""
    import subprocess
    project_manager.init_new_project(tmp_path)
    job_dir = tmp_path / "Import" / "job001"
    job_dir.mkdir(parents=True)

    proc = subprocess.Popen(
        # The safety check (_pid_matches_persisted_run) looks for the job's
        # own project-relative output dir in the process's cmdline, exactly
        # as it would appear in a real --o Import/job001 argument.
        ["/bin/sh", "-c", "sleep 30 --o Import/job001/"],
        start_new_session=True,
    )
    project_manager.save_history(tmp_path, [
        {"run_id": "orphaned", "display_name": "Import", "status": "running",
         "started_at": 1.0, "pid": proc.pid, "command": "sleep 30",
         "project_dir": str(tmp_path), "cwd": str(job_dir), "job_number": 1},
    ])
    assert proc.poll() is None, "test process never started"

    fresh_manager = JobRunManager(tmp_path)  # nothing in .runs, like after a restart

    async def go():
        aborted = await fresh_manager.abort_run("orphaned")
        for _ in range(50):
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.05)
        return aborted

    aborted = asyncio.run(go())
    assert aborted is True
    assert proc.poll() is not None, "the real orphaned process survived the abort"
    proc.wait(timeout=2)


def test_abort_on_an_orphaned_run_never_kills_an_unrelated_process_at_a_reused_pid(tmp_path):
    """PIDs get reused by the OS -- a persisted pid whose current process
    doesn't actually look like this job (nothing recognisable in its
    cmdline) must never be signalled, however tempting reconciling
    "running" is."""
    import subprocess
    project_manager.init_new_project(tmp_path)
    job_dir = tmp_path / "Import" / "job001"
    job_dir.mkdir(parents=True)

    # A real, currently-running process with nothing to do with this job --
    # if the safety check were broken, abort_run would kill this instead.
    unrelated = subprocess.Popen(["sleep", "30"])
    project_manager.save_history(tmp_path, [
        {"run_id": "orphaned", "display_name": "Import", "status": "running",
         "started_at": 1.0, "pid": unrelated.pid, "command": "sleep 30",
         "project_dir": str(tmp_path), "cwd": str(job_dir), "job_number": 1},
    ])

    fresh_manager = JobRunManager(tmp_path)
    try:
        aborted = asyncio.run(fresh_manager.abort_run("orphaned"))
        assert aborted is True          # status still reconciles...
        assert unrelated.poll() is None  # ...but the unrelated process is untouched
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=2)


def test_abort_on_an_already_terminal_orphaned_run_is_a_noop(tmp_path):
    """Only a pending/running orphaned run gets reconciled -- one that's
    already completed/failed is honest as recorded, and abort_run must not
    rewrite it."""
    project_manager.init_new_project(tmp_path)
    project_manager.save_history(tmp_path, [
        {"run_id": "done", "display_name": "Import", "status": "completed",
         "started_at": 1.0, "ended_at": 2.0, "command": "true", "project_dir": str(tmp_path)},
    ])
    fresh_manager = JobRunManager(tmp_path)
    assert asyncio.run(fresh_manager.abort_run("done")) is False
    entry = next(h for h in project_manager.load_history(tmp_path) if h["run_id"] == "done")
    assert entry["status"] == "completed"


def test_abort_unknown_run_id_returns_false(tmp_path):
    project_manager.init_new_project(tmp_path)
    fresh_manager = JobRunManager(tmp_path)
    assert asyncio.run(fresh_manager.abort_run("nope")) is False


def test_cancelling_the_tracking_task_while_process_still_running_does_not_mis_finalize(tmp_path):
    """issue #57: `run.task` getting cancelled (e.g. a real app shutdown)
    while the real child process is still genuinely alive must NOT
    finalize the run to a terminal status -- that would incorrectly report
    a live job as completed/failed. Confirmed for real before this fix:
    the exact scenario below left the run at STATUS_COMPLETED (or FAILED,
    depending on timing) within milliseconds of cancelling, despite the
    real `sleep` process still running (start_new_session=True means it
    outlives the cancelled task)."""
    import subprocess
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)
    marker = "relion_us_cancel_while_running_test"

    async def go():
        run = await manager.start_subprocess_job(
            "Import", "Import", f"sleep 5 # {marker}", subdir="Import/job001"
        )
        for _ in range(150):
            if run.proc is not None:
                break
            await asyncio.sleep(0.02)
        assert run.proc is not None, "process never spawned"
        assert run.proc.returncode is None, (
            "process finished before we could cancel it -- test needs a longer sleep"
        )
        run.task.cancel()
        # Give the cancellation a real chance to be delivered and handled.
        for _ in range(50):
            await asyncio.sleep(0.02)
        return run

    run = asyncio.run(go())
    try:
        assert run.status == STATUS_RUNNING
        assert run.exit_code is None
    finally:
        # The real process is a deliberately-orphaned survivor of this
        # test (that's the whole point being tested) -- clean it up so it
        # doesn't linger past the test run.
        subprocess.run(["pkill", "-f", marker])


def test_cancelling_the_tracking_task_soon_after_a_fast_job_still_finalizes_correctly(tmp_path):
    """issue #57 regression guard: a previously-discarded fix attempt left
    run.status at RUNNING on ANY cancellation, unconditionally -- which
    broke ordinary fast jobs whose tracking task got cancelled by
    something unrelated to the real process's own lifetime (e.g.
    TestClient's own event-loop portal cancelling fire-and-forget tasks on
    teardown) even though the job had, in fact, already finished.

    The exact interleaving that trips this (cancellation landing between
    the real process exiting and this task's own pump() coroutines
    finishing the drain) is a genuine race at the OS/event-loop level --
    not reliably pinned to one precise instant via wall-clock polling from
    a test. Cancelling immediately after spawn and repeating several times
    instead exercises whichever of that race's actual sub-windows real
    scheduling happens to land in on any given attempt; the property that
    actually matters holds regardless of which one: a fast job that
    genuinely completes must ALWAYS end up correctly finalized, never
    stuck at RUNNING."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def one_attempt(i):
        run = await manager.start_subprocess_job(
            "Import", "Import", "echo hello", subdir=f"Import/job{i:03d}"
        )
        # Wait for the process to actually be spawned before cancelling --
        # run.status flips to RUNNING at the very top of _run_subprocess,
        # BEFORE the pipeline-lock work (asyncio.to_thread) and the actual
        # subprocess spawn that follow it; cancelling that early lands
        # outside this fix's try/except entirely (no process ever gets
        # spawned, run.status is stuck at RUNNING forever with nothing to
        # finalize) -- a real, but different and earlier, race than the
        # one this test targets. run.proc is only set once the process
        # genuinely exists.
        for _ in range(200):
            if run.proc is not None:
                break
            await asyncio.sleep(0.001)
        run.task.cancel()
        for _ in range(75):
            await asyncio.sleep(0.02)
            if run.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return run

    async def go():
        return [await one_attempt(i) for i in range(10)]

    runs = asyncio.run(go())
    for run in runs:
        assert run.status == STATUS_COMPLETED, f"{run.run_id}: status={run.status}"
        assert run.exit_code == 0
        assert run.stdout_lines == ["hello"]


def test_subprocess_output_is_teed_to_run_out_and_run_err(tmp_path):
    """RELION's own GUI always tees a job's stdout/stderr into run.out/
    run.err inside the job directory (RelionJob::prepareFinalCommand,
    src/pipeline_jobs.cpp ~line 760). RELION-US streams live over the
    websocket instead of shell-redirecting the command itself, but it must
    still leave the same two files behind -- other RELION tooling (and
    users used to RELION's own GUI) expect them there."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        sub = manager.prospective_subdir("Import")
        run = await manager.start_subprocess_job(
            "Import", "Import",
            "echo stdout-line-1; echo stderr-line-1 1>&2",
            subdir=sub,
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_COMPLETED, run.stderr_lines
    out_text = (Path(run.cwd) / "run.out").read_text()
    err_text = (Path(run.cwd) / "run.err").read_text()
    assert "stdout-line-1" in out_text
    assert "stderr-line-1" in err_text


def test_subprocess_surviving_a_carriage_return_animated_progress_line(tmp_path):
    """RELION's own progress-bar animation prints in place via bare \\r with
    no real \\n between updates (every job's run.out this session showed
    the same `~~(,_,"> yum!` spinner shape) -- a long-running step can
    produce well over 64 KiB between real newlines. The old pump()
    implementation used stream.readline(), which raises ValueError once a
    single line exceeds that limit; the exception was caught and stopped
    THAT pump() coroutine, but nothing else was left draining the pipe, so
    once the OS pipe's own buffer filled the child's next write() blocked
    forever -- confirmed for real: a Class2D job hung 17+ minutes at ~0%
    CPU.

    The payload here is 5,000,000 bytes, not just "over 64 KiB": a smaller
    payload (originally 220,000 bytes in an earlier version of this test)
    writes fast enough that the child finishes and exits before
    readline()'s internal buffering ever backs up far enough to pause the
    transport -- under the OLD code that made this test pass for the WRONG
    reason (proc.wait() returns because the child genuinely exited, not
    because pumping recovered), while stdout_lines came back completely
    empty instead of catching the real hang. Verified directly (via `git
    stash` on job_runner.py): at this size the OLD code hangs -- proc.wait()
    never returns, the test's own poll loop times out with status still
    STATUS_RUNNING -- while the NEW code completes in under a second with
    every line intact. Asserting the full line count (not just that
    "done-marker" appears somewhere) additionally guards against the
    silent-data-loss failure mode the smaller payload could only catch by
    accident."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    n_lines = 500_000
    spinner_script = (
        "python3 -c \""
        "import sys\n"
        f"for i in range({n_lines}):\n"
        "    sys.stdout.write('x' * 9 + chr(13))\n"
        "print('done-marker')\n"
        "\""
    )

    async def go():
        sub = manager.prospective_subdir("Import")
        run = await manager.start_subprocess_job(
            "Import", "Import", spinner_script, subdir=sub,
        )
        for _ in range(400):
            await asyncio.sleep(0.05)
            if run.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_COMPLETED, (run.status, run.stderr_lines[-3:])
    assert len(run.stdout_lines) == n_lines + 1, len(run.stdout_lines)
    assert run.stdout_lines[-1] == "done-marker"
    assert not any("output stream error" in line for line in run.stderr_lines)


def test_overwrite_run_out_appends_rather_than_truncates(tmp_path):
    """RELION appends (">>") to run.out/run.err, not overwrites -- a
    re-run's output accumulates on top of the previous attempt's, matching
    what RELION's own Overwrite does."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        sub = manager.prospective_subdir("Import")
        first = await manager.start_subprocess_job(
            "Import", "Import", "echo first-attempt", subdir=sub,
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if first.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        second = await manager.start_subprocess_job(
            "Import", "Import", "echo second-attempt",
            subdir=sub, overwrite_run_id=first.run_id,
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if second.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return first, second

    first, second = asyncio.run(go())
    assert second.status == STATUS_COMPLETED, second.stderr_lines
    out_text = (Path(second.cwd) / "run.out").read_text()
    assert "first-attempt" in out_text
    assert "second-attempt" in out_text


def test_overwrite_rewrites_a_mismatched_output_path(tmp_path):
    """The command handed to Overwrite is trusted from the (user-editable)
    command box; if its --o path doesn't match the job's real directory
    (e.g. a stale value), it must be corrected the same way a fresh run's
    stale prospective number is -- otherwise RELION writes output somewhere
    the Command Center isn't tracking, or fails outright."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        sub = manager.prospective_subdir("Import")
        first = await manager.start_subprocess_job(
            "Import", "Import", "echo hi", subdir=sub,
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if first.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        second = await manager.start_subprocess_job(
            "Import", "Import", "echo hi --o Import/wrongdir/",
            subdir="Import/wrongdir", overwrite_run_id=first.run_id,
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if second.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return second

    second = asyncio.run(go())
    assert "Import/job001/" in second.command
    assert "wrongdir" not in second.command


# --------------------------------------------------------------------------
# _extract_output_subdir / _output_subdir_matches -- the guard against an
# Overwrite whose (freely user-editable) command was manually pointed at a
# DIFFERENT job's directory. Distinct from the "stale prospective path" case
# above: that one is caught because `subdir` itself disagrees with the real
# directory; this one is a mismatch _rewrite_output_subdir never sees,
# because `subdir` still correctly reports the job's own directory -- only
# the command text was changed.
# --------------------------------------------------------------------------


def test_extract_output_subdir_finds_the_dash_o_flag():
    cmd = "mpirun -n 2 `which relion_refine_mpi` --o Refine3D/job029/run --j 10"
    assert _extract_output_subdir(cmd) == "Refine3D/job029/run"


def test_extract_output_subdir_finds_the_python_tools_spelling():
    cmd = "python3 -m warp_bridge --output-directory Tomo/job005 --other-flag x"
    assert _extract_output_subdir(cmd) == "Tomo/job005"


def test_extract_output_subdir_none_when_flag_is_absent():
    assert _extract_output_subdir("echo hi") is None


def test_extract_output_subdir_none_on_unparseable_command():
    assert _extract_output_subdir("echo 'unbalanced quote") is None


def test_output_subdir_matches_exact_directory():
    assert _output_subdir_matches("prog --o Refine3D/job029/", "Refine3D/job029") is True


def test_output_subdir_matches_the_run_prefix_form():
    """RELION's own convention is --o <JobDir>/jobNNN/run -- "run" is the
    output filename PREFIX for that job, not another directory level."""
    assert _output_subdir_matches("prog --o Refine3D/job029/run", "Refine3D/job029") is True


def test_output_subdir_matches_rejects_a_different_job():
    assert _output_subdir_matches("prog --o Refine3D/job030/run", "Refine3D/job029") is False


def test_output_subdir_matches_true_when_nothing_to_check():
    """No --o at all, or an unparseable command -- fail open (don't block)
    rather than treat "couldn't verify" the same as "confirmed mismatch"."""
    assert _output_subdir_matches("echo hi", "Refine3D/job029") is True


def test_overwrite_blocks_when_the_command_targets_a_different_directory(tmp_path):
    """The real-world failure this guards against: `subdir` still correctly
    reports the job's own directory (so _rewrite_output_subdir sees no
    drift and is a no-op), but the command text itself was manually edited
    to point --o somewhere else. Allowed to run, this app's own tracking
    (and --pipeline_control's exit markers, if sync were on) would stay
    pinned to the job being overwritten while the real output silently
    lands wherever the edited command says."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        sub = manager.prospective_subdir("Import")
        first = await manager.start_subprocess_job("Import", "Import", "echo hi", subdir=sub)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if first.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return first

    first = asyncio.run(go())
    assert first.cwd == str(tmp_path / "Import" / "job001")

    async def try_overwrite():
        return await manager.start_subprocess_job(
            "Import", "Import", "echo hi --o Import/job099/",
            subdir="Import/job001",   # correctly matches -- nothing for the rewrite to fix
            overwrite_run_id=first.run_id,
        )

    with pytest.raises(ValueError, match="doesn't match"):
        asyncio.run(try_overwrite())


def test_overwrite_allows_a_command_that_correctly_targets_its_own_directory(tmp_path):
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        sub = manager.prospective_subdir("Import")
        first = await manager.start_subprocess_job("Import", "Import", "echo hi", subdir=sub)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if first.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        second = await manager.start_subprocess_job(
            "Import", "Import", "echo hi --o Import/job001/",
            subdir="Import/job001", overwrite_run_id=first.run_id,
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if second.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return second

    second = asyncio.run(go())
    assert second.status == STATUS_COMPLETED


def test_overwrite_target_subdir_returns_the_existing_jobs_own_directory(tmp_path):
    """Recomputing a draft for a job the user reopened to Overwrite (e.g. a
    FAILED run) must target THAT job's own directory, not
    prospective_subdir's fresh "next unused number" -- otherwise Recompute
    silently drifts the command's --o onto a new job while the user meant
    to fix the one they have open, and clicking Overwrite creates a new job
    next to it instead of overwriting it."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        sub = manager.prospective_subdir("Import")
        run = await manager.start_subprocess_job(
            "Import", "Import", "echo hi", subdir=sub,
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return run

    first = asyncio.run(go())
    assert first.cwd == str(tmp_path / "Import" / "job001")

    # A second, unrelated job must have moved prospective_subdir on...
    assert manager.prospective_subdir("Import") == "Import/job002"
    # ...but overwrite_target_subdir still points back at the first job.
    assert manager.overwrite_target_subdir(first.run_id) == "Import/job001"


def test_overwrite_target_subdir_unknown_run_raises():
    manager = JobRunManager(Path("/nonexistent"))
    with pytest.raises(ValueError):
        manager.overwrite_target_subdir("no-such-run-id")


# --------------------------------------------------------------------------
# backfill_missing_timestamps -- the one-time repair for history entries
# with no started_at/ended_at recorded (see backend/backfill_timestamps.py
# for the CLI wrapper around this).
# --------------------------------------------------------------------------


def _touch_at(path, mtime):
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_backfill_fills_missing_start_and_end_from_job_files(tmp_path):
    project_manager.init_new_project(tmp_path)
    job_dir = tmp_path / "Import" / "job001"
    job_dir.mkdir(parents=True)
    _touch_at(job_dir / "job.star", 1000.0)
    _touch_at(job_dir / "RELION_JOB_EXIT_SUCCESS", 1200.0)
    project_manager.save_history(tmp_path, [
        {"run_id": "r1", "display_name": "Import", "status": "completed",
         "started_at": None, "ended_at": None, "command": "true",
         "project_dir": str(tmp_path), "cwd": str(job_dir), "job_number": 1},
    ])

    manager = JobRunManager(tmp_path)   # nothing in .runs, like after a restart
    updated = manager.backfill_missing_timestamps()

    assert len(updated) == 1
    assert updated[0]["started_at"] == 1000.0
    assert updated[0]["ended_at"] == 1200.0
    assert updated[0]["timestamp_estimated"] is True
    # ...and it's actually persisted, not just returned
    entry = next(h for h in project_manager.load_history(tmp_path) if h["run_id"] == "r1")
    assert entry["started_at"] == 1000.0
    assert entry["timestamp_estimated"] is True


def test_backfill_never_overwrites_a_value_that_is_already_recorded(tmp_path):
    project_manager.init_new_project(tmp_path)
    job_dir = tmp_path / "Import" / "job001"
    job_dir.mkdir(parents=True)
    _touch_at(job_dir / "job.star", 9999.0)   # would estimate very differently
    project_manager.save_history(tmp_path, [
        {"run_id": "r1", "display_name": "Import", "status": "completed",
         "started_at": 42.0, "ended_at": 100.0, "command": "true",
         "project_dir": str(tmp_path), "cwd": str(job_dir), "job_number": 1},
    ])

    manager = JobRunManager(tmp_path)
    assert manager.backfill_missing_timestamps() == []
    entry = next(h for h in project_manager.load_history(tmp_path) if h["run_id"] == "r1")
    assert entry["started_at"] == 42.0
    assert entry["ended_at"] == 100.0
    assert "timestamp_estimated" not in entry


def test_backfill_leaves_a_still_running_jobs_end_time_alone(tmp_path):
    """A running job genuinely has no end yet -- backfill must only ever
    touch started_at for it, never invent an ended_at."""
    project_manager.init_new_project(tmp_path)
    job_dir = tmp_path / "Import" / "job001"
    job_dir.mkdir(parents=True)
    _touch_at(job_dir / "job.star", 1000.0)
    project_manager.save_history(tmp_path, [
        {"run_id": "r1", "display_name": "Import", "status": STATUS_RUNNING,
         "started_at": None, "ended_at": None, "command": "sleep 30",
         "project_dir": str(tmp_path), "cwd": str(job_dir), "job_number": 1},
    ])

    manager = JobRunManager(tmp_path)
    updated = manager.backfill_missing_timestamps()
    assert len(updated) == 1
    assert updated[0]["started_at"] == 1000.0
    assert updated[0]["ended_at"] is None


def test_backfill_skips_a_run_still_live_in_this_session(tmp_path):
    """A run this session is actively tracking owns its own status/timing --
    backfill must never race or overwrite it via the persisted-history path."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        return await manager.start_subprocess_job(
            "Import", "Import", "echo hi", subdir="Import/job001",
        )

    run = asyncio.run(go())
    # Live run, freshly created -- started_at may not be set yet (the
    # launcher task sets it), but it must not be "fixed" by backfill either
    # way while still tracked in self.runs.
    assert manager.backfill_missing_timestamps() == []


def test_backfill_with_nothing_missing_is_a_noop(tmp_path):
    project_manager.init_new_project(tmp_path)
    project_manager.save_history(tmp_path, [
        {"run_id": "r1", "display_name": "Import", "status": "completed",
         "started_at": 1.0, "ended_at": 2.0, "command": "true",
         "project_dir": str(tmp_path), "cwd": str(tmp_path / "Import" / "job001")},
    ])
    manager = JobRunManager(tmp_path)
    assert manager.backfill_missing_timestamps() == []
