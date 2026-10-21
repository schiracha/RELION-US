"""
Jobs must survive a RELION-US restart. Their stdout/stderr used to be pipes
owned by the backend, so killing it killed every running job at its next write.
They now go straight to run.out / run.err, and a restarted backend takes still-
running jobs back (adopt_running_jobs) and records how they really ended.
"""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_runner
import main
import project_manager
from job_runner import (
    STATUS_ABORTED, STATUS_COMPLETED, STATUS_FAILED, STATUS_RUNNING, JobRunManager,
)

BACKEND = Path(__file__).resolve().parent.parent
TERMINAL = (STATUS_COMPLETED, STATUS_FAILED, STATUS_ABORTED)


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(job_runner, "ADOPT_POLL_S", 0.1)


@pytest.fixture
def project(tmp_path):
    project_manager.init_new_project(tmp_path)
    return tmp_path


async def until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def start_in_a_backend_that_then_dies(project, command):
    """A separate Python process (a "backend") starts the job through the real
    launcher and is then killed outright -- no clean shutdown. Returns run_id."""
    script = (
        "import asyncio, os, sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "from pathlib import Path\n"
        "from job_runner import JobRunManager\n"
        "async def go():\n"
        f"    m = JobRunManager(Path({str(project)!r}))\n"
        f"    run = await m.start_subprocess_job('Import', 'Import', {command!r}, subdir='Import/job001')\n"
        "    for _ in range(200):\n"
        "        await asyncio.sleep(0.05)\n"
        "        if run.pid and len(run.stdout_lines) >= 1: break\n"
        "    print(run.run_id, flush=True)\n"
        "    os._exit(0)\n"
        "asyncio.run(go())\n"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip().splitlines()[-1]


def test_output_goes_to_the_job_files_and_is_still_streamed(project):
    manager = JobRunManager(project)

    async def go():
        queue = asyncio.Queue()
        run = await manager.start_subprocess_job(
            "Import", "Import", "echo out1; echo err1 1>&2; printf 'a\\rb\\rc\\n'", subdir="Import/job001")
        run.subscribers.append(queue)
        assert await until(lambda: run.status in TERMINAL)
        return run

    run = asyncio.run(go())
    job_dir = Path(run.cwd)
    assert (job_dir / "run.out").read_text().splitlines()[0] == "out1"
    assert "err1" in (job_dir / "run.err").read_text()
    assert run.stdout_lines == ["out1", "a", "b", "c"]          # CR-separated progress stays separate lines
    assert run.stderr_lines == ["err1"]
    assert run.status == STATUS_COMPLETED and run.exit_code == 0


def test_the_exit_status_is_recorded_for_a_restarted_backend(project):
    manager = JobRunManager(project)

    async def go():
        run = await manager.start_subprocess_job("Import", "Import", "exit 7", subdir="Import/job001")
        assert await until(lambda: run.status in TERMINAL)
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_FAILED and run.exit_code == 7
    assert JobRunManager._read_exit_file(project, run.run_id) == 7


def test_overwrite_starts_the_files_afresh_and_streams_only_its_own_output(project):
    manager = JobRunManager(project)

    async def go():
        first = await manager.start_subprocess_job("Import", "Import", "echo first", subdir="Import/job001")
        assert await until(lambda: first.status in TERMINAL)
        second = await manager.start_subprocess_job(
            "Import", "Import", "echo second", subdir="Import/job001", overwrite_run_id=first.run_id)
        assert await until(lambda: second.status in TERMINAL)
        return second

    run = asyncio.run(go())
    assert (Path(run.cwd) / "run.out").read_text().splitlines() == ["second"]    # the Overwrite cleared "first"
    assert [line for line in run.stdout_lines if not line.startswith("[RELION-US]")] == ["second"]


def test_a_job_keeps_running_when_its_backend_dies_and_is_taken_back(project):
    run_id = start_in_a_backend_that_then_dies(project, "echo started; sleep 1.5; echo finishing; exit 3")
    entry = next(h for h in project_manager.load_history(project) if h["run_id"] == run_id)
    assert entry["status"] == STATUS_RUNNING and entry["pid"]
    os.kill(entry["pid"], 0)                       # the job outlived the backend that started it

    async def go():
        manager = JobRunManager(project)
        assert await manager.adopt_running_jobs() == [run_id]
        run = manager.get(run_id)
        assert run.adopted and run.status == STATUS_RUNNING
        assert await until(lambda: any("started" in line for line in run.stdout_lines))   # earlier output replayed
        assert await until(lambda: run.status in TERMINAL)
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_FAILED and run.exit_code == 3          # its REAL outcome
    assert "finishing" in run.stdout_lines                             # and its later output followed live
    stored = next(h for h in project_manager.load_history(project) if h["run_id"] == run_id)
    assert stored["status"] == STATUS_FAILED and stored["exit_code"] == 3 and stored["ended_at"]


def test_a_job_that_finishes_fine_while_nobody_watches_is_recorded_completed(project):
    run_id = start_in_a_backend_that_then_dies(project, "echo started; sleep 0.5; echo done")
    time.sleep(1.5)                                 # it ends with no backend running at all

    async def go():
        manager = JobRunManager(project)
        await manager.adopt_running_jobs()
        run = manager.get(run_id)
        assert await until(lambda: run.status in TERMINAL)
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_COMPLETED and run.exit_code == 0


def test_an_adopted_job_can_be_aborted(project):
    run_id = start_in_a_backend_that_then_dies(project, "echo started; sleep 60")
    pid = next(h for h in project_manager.load_history(project) if h["run_id"] == run_id)["pid"]

    async def go():
        manager = JobRunManager(project)
        await manager.adopt_running_jobs()
        assert await manager.abort_run(run_id) is True
        run = manager.get(run_id)
        assert await until(lambda: not Path(f"/proc/{pid}").exists())
        await until(lambda: run.task.done())
        return run

    run = asyncio.run(go())
    assert run.status == STATUS_ABORTED


def _entry(project, run_id="r1", **extra):
    job_dir = project / "Import" / "job001"
    job_dir.mkdir(parents=True, exist_ok=True)
    return {"run_id": run_id, "internal_name": "Import", "display_name": "Import", "command": "x --o Import/job001/",
            "status": STATUS_RUNNING, "job_number": 1, "job_name": "job001", "cwd": str(job_dir),
            "project_dir": str(project), "pid": 2_000_000_000, "started_at": 1.0, **extra}


def _adopt(project):
    async def go():
        manager = JobRunManager(project)
        taken = await manager.adopt_running_jobs()
        for run_id in taken:
            run = manager.get(run_id)
            assert await until(lambda: run.status in TERMINAL)
        return manager, taken

    return asyncio.run(go())


def test_a_dead_job_with_no_exit_status_is_recorded_failed_with_an_explanation(project):
    project_manager.save_history(project, [_entry(project)])
    manager, taken = _adopt(project)
    run = manager.get("r1")
    assert taken == ["r1"] and run.status == STATUS_FAILED
    assert any("left no exit status" in line for line in run.stderr_lines)


def test_a_dead_job_uses_the_exit_status_file_or_relions_own_markers(project):
    project_manager.save_history(project, [_entry(project)])
    exit_file = JobRunManager._exit_file(project, "r1")
    exit_file.parent.mkdir(parents=True)
    exit_file.write_text("0")
    manager, _ = _adopt(project)
    assert manager.get("r1").status == STATUS_COMPLETED

    other = project / "Import2"
    project_manager.init_new_project(other)
    entry = _entry(other, run_id="r2")
    project_manager.save_history(other, [entry])
    (Path(entry["cwd"]) / "RELION_JOB_EXIT_SUCCESS").touch()
    manager2, _ = _adopt(other)
    assert manager2.get("r2").status == STATUS_COMPLETED


def test_adoption_leaves_alone_what_it_cannot_or_should_not_take(project):
    project_manager.save_history(project, [
        _entry(project, "done", status=STATUS_COMPLETED),
        _entry(project, "nopid", pid=None),
        _entry(project, "slurm", slurm_job_id="123"),
        _entry(project, "inproc", command="<in-process: picker>"),
        _entry(project, "waiting", status="queued", waiting=True),
    ])
    assert asyncio.run(JobRunManager(project).adopt_running_jobs()) == []


def test_adoption_does_not_take_a_run_twice(project):
    project_manager.save_history(project, [_entry(project)])

    async def go():
        manager = JobRunManager(project)
        first = await manager.adopt_running_jobs()
        second = await manager.adopt_running_jobs()
        await until(lambda: manager.get("r1").status in TERMINAL)
        return first, second

    assert asyncio.run(go()) == (["r1"], [])


def test_deleting_a_run_removes_its_exit_status_file(project):
    manager = JobRunManager(project)

    async def go():
        run = await manager.start_subprocess_job("Import", "Import", "true", subdir="Import/job001")
        assert await until(lambda: run.status in TERMINAL)
        return run

    run = asyncio.run(go())
    assert JobRunManager._exit_file(project, run.run_id).exists()
    manager.delete_run(run.run_id, remove_files=False)
    assert not JobRunManager._exit_file(project, run.run_id).exists()


def test_the_running_app_takes_jobs_back_on_its_own(tmp_path, monkeypatch):
    """The lifespan loop: no call needed from anywhere."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(main, "ADOPT_INTERVAL_S", 0.1)
    project = tmp_path / "project"
    project_manager.init_new_project(project)
    project_manager.save_history(project, [_entry(project)])
    main.run_manager.set_project_dir(project)
    with TestClient(main.app) as client:
        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            status = client.get("/api/runs/r1").json().get("status")
            if status == STATUS_FAILED:
                break
            time.sleep(0.1)
    assert status == STATUS_FAILED


def test_a_hand_written_command_without_the_job_directory_is_still_recognised_as_alive(project):
    """The liveness check used to look for the job directory in the process's
    command line; a command that never names it (the one above is such a
    command) would have been taken for dead and recorded as failed while it
    ran. The launcher's wrapper now carries a per-run path."""
    run_id = start_in_a_backend_that_then_dies(project, "echo started; sleep 1; echo done")
    entry = next(h for h in project_manager.load_history(project) if h["run_id"] == run_id)
    assert "Import/job001" not in entry["command"]
    assert JobRunManager._pid_matches_persisted_run(entry["pid"], entry) is True
    assert JobRunManager._pid_matches_persisted_run(entry["pid"], {**entry, "run_id": "someone-else", "cwd": str(project / "X/job9")}) is False
    time.sleep(1.6)
