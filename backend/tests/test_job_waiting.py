"""
Issue #78 (a job that waits for other jobs to finish before starting) and the
change-notification plumbing behind issue #79 (live Command Center updates).
asyncio.run() directly, like test_job_runner.py.
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_runner
import project_manager
from job_runner import (
    STATUS_ABORTED, STATUS_COMPLETED, STATUS_FAILED, STATUS_QUEUED, STATUS_RUNNING,
    JobRunManager,
)

TERMINAL = (STATUS_COMPLETED, STATUS_FAILED, STATUS_ABORTED)


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(job_runner, "WAIT_POLL_INTERVAL_S", 0.05)


@pytest.fixture
def manager(tmp_path):
    project_manager.init_new_project(tmp_path)
    return JobRunManager(tmp_path)


async def until(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def start(manager, command, **kw):
    return await manager.start_subprocess_job("Import", "Import", command, subdir="x", **kw)


def test_waits_for_success_then_runs(manager, tmp_path):
    async def go():
        a = await start(manager, "sleep 0.5")
        b = await start(manager, "touch b_ran", wait_for=[a.run_id])
        assert b.status == STATUS_QUEUED and b.waiting and b.wait_for == [a.run_id]
        assert not (tmp_path / "b_ran").exists()
        assert await until(lambda: b.status in TERMINAL)
        return a, b

    a, b = asyncio.run(go())
    assert a.status == STATUS_COMPLETED and b.status == STATUS_COMPLETED
    assert (tmp_path / "b_ran").exists()
    assert b.started_at >= a.ended_at
    assert any("Waited for" in line for line in b.stdout_lines)
    assert b.waiting is False


def test_failed_dependency_cancels_a_success_only_job(manager, tmp_path):
    async def go():
        a = await start(manager, "sleep 0.2; exit 3")
        b = await start(manager, "touch b_ran", wait_for=[a.run_id])
        assert await until(lambda: b.status in TERMINAL)
        return a, b

    a, b = asyncio.run(go())
    assert a.status == STATUS_FAILED
    assert b.status == STATUS_ABORTED and b.waiting is False
    assert not (tmp_path / "b_ran").exists() and b.pid is None
    assert any("Cancelled before starting" in line for line in b.stderr_lines)


def test_any_mode_runs_after_a_failure(manager, tmp_path):
    async def go():
        a = await start(manager, "exit 1")
        b = await start(manager, "touch b_ran", wait_for=[a.run_id], wait_mode="any")
        assert await until(lambda: b.status in TERMINAL)
        return a, b

    a, b = asyncio.run(go())
    assert a.status == STATUS_FAILED and b.status == STATUS_COMPLETED
    assert (tmp_path / "b_ran").exists()


def test_waits_for_several_jobs(manager, tmp_path):
    async def go():
        a = await start(manager, "sleep 0.3")
        b = await start(manager, "sleep 0.6")
        c = await start(manager, "touch c_ran", wait_for=[a.run_id, b.run_id])
        await until(lambda: a.status in TERMINAL)
        assert c.status == STATUS_QUEUED          # b is still going
        assert await until(lambda: c.status in TERMINAL)
        return b, c

    b, c = asyncio.run(go())
    assert c.status == STATUS_COMPLETED and c.started_at >= b.ended_at


def test_dependency_that_already_completed_does_not_hold_the_job(manager, tmp_path):
    async def go():
        a = await start(manager, "true")
        assert await until(lambda: a.status in TERMINAL)
        b = await start(manager, "touch b_ran", wait_for=[a.run_id])
        assert await until(lambda: b.status in TERMINAL)
        return b

    assert asyncio.run(go()).status == STATUS_COMPLETED


def test_chain_runs_in_order(manager, tmp_path):
    async def go():
        a = await start(manager, "sleep 0.3; echo a >> order")
        b = await start(manager, "sleep 0.2; echo b >> order", wait_for=[a.run_id])
        c = await start(manager, "echo c >> order", wait_for=[b.run_id])
        assert await until(lambda: c.status in TERMINAL)

    asyncio.run(go())
    assert (tmp_path / "order").read_text().split() == ["a", "b", "c"]


def test_abort_while_waiting_never_starts_the_command(manager, tmp_path):
    async def go():
        a = await start(manager, "sleep 1")
        b = await start(manager, "touch b_ran", wait_for=[a.run_id])
        assert await manager.abort_run(b.run_id) is True
        assert b.status == STATUS_ABORTED
        await until(lambda: a.status in TERMINAL)
        await asyncio.sleep(0.3)              # give a would-be start time to happen
        return b

    b = asyncio.run(go())
    assert b.status == STATUS_ABORTED and b.waiting is False
    assert not (tmp_path / "b_ran").exists() and b.pid is None


def test_waiting_job_is_abortable_and_reported_queued(manager):
    async def go():
        a = await start(manager, "sleep 0.5")
        b = await start(manager, "true", wait_for=[a.run_id])
        summary = b.to_summary()
        await manager.abort_run(b.run_id)
        await until(lambda: a.status in TERMINAL)
        return summary

    s = asyncio.run(go())
    assert s["status"] == STATUS_QUEUED and s["waiting"] is True and s["abortable"] is True
    assert s["wait_mode"] == "success" and len(s["wait_for"]) == 1


def test_wait_state_is_persisted_for_the_command_center(manager, tmp_path):
    async def go():
        a = await start(manager, "sleep 0.4")
        b = await start(manager, "true", wait_for=[a.run_id])
        entry = next(h for h in project_manager.load_history(tmp_path) if h["run_id"] == b.run_id)
        await until(lambda: b.status in TERMINAL)
        return a, entry

    a, entry = asyncio.run(go())
    assert entry["status"] == STATUS_QUEUED and entry["waiting"] is True
    assert entry["wait_for"] == [a.run_id]


def test_unknown_dependency_is_rejected_before_a_job_number_is_used(manager):
    async def go():
        with pytest.raises(ValueError, match="no such job"):
            await start(manager, "true", wait_for=["nope"])

    asyncio.run(go())
    assert manager.runs == {}


def test_success_mode_rejects_a_dependency_that_already_failed(manager):
    async def go():
        a = await start(manager, "exit 2")
        assert await until(lambda: a.status in TERMINAL)
        with pytest.raises(ValueError, match="already failed"):
            await start(manager, "true", wait_for=[a.run_id])
        b = await start(manager, "true", wait_for=[a.run_id], wait_mode="any")   # allowed
        assert await until(lambda: b.status in TERMINAL)
        return b

    assert asyncio.run(go()).status == STATUS_COMPLETED


def test_bad_wait_mode_rejected(manager):
    async def go():
        with pytest.raises(ValueError, match="wait_mode"):
            await start(manager, "true", wait_for=["x"], wait_mode="whenever")

    asyncio.run(go())


def test_overwrite_cannot_wait_on_itself_or_create_a_cycle(manager):
    async def go():
        a = await start(manager, "true")
        assert await until(lambda: a.status in TERMINAL)
        with pytest.raises(ValueError, match="wait for itself"):
            await start(manager, "true", overwrite_run_id=a.run_id, wait_for=[a.run_id])
        # b waits on a long job c; overwriting c to wait on b would deadlock
        c = await start(manager, "sleep 1")
        b = await start(manager, "true", wait_for=[c.run_id])
        await manager.abort_run(c.run_id)
        # c is now aborted -> overwrite it (allowed) while waiting on b, which waits on c
        assert await until(lambda: c.status in TERMINAL)
        b2 = await start(manager, "sleep 0.5")
        d = await start(manager, "true", wait_for=[b2.run_id])
        with pytest.raises(ValueError, match="cycle"):
            await start(manager, "true", overwrite_run_id=b2.run_id, wait_for=[d.run_id])
        await manager.abort_run(d.run_id)
        await until(lambda: b2.status in TERMINAL)
        await manager.abort_run(b.run_id)

    asyncio.run(go())


# --------------------------------------------------------------------------
# Change notifications (issue #79)
# --------------------------------------------------------------------------


def test_status_changes_are_pushed_to_event_subscribers(manager):
    async def go():
        queue = asyncio.Queue(maxsize=100)
        manager.event_subscribers.append(queue)
        run = await start(manager, "true")
        assert await until(lambda: run.status in TERMINAL)
        await asyncio.sleep(0.1)
        msgs = []
        while not queue.empty():
            msgs.append(queue.get_nowait())
        return run, msgs

    run, msgs = asyncio.run(go())
    assert all(m["type"] == "runs_changed" for m in msgs)
    statuses = [m["status"] for m in msgs if m["run_id"] == run.run_id]
    assert statuses[0] in ("pending", "running") and statuses[-1] == STATUS_COMPLETED


def test_alias_and_note_edits_notify(manager):
    async def go():
        run = await start(manager, "true")
        assert await until(lambda: run.status in TERMINAL)
        queue = asyncio.Queue(maxsize=100)
        manager.event_subscribers.append(queue)
        manager.set_alias(run.run_id, "renamed")
        manager.set_note(run.run_id, "a note")
        return queue.qsize()

    assert asyncio.run(go()) >= 2


def test_a_full_subscriber_queue_never_blocks_a_job(manager):
    async def go():
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait({"type": "runs_changed"})        # already full
        manager.event_subscribers.append(queue)
        run = await start(manager, "true")
        assert await until(lambda: run.status in TERMINAL)
        return run

    assert asyncio.run(go()).status == STATUS_COMPLETED


def test_notify_outside_an_event_loop_is_a_noop(manager):
    manager.event_subscribers.append(asyncio.Queue())
    manager._notify_changed("x", "running")      # no running loop: must not raise
    assert manager.event_subscribers[0].qsize() == 0


def test_a_waiting_job_orphaned_by_a_restart_is_marked_aborted(tmp_path):
    """The wait is an in-memory task: after a backend restart nothing would
    ever start it, so it must not be left showing 'queued' forever."""
    project_manager.init_new_project(tmp_path)
    project_manager.save_history(tmp_path, [{
        "run_id": "r1", "internal_name": "Import", "display_name": "Import", "command": "true",
        "status": "queued", "waiting": True, "wait_for": ["r0"], "wait_mode": "success",
        "job_number": 2, "cwd": str(tmp_path / "Import" / "job002"), "project_dir": str(tmp_path),
    }])
    fresh_manager = JobRunManager(tmp_path)          # a "restarted" backend: no runs in memory
    runs = fresh_manager.list_runs()
    entry = next(r for r in runs if r["run_id"] == "r1")
    assert entry["status"] == STATUS_ABORTED and entry["waiting"] is False and entry["ended_at"]
    stored = project_manager.load_history(tmp_path)[0]
    assert stored["status"] == STATUS_ABORTED       # persisted, not just reported


def test_a_live_waiting_job_is_not_reconciled_away(manager):
    async def go():
        a = await start(manager, "sleep 0.5")
        b = await start(manager, "true", wait_for=[a.run_id])
        listed = next(r for r in manager.list_runs() if r["run_id"] == b.run_id)
        await until(lambda: b.status in TERMINAL)
        return listed

    listed = asyncio.run(go())
    assert listed["status"] == STATUS_QUEUED and listed["waiting"] is True


# --------------------------------------------------------------------------
# Overwrite must not inherit the previous attempt's exit markers
# --------------------------------------------------------------------------

MARKERS = ("RELION_JOB_EXIT_SUCCESS", "RELION_JOB_EXIT_FAILURE",
           "RELION_JOB_EXIT_ABORTED", "RELION_JOB_ABORT_NOW")


def test_overwrite_clears_exit_markers_left_by_the_previous_attempt(manager, tmp_path):
    """A stale RELION_JOB_EXIT_FAILURE from a failed first attempt made RELION
    treat the overwritten, still-running job as failed (it checks the marker
    files of every Running process). RELION itself deletes them at start."""
    async def go():
        first = await start(manager, "exit 1")
        assert await until(lambda: first.status in TERMINAL)
        job_dir = Path(first.cwd)
        for name in MARKERS:
            (job_dir / name).touch()
        second = await start(manager, "sleep 0.6", overwrite_run_id=first.run_id)
        present_while_running = [n for n in MARKERS if (job_dir / n).exists()]
        assert await until(lambda: second.status in TERMINAL)
        return present_while_running

    assert asyncio.run(go()) == []


def test_a_fresh_run_does_not_touch_markers_of_other_jobs(manager, tmp_path):
    async def go():
        other = await start(manager, "true")
        assert await until(lambda: other.status in TERMINAL)
        marker = Path(other.cwd) / "RELION_JOB_EXIT_SUCCESS"
        marker.touch()
        fresh = await start(manager, "true")
        assert await until(lambda: fresh.status in TERMINAL)
        return marker.exists()

    assert asyncio.run(go()) is True


# --------------------------------------------------------------------------
# LogFollower: tailing a job's saved run.out / run.err
# --------------------------------------------------------------------------

from job_runner import LogFollower  # noqa: E402


def test_log_follower_missing_file_is_empty(tmp_path):
    f = LogFollower(tmp_path / "nope.out")
    assert f.initial() == ([], False) and f.new_lines() == []


def test_log_follower_returns_the_whole_small_file_then_only_new_lines(tmp_path):
    p = tmp_path / "run.out"
    p.write_text("a\nb\n")
    f = LogFollower(p)
    assert f.initial() == (["a", "b"], False)
    assert f.new_lines() == []
    with open(p, "a") as fh:
        fh.write("c\nd")
    assert f.new_lines() == ["c"]            # "d" is unfinished: held back
    with open(p, "a") as fh:
        fh.write("\n")
    assert f.new_lines() == ["d"]


def test_log_follower_splits_progress_bar_carriage_returns(tmp_path):
    p = tmp_path / "run.out"
    p.write_bytes(b"1/200\r2/200\r3/200\rdone\n")
    assert LogFollower(p).initial()[0] == ["1/200", "2/200", "3/200", "done"]


def test_log_follower_keeps_only_the_tail_of_a_big_file(tmp_path):
    p = tmp_path / "run.out"
    p.write_text("".join(f"line {i}\n" for i in range(5000)))
    lines, truncated = LogFollower(p, max_lines=100).initial()
    assert truncated and len(lines) == 100 and lines[-1] == "line 4999"
    lines, truncated = LogFollower(p, max_lines=10_000, max_bytes=2000).initial()
    assert truncated and lines[-1] == "line 4999" and lines[0].startswith("line ")   # no cut-off fragment


def test_log_follower_restarts_when_the_file_is_replaced(tmp_path):
    p = tmp_path / "run.out"
    p.write_text("old old old old\n")
    f = LogFollower(p)
    f.initial()
    p.write_text("new\n")                    # shorter than before: an Overwrite started over
    assert f.new_lines() == ["new"]
