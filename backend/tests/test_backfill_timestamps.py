"""
Tests for backfill_timestamps.py's main() CLI entry point.

Why this exists: the script wraps JobRunManager.backfill_missing_timestamps()
(tested in test_job_runner.py, e.g. test_backfill_never_overwrites_a_value_
that_is_already_recorded and test_backfill_leaves_a_still_running_jobs_end_
time_alone) but the CLI script itself -- its argument parsing, --dry-run
branch, "Nothing to backfill." messaging, and error handling for a bad
project_dir -- had zero coverage of its own, even though the manager method
it delegates to was tested. A bug in main()'s own code (e.g. a typo in the
dry-run candidate-selection logic, which duplicates the manager's selection
logic rather than calling it) would have shipped silently.

Calls backfill_timestamps.main([...]) directly rather than via subprocess,
matching this project's convention of testing CLI-adjacent code as a plain
function call for speed and determinism.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backfill_timestamps
import project_manager


def _touch_at(path, mtime):
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_dry_run_reports_but_writes_nothing(tmp_path, capsys):
    """--dry-run must mirror backfill_missing_timestamps' own candidate
    selection closely enough that its report matches what a real run would
    touch, but must never call save_history -- run_history.json on disk
    should be byte-for-byte unchanged afterward."""
    project_manager.init_new_project(tmp_path)
    job_dir = tmp_path / "Import" / "job001"
    job_dir.mkdir(parents=True)
    _touch_at(job_dir / "job.star", 1000.0)
    _touch_at(job_dir / "RELION_JOB_EXIT_SUCCESS", 1200.0)
    project_manager.save_history(tmp_path, [
        {"run_id": "r1", "job_name": "Import", "display_name": "Import",
         "status": "completed", "started_at": None, "ended_at": None,
         "command": "true", "project_dir": str(tmp_path), "cwd": str(job_dir),
         "job_number": 1},
    ])
    history_path = tmp_path / project_manager.MARKER_DIRNAME / project_manager.HISTORY_FILENAME
    before = history_path.read_text()

    rc = backfill_timestamps.main([str(tmp_path), "--dry-run"])

    assert rc == 0
    after = history_path.read_text()
    assert after == before   # nothing written to disk

    out = capsys.readouterr().out
    assert "Would check 1 run(s) missing timing data:" in out
    assert "Import" in out
    assert "started_at=None, ended_at=None" in out

    # Confirm the entry genuinely still has no timing recorded.
    entry = project_manager.load_history(tmp_path)[0]
    assert entry["started_at"] is None
    assert entry["ended_at"] is None
    assert "timestamp_estimated" not in entry


def test_real_run_writes_estimated_timestamps(tmp_path, capsys):
    """A real (non-dry-run) invocation must actually persist the estimated
    started_at/ended_at into run_history.json, not just print them."""
    project_manager.init_new_project(tmp_path)
    job_dir = tmp_path / "Import" / "job001"
    job_dir.mkdir(parents=True)
    _touch_at(job_dir / "job.star", 1000.0)
    _touch_at(job_dir / "RELION_JOB_EXIT_SUCCESS", 1200.0)
    project_manager.save_history(tmp_path, [
        {"run_id": "r1", "job_name": "Import", "display_name": "Import",
         "status": "completed", "started_at": None, "ended_at": None,
         "command": "true", "project_dir": str(tmp_path), "cwd": str(job_dir),
         "job_number": 1},
    ])

    rc = backfill_timestamps.main([str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Backfilled 1 run(s):" in out
    assert "Import" in out
    assert "(estimated)" in out

    entry = project_manager.load_history(tmp_path)[0]
    assert entry["started_at"] == 1000.0
    assert entry["ended_at"] == 1200.0
    assert entry["timestamp_estimated"] is True


def test_nothing_to_backfill_dry_run(tmp_path, capsys):
    """When every entry already has full timing data, --dry-run must say so
    plainly and touch nothing."""
    project_manager.init_new_project(tmp_path)
    project_manager.save_history(tmp_path, [
        {"run_id": "r1", "display_name": "Import", "status": "completed",
         "started_at": 1.0, "ended_at": 2.0, "command": "true",
         "project_dir": str(tmp_path), "cwd": str(tmp_path / "Import" / "job001")},
    ])
    history_path = tmp_path / project_manager.MARKER_DIRNAME / project_manager.HISTORY_FILENAME
    before = history_path.read_text()

    rc = backfill_timestamps.main([str(tmp_path), "--dry-run"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "Nothing to backfill."
    assert history_path.read_text() == before


def test_nothing_to_backfill_real_run(tmp_path, capsys):
    """Same as above but for a real run: prints the same message and writes
    nothing, since backfill_missing_timestamps() returns an empty list."""
    project_manager.init_new_project(tmp_path)
    project_manager.save_history(tmp_path, [
        {"run_id": "r1", "display_name": "Import", "status": "completed",
         "started_at": 1.0, "ended_at": 2.0, "command": "true",
         "project_dir": str(tmp_path), "cwd": str(tmp_path / "Import" / "job001")},
    ])
    history_path = tmp_path / project_manager.MARKER_DIRNAME / project_manager.HISTORY_FILENAME
    before = history_path.read_text()

    rc = backfill_timestamps.main([str(tmp_path)])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "Nothing to backfill."
    assert history_path.read_text() == before


def test_nonexistent_project_dir_reports_clean_error(tmp_path, capsys):
    """A bad project_dir must produce a nonzero exit code and a clear
    stderr message -- not a raw traceback from deeper inside JobRunManager
    or project_manager.load_history."""
    bad_dir = tmp_path / "does-not-exist"

    rc = backfill_timestamps.main([str(bad_dir)])

    assert rc != 0
    captured = capsys.readouterr()
    assert "Not a directory" in captured.err
    assert str(bad_dir) in captured.err
    assert captured.out == ""
