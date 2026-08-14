"""
Tests for job_runner.JobRunManager's project-switching + history-persistence
behavior (see project_manager.py and the "Change Project" feature). Uses
asyncio.run() directly rather than pytest-asyncio, since that plugin isn't a
project dependency and these tests don't need anything fancier.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project_manager
from job_runner import STATUS_COMPLETED, STATUS_FAILED, JobRunManager


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
