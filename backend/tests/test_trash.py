"""
Tests for the Job Recovery / Trash feature (issue #2): project_manager's
move_to_trash/write_trash_sidecar/list_trash/restore_from_trash/
permanently_delete_trash, and JobRunManager.delete_run's new move-to-Trash
behavior + restore_from_trash wrapper.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import project_manager
from job_runner import STATUS_COMPLETED, JobRun, JobRunManager


# ---------------------------------------------------------------------------
# project_manager-level: move_to_trash / write_trash_sidecar / list_trash /
# restore_from_trash / permanently_delete_trash
# ---------------------------------------------------------------------------


def _make_job_dir(tmp_path, rel="CtfFind/job003"):
    job_dir = tmp_path / rel
    job_dir.mkdir(parents=True)
    (job_dir / "micrographs_ctf.star").write_text("dummy output\n")
    return job_dir


def test_move_to_trash_preserves_relative_structure(tmp_path):
    job_dir = _make_job_dir(tmp_path)
    trashed = project_manager.move_to_trash(tmp_path, job_dir)
    assert trashed == tmp_path / "Trash" / "CtfFind" / "job003"
    assert trashed.is_dir()
    assert (trashed / "micrographs_ctf.star").read_text() == "dummy output\n"
    assert not job_dir.exists()


def test_move_to_trash_refuses_outside_project(tmp_path):
    outside = tmp_path.parent / "not_in_project_xyz"
    outside.mkdir(exist_ok=True)
    try:
        with pytest.raises(ValueError, match="outside the project"):
            project_manager.move_to_trash(tmp_path, outside)
    finally:
        outside.rmdir()


def test_move_to_trash_refuses_destination_collision(tmp_path):
    job_dir = _make_job_dir(tmp_path)
    (tmp_path / "Trash" / "CtfFind" / "job003").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        project_manager.move_to_trash(tmp_path, job_dir)


def test_list_trash_finds_a_written_sidecar(tmp_path):
    job_dir = _make_job_dir(tmp_path)
    trashed = project_manager.move_to_trash(tmp_path, job_dir)
    summary = {
        "run_id": "abc123", "internal_name": "CtfFind", "display_name": "CTF Estimation",
        "command": "true", "cwd": str(job_dir), "project_dir": str(tmp_path),
        "job_number": 3, "status": "completed", "field_values": {"foo": "bar"},
    }
    project_manager.write_trash_sidecar(trashed, summary)

    entries = project_manager.list_trash(tmp_path)
    assert len(entries) == 1
    assert entries[0]["run_id"] == "abc123"
    assert entries[0]["trash_id"] == "CtfFind/job003"
    assert entries[0]["field_values"] == {"foo": "bar"}
    assert entries[0]["job_star_available"] is False  # no job.star written in this fixture
    assert "deleted_at" in entries[0]


def test_list_trash_empty_project_returns_empty_list(tmp_path):
    assert project_manager.list_trash(tmp_path) == []


def test_list_trash_always_uses_sidecar_field_values_even_when_job_star_survives(tmp_path):
    """Found in code review: an earlier version preferred job.star's own
    field_values over the sidecar's snapshot when job.star survived the
    move -- but job.star stores every value as a STRING in RELION's own
    "Yes"/"No" boolean convention, unlike the sidecar's natively-typed
    JSON snapshot. Substituting job.star's raw strings silently corrupted
    boolean fields downstream (a "No" string is truthy). field_values
    must always come from the sidecar; job_star_available is reported
    purely as an informational flag, never used to swap values in."""
    job_dir = _make_job_dir(tmp_path)
    (job_dir / "job.star").write_text(
        "\ndata_job\n\n_rlnJobTypeLabel relion.ctffind.ctffind4\n\n"
        "\ndata_joboptions_values\n\nloop_\n"
        "_rlnJobOptionVariable #1\n_rlnJobOptionValue #2\n"
        'fn_micrographs "from_job_star.star"\n'
    )
    trashed = project_manager.move_to_trash(tmp_path, job_dir)
    project_manager.write_trash_sidecar(trashed, {
        "run_id": "abc123", "cwd": str(job_dir), "project_dir": str(tmp_path),
        "field_values": {"fn_micrographs": "from_sidecar_snapshot.star", "do_something": False},
    })

    entries = project_manager.list_trash(tmp_path)
    assert entries[0]["job_star_available"] is True  # informational only
    assert entries[0]["field_values"]["fn_micrographs"] == "from_sidecar_snapshot.star"
    assert entries[0]["field_values"]["do_something"] is False  # still a real bool, not "No"


def test_restore_from_trash_moves_directory_back_and_returns_snapshot(tmp_path):
    job_dir = _make_job_dir(tmp_path)
    original_cwd = str(job_dir)
    trashed = project_manager.move_to_trash(tmp_path, job_dir)
    project_manager.write_trash_sidecar(trashed, {
        "run_id": "abc123", "cwd": original_cwd, "project_dir": str(tmp_path),
        "job_number": 3, "status": "completed",
    })

    restored_summary = project_manager.restore_from_trash(tmp_path, "CtfFind/job003")

    assert restored_summary["run_id"] == "abc123"
    assert "deleted_at" not in restored_summary
    assert Path(original_cwd).is_dir()
    assert (Path(original_cwd) / "micrographs_ctf.star").is_file()
    assert not trashed.exists()


def test_restore_from_trash_raises_on_unknown_trash_id(tmp_path):
    with pytest.raises(ValueError, match="Unknown trash_id"):
        project_manager.restore_from_trash(tmp_path, "CtfFind/job999")


def test_restore_from_trash_refuses_when_original_slot_occupied(tmp_path):
    job_dir = _make_job_dir(tmp_path)
    original_cwd = str(job_dir)
    trashed = project_manager.move_to_trash(tmp_path, job_dir)
    project_manager.write_trash_sidecar(trashed, {
        "run_id": "abc123", "cwd": original_cwd, "project_dir": str(tmp_path),
    })
    # Something new now occupies the original slot.
    Path(original_cwd).mkdir(parents=True)

    with pytest.raises(FileExistsError):
        project_manager.restore_from_trash(tmp_path, "CtfFind/job003")


def test_permanently_delete_trash_refuses_path_traversal_outside_trash(tmp_path):
    """CONFIRMED via code review as a real, exploitable bug in an earlier
    version: permanently_delete_trash's only safety check bounded the
    resolved path to "under project_dir", not specifically "under
    Trash/" -- so a crafted trash_id like "../CtfFind/job003" resolved
    OUTSIDE Trash/ (to project_dir/CtfFind/job003) while still technically
    passing that check, letting a single DELETE /api/trash?trash_id=...
    call permanently rmtree a LIVE, never-trashed job's directory with no
    other gate in the way. Must now be refused outright."""
    live_job_dir = _make_job_dir(tmp_path, "CtfFind/job003")  # never trashed
    # Also create Trash/ itself so the traversal has somewhere to start from.
    (tmp_path / "Trash").mkdir()

    with pytest.raises(ValueError, match="outside Trash"):
        project_manager.permanently_delete_trash(tmp_path, "../CtfFind/job003")

    assert live_job_dir.is_dir()  # untouched
    assert (live_job_dir / "micrographs_ctf.star").is_file()


def test_restore_from_trash_refuses_path_traversal_outside_trash(tmp_path):
    """Same class of bug as the permanently_delete_trash case above --
    restore_from_trash builds the same kind of trash_root/trash_id path
    and was only ACCIDENTALLY protected by also requiring a real sidecar
    file to exist at the resolved location, not by a designed check."""
    (tmp_path / "Trash").mkdir()
    with pytest.raises(ValueError, match="outside Trash"):
        project_manager.restore_from_trash(tmp_path, "../CtfFind/job003")


def test_restore_from_trash_refuses_a_sidecar_whose_cwd_disagrees_with_trash_id(tmp_path):
    """A hand-edited/corrupted/future-schema-mismatched sidecar whose
    recorded "cwd" doesn't match the trash_id it was found under must be
    refused, not silently relocate content into an unrelated slot."""
    job_dir = _make_job_dir(tmp_path, "CtfFind/job003")
    trashed = project_manager.move_to_trash(tmp_path, job_dir)
    project_manager.write_trash_sidecar(trashed, {
        "run_id": "abc123",
        "cwd": str(tmp_path / "MotionCorr" / "job005"),  # disagrees with CtfFind/job003
        "project_dir": str(tmp_path),
    })

    with pytest.raises(ValueError, match="doesn't match trash_id"):
        project_manager.restore_from_trash(tmp_path, "CtfFind/job003")

    assert trashed.exists()  # nothing moved


def test_restore_from_trash_leaves_the_sidecar_alone_if_the_move_would_fail(tmp_path):
    """The sidecar must not be deleted before the move is attempted --
    otherwise a failed move (e.g. destination collision) would orphan the
    job: gone from Trash's listing (no sidecar) but not actually restored
    either. Verified indirectly via the existing collision-refusal test
    (test_restore_from_trash_refuses_when_original_slot_occupied) still
    finding a valid sidecar afterward -- confirms restore didn't delete it
    before discovering the collision."""
    job_dir = _make_job_dir(tmp_path)
    original_cwd = str(job_dir)
    trashed = project_manager.move_to_trash(tmp_path, job_dir)
    project_manager.write_trash_sidecar(trashed, {
        "run_id": "abc123", "cwd": original_cwd, "project_dir": str(tmp_path),
    })
    Path(original_cwd).mkdir(parents=True)  # something now occupies the slot

    with pytest.raises(FileExistsError):
        project_manager.restore_from_trash(tmp_path, "CtfFind/job003")

    # Sidecar (and the trashed directory itself) must still be there --
    # not orphaned by a premature delete before the collision was found.
    assert (trashed / project_manager.TRASH_SIDECAR_FILENAME).is_file()


def test_permanently_delete_trash_removes_one_job(tmp_path):
    job_dir = _make_job_dir(tmp_path, "CtfFind/job003")
    other_job_dir = _make_job_dir(tmp_path, "CtfFind/job004")
    t1 = project_manager.move_to_trash(tmp_path, job_dir)
    t2 = project_manager.move_to_trash(tmp_path, other_job_dir)
    project_manager.write_trash_sidecar(t1, {"run_id": "a", "cwd": str(job_dir), "project_dir": str(tmp_path)})
    project_manager.write_trash_sidecar(t2, {"run_id": "b", "cwd": str(other_job_dir), "project_dir": str(tmp_path)})

    project_manager.permanently_delete_trash(tmp_path, "CtfFind/job003")

    assert not t1.exists()
    assert t2.exists()  # the other trashed job is untouched


def test_permanently_delete_trash_empties_everything_when_no_id_given(tmp_path):
    job_dir = _make_job_dir(tmp_path)
    trashed = project_manager.move_to_trash(tmp_path, job_dir)
    project_manager.write_trash_sidecar(trashed, {"run_id": "a", "cwd": str(job_dir), "project_dir": str(tmp_path)})

    project_manager.permanently_delete_trash(tmp_path, None)

    assert not (tmp_path / "Trash").exists()


def test_permanently_delete_trash_is_a_noop_when_nothing_to_remove(tmp_path):
    project_manager.permanently_delete_trash(tmp_path, None)  # must not raise
    project_manager.permanently_delete_trash(tmp_path, "NoSuchType/job001")  # must not raise


# ---------------------------------------------------------------------------
# JobRunManager-level: delete_run's new move-to-Trash behavior + restore_from_trash
# ---------------------------------------------------------------------------


def test_delete_run_moves_files_to_trash_instead_of_destroying_them(tmp_path):
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job("Import", "Import", "echo hello", subdir="run1")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if run.status == STATUS_COMPLETED:
                break
        return run

    run = asyncio.run(go())
    original_cwd = Path(run.cwd)
    assert original_cwd.is_dir()

    ok, reason = manager.delete_run(run.run_id, remove_files=True)
    assert ok is True
    assert reason == "Moved to Trash"
    assert not original_cwd.exists()  # gone from its original spot...
    trash_entries = project_manager.list_trash(tmp_path)
    assert len(trash_entries) == 1
    assert trash_entries[0]["run_id"] == run.run_id  # ...but recorded in Trash


def test_delete_run_rolls_back_the_move_if_writing_the_sidecar_fails(tmp_path, monkeypatch):
    """Found in code review: if move_to_trash succeeds but
    write_trash_sidecar then fails (disk full, permission error, etc.),
    the directory must NOT be left as an un-sidecared orphan in Trash/
    (invisible to list_trash's own glob, unrecoverable except by wiping
    ALL of Trash) -- it must be moved back to where it was, and delete_run
    must report failure, not silently succeed."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job("Import", "Import", "echo hello", subdir="run1")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if run.status == STATUS_COMPLETED:
                break
        return run

    run = asyncio.run(go())
    original_cwd = Path(run.cwd)

    def boom(*a, **kw):
        raise OSError("disk full (simulated)")
    monkeypatch.setattr(project_manager, "write_trash_sidecar", boom)

    ok, reason = manager.delete_run(run.run_id, remove_files=True)

    assert ok is False
    assert "rolled back" in reason
    assert original_cwd.is_dir()  # back where it started
    assert project_manager.list_trash(tmp_path) == []  # nothing orphaned in Trash


def test_restore_from_trash_via_job_run_manager_re_adds_history(tmp_path):
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job("Import", "Import", "echo hello", subdir="run1")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if run.status == STATUS_COMPLETED:
                break
        return run

    run = asyncio.run(go())
    run_id, original_cwd = run.run_id, run.cwd
    manager.delete_run(run_id, remove_files=True)
    assert manager.get(run_id) is None
    assert run_id not in {h.get("run_id") for h in project_manager.load_history(tmp_path)}

    trash_id = project_manager.list_trash(tmp_path)[0]["trash_id"]
    restored = manager.restore_from_trash(trash_id)

    assert restored is not None
    assert restored.run_id == run_id
    assert restored.cwd == original_cwd
    assert Path(original_cwd).is_dir()
    assert manager.get(run_id) is not None
    assert run_id in {h.get("run_id") for h in project_manager.load_history(tmp_path)}
    assert project_manager.list_trash(tmp_path) == []  # sidecar consumed


def test_restore_from_trash_via_job_run_manager_returns_none_for_unknown_id(tmp_path):
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)
    assert manager.restore_from_trash("NoSuchType/job999") is None


def test_job_number_reuse_after_trashing_makes_a_later_restore_collide(tmp_path):
    """Confirmed real, not just theoretical, while writing these tests:
    JobRunManager._next_job_number derives the next number from
    run_history.json + on-disk directories, both of which stop counting a
    trashed job immediately -- so trashing job001 and then starting a
    NEW job of the same type gives that new job the number "1" again.
    Restoring the ORIGINAL trashed job001 must then collide loudly with
    the new job now legitimately living there, not silently overwrite it."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def run_one():
        run = await manager.start_subprocess_job("Import", "Import", "echo hello", subdir="run1")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if run.status == STATUS_COMPLETED:
                break
        return run

    first = asyncio.run(run_one())
    assert first.job_number == 1
    manager.delete_run(first.run_id, remove_files=True)

    second = asyncio.run(run_one())
    assert second.job_number == 1  # the freed slot was reused
    assert second.cwd == first.cwd  # same directory, now a different job

    trash_id = project_manager.list_trash(tmp_path)[0]["trash_id"]
    with pytest.raises(FileExistsError):
        manager.restore_from_trash(trash_id)
    # The second (current, live) job's directory must be untouched.
    assert Path(second.cwd).is_dir()
    assert manager.get(second.run_id) is not None


# ---------------------------------------------------------------------------
# Deleting a pipeline-synced job: relion_pipeliner has no CLI verb to remove
# a process from default_pipeline.star (PipeLine::deleteNodesAndProcesses is
# GUI-only -- see pipeline_bridge.py's module docstring), so the orphaned
# process is hidden locally instead of left to reappear as a "relion:jobNNN"
# ghost row. See project_manager.load_relion_deleted_job_numbers's docstring
# for why keying this by RELION's own job_number is safe.
# ---------------------------------------------------------------------------

_FAKE_PIPELINE_STAR = """
# version 30001

data_pipeline_general

_rlnPipeLineJobCounter                      5


# version 30001

data_pipeline_processes

loop_
_rlnPipeLineProcessName #1
_rlnPipeLineProcessAlias #2
_rlnPipeLineProcessTypeLabel #3
_rlnPipeLineProcessStatusLabel #4
CtfFind/job005/          None            relion.ctffind.ctffind4  Succeeded
"""


def _make_pipeline_synced_run(tmp_path):
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)
    job_dir = tmp_path / "CtfFind" / "job005"
    job_dir.mkdir(parents=True)
    (job_dir / "micrographs_ctf.star").write_text("dummy output\n")
    (tmp_path / "default_pipeline.star").write_text(_FAKE_PIPELINE_STAR)

    run = JobRun(
        run_id="abc123", internal_name="CtfFind", display_name="CTF Estimation",
        command="true", cwd=str(job_dir), project_dir=str(tmp_path),
        job_number=5, status=STATUS_COMPLETED, pipeline_registered=True,
    )
    manager.runs[run.run_id] = run
    project_manager.save_history(tmp_path, [run.to_summary()])
    return manager, run


def test_delete_run_hides_the_orphaned_relion_pipeline_ghost_row(tmp_path):
    manager, run = _make_pipeline_synced_run(tmp_path)

    # Before delete: this app's own tracked entry already suppresses the
    # would-be duplicate from default_pipeline.star (own_job_numbers).
    assert [r["run_id"] for r in manager.list_runs()] == ["abc123"]

    ok, reason = manager.delete_run(run.run_id, remove_files=False)
    assert ok is True

    # After delete: no "relion:job005" ghost row, even though the process
    # entry is still sitting untouched in default_pipeline.star.
    assert manager.list_runs() == []
    assert 5 in project_manager.load_relion_deleted_job_numbers(tmp_path)
    assert "5" in (tmp_path / "default_pipeline.star").read_text()  # untouched


def test_delete_run_does_not_hide_a_job_that_was_never_pipeline_registered(tmp_path):
    """Only a job_number known to be RELION's own monotonic pipeline
    counter value (pipeline_registered) is safe to hide -- this app's own
    locally-reused numbering scheme must never feed this set (see
    load_relion_deleted_job_numbers's docstring on why reuse would then be
    unsafe)."""
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job("Import", "Import", "echo hello", subdir="run1")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if run.status == STATUS_COMPLETED:
                break
        return run

    run = asyncio.run(go())
    assert run.pipeline_registered is False  # no relion_pipeliner in this test env
    manager.delete_run(run.run_id, remove_files=True)
    assert project_manager.load_relion_deleted_job_numbers(tmp_path) == set()


def test_restore_from_trash_unhides_the_relion_pipeline_row(tmp_path):
    manager, run = _make_pipeline_synced_run(tmp_path)
    manager.delete_run(run.run_id, remove_files=True)
    assert 5 in project_manager.load_relion_deleted_job_numbers(tmp_path)

    trash_id = project_manager.list_trash(tmp_path)[0]["trash_id"]
    restored = manager.restore_from_trash(trash_id)

    assert restored is not None
    assert 5 not in project_manager.load_relion_deleted_job_numbers(tmp_path)
    assert [r["run_id"] for r in manager.list_runs()] == ["abc123"]


def test_delete_run_refuses_a_running_job_before_ever_trashing_it(tmp_path):
    """A still-running job must be refused (existing behavior) -- and, now
    that Delete moves files, must not partially move anything either.

    Everything stays inside ONE asyncio.run() call, never returning while
    run.task is still alive un-awaited -- asyncio.run()'s own teardown
    cancels any still-pending task, which (a pre-existing, unrelated
    quirk of _run_subprocess, not something this feature touches) can
    race a live subprocess's own real completion in a way that makes
    "still running" a moving target across separate asyncio.run() calls.
    Matches the same pattern test_main_endpoints.py's own
    test_abort_a_running_job already uses for the identical reason.
    """
    project_manager.init_new_project(tmp_path)
    manager = JobRunManager(tmp_path)

    async def go():
        run = await manager.start_subprocess_job("Import", "Import", "sleep 5", subdir="run1")
        for _ in range(150):
            run2 = manager.get(run.run_id)
            if run2 is not None and run2.proc is not None:
                break
            await asyncio.sleep(0.02)
        assert run2 is not None and run2.proc is not None, "process never spawned"

        ok, reason = manager.delete_run(run.run_id, remove_files=True)
        assert ok is False
        assert "still running" in reason
        assert Path(run.cwd).is_dir()
        assert project_manager.list_trash(tmp_path) == []

        await manager.abort_run(run.run_id)

    asyncio.run(go())
