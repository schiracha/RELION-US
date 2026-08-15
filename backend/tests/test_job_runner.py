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


def _wait(run):
    async def go():
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status in (STATUS_COMPLETED, STATUS_FAILED):
                break
        return run
    return asyncio.run(go())


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
    assert getattr(r2, "_rewrite_note", None) and "job002" in r2._rewrite_note
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
