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
    assert f.initial() == ([], False) and f.new_entries() == []


def test_log_follower_returns_the_whole_small_file_then_only_new_lines(tmp_path):
    p = tmp_path / "run.out"
    p.write_text("a\nb\n")
    f = LogFollower(p)
    assert f.initial() == (["a", "b"], False)
    assert f.new_entries() == []
    with open(p, "a") as fh:
        fh.write("c\nd")
    assert f.new_entries() == [("c", False)]            # "d" is unfinished: held back
    with open(p, "a") as fh:
        fh.write("\n")
    assert f.new_entries() == [("d", False)]


def test_log_follower_shows_a_redrawn_progress_bar_as_one_line(tmp_path):
    """tqdm / RELION rewrite one line with \\r; like a terminal, only its last
    state is a line -- a training's thousands of redraws must not become
    thousands of lines in the live output and the Alerts tab."""
    p = tmp_path / "run.out"
    p.write_bytes(b"start\n1/200\r2/200\r3/200\rdone\nnext\n")
    assert LogFollower(p).initial()[0] == ["start", "done", "next"]
    p.write_bytes(b"1/200\r2/200\r3/200\r")
    assert LogFollower(p).initial()[0] == ["3/200"]                 # still being redrawn


def test_log_follower_marks_a_redraw_of_a_line_already_handed_out(tmp_path):
    p = tmp_path / "run.err"
    p.write_bytes(b"INFO begin\nEpoch 1: 1%\r")
    f = LogFollower(p)
    assert f.initial()[0] == ["INFO begin", "Epoch 1: 1%"] and f.open_line
    with open(p, "ab") as fh:
        fh.write(b"Epoch 1: 2%\rEpoch 1: 3%\r")
    assert f.new_entries() == [("Epoch 1: 3%", True)]               # overwrites the bar already shown
    with open(p, "ab") as fh:
        fh.write(b"Epoch 1: 100%\nINFO next\n")
    assert f.new_entries() == [("Epoch 1: 100%", True), ("INFO next", False)]
    assert not f.open_line


def test_log_follower_crlf_is_one_ordinary_line_break(tmp_path):
    p = tmp_path / "run.out"
    p.write_bytes(b"a\r\nb\r\n\r\nc\n")
    assert LogFollower(p).initial()[0] == ["a", "b", "", "c"]


def test_log_follower_flush_gives_the_unfinished_last_line_once(tmp_path):
    p = tmp_path / "run.out"
    p.write_bytes(b"bar 1\rbar 2\r")
    f = LogFollower(p).start_at_end()
    with open(p, "ab") as fh:
        fh.write(b"bar 3\rlast words")
    assert f.new_entries() == [("bar 3", False)]
    assert f.flush() == ("last words", True)       # redraws the bar still on screen
    assert f.flush() is None


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
    assert f.new_entries() == [("new", False)]


# --------------------------------------------------------------------------
# Overwrite clears the job directory; Clean / Harsh Clean know about iterations
# --------------------------------------------------------------------------

import os  # noqa: E402


def _populate(job_dir, names, size=10):
    for n in names:
        p = job_dir / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)


def test_overwrite_empties_the_job_directory_but_keeps_the_bookkeeping(manager, tmp_path):
    async def go():
        first = await start(manager, "true")
        assert await until(lambda: first.status in TERMINAL)
        job_dir = Path(first.cwd)
        _populate(job_dir, ["job.star", "note.txt", "job_pipeline.star", "default_pipeline.star",
                            "run_it010_data.star", "run_it010_class001.mrc", "run.err", "result.mrc",
                            "sub/deep/old.mrc", "other/thing.txt"])
        second = await start(manager, "ls", overwrite_run_id=first.run_id)
        assert await until(lambda: second.status in TERMINAL)
        return job_dir, second

    job_dir, run = asyncio.run(go())
    left = {p.name for p in job_dir.iterdir() if not p.name.startswith("RELION_JOB_")}
    assert left == {"job.star", "note.txt", "job_pipeline.star", "default_pipeline.star", "run.out", "run.err"}
    assert not (job_dir / "run_it010_data.star").exists() and not (job_dir / "sub").exists()
    # run_it010 x2, run.err, result.mrc, sub/deep/old.mrc, other/thing.txt and the first attempt's run.out
    note = next(line for line in run.stdout_lines if "Overwrite: cleared" in line)
    assert "cleared 7 file(s) and 2 folder(s)" in note and "kept job.star" in note
    # run.out/run.err hold only the new attempt: the old run.err contents are gone
    assert (job_dir / "run.err").read_text() == ""


def test_the_old_iterations_are_gone_before_the_new_run_starts(manager, tmp_path):
    """The Progress tab reads the highest run_itNNN it finds; a leftover from
    the previous attempt must not be there for it to find."""
    async def go():
        first = await start(manager, "true")
        assert await until(lambda: first.status in TERMINAL)
        job_dir = Path(first.cwd)
        _populate(job_dir, [f"run_it{i:03d}_{k}" for i in (1, 25) for k in ("data.star", "model.star")])
        second = await start(manager, "sleep 0.4; touch run_it001_model.star", overwrite_run_id=first.run_id)
        # while it runs, nothing of the old run is left to be taken for the latest iteration
        assert await until(lambda: second.status == "running")
        seen = sorted(p.name for p in job_dir.glob("run_it*"))
        assert await until(lambda: second.status in TERMINAL)
        return seen

    assert asyncio.run(go()) == []


def test_overwrite_never_follows_a_symlink_out_of_the_job_directory(manager, tmp_path):
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete")

    async def go():
        first = await start(manager, "true")
        assert await until(lambda: first.status in TERMINAL)
        job_dir = Path(first.cwd)
        os.symlink(outside, job_dir / "linked")
        os.symlink(outside / "keep.txt", job_dir / "linkedfile")
        second = await start(manager, "true", overwrite_run_id=first.run_id)
        assert await until(lambda: second.status in TERMINAL)
        return job_dir

    job_dir = asyncio.run(go())
    assert (outside / "keep.txt").read_text() == "do not delete"
    assert not (job_dir / "linked").exists() and not (job_dir / "linkedfile").is_symlink()


def test_overwrite_refuses_to_clear_a_directory_that_is_not_a_job_folder(manager, tmp_path):
    run = job_runner.JobRun(run_id="x", internal_name="Import", display_name="Import", command="true",
                            cwd=str(tmp_path / "Data"), project_dir=str(tmp_path), job_number=1)
    (tmp_path / "Data").mkdir()
    (tmp_path / "Data" / "precious.txt").write_text("keep")
    assert "NOT cleared" in manager._clear_job_dir_for_overwrite(run)
    outside = job_runner.JobRun(run_id="y", internal_name="Import", display_name="Import", command="true",
                                cwd=str(tmp_path.parent), project_dir=str(tmp_path), job_number=1)
    assert "outside the project" in manager._clear_job_dir_for_overwrite(outside)
    assert (tmp_path / "Data" / "precious.txt").exists()


def test_a_fresh_run_and_an_ordinary_rerun_do_not_clear_anything(manager, tmp_path):
    async def go():
        a = await start(manager, "true")
        assert await until(lambda: a.status in TERMINAL)
        (Path(a.cwd) / "keepme.txt").write_text("x")
        b = await start(manager, "true")             # a different, new job
        assert await until(lambda: b.status in TERMINAL)
        return a

    assert (Path(asyncio.run(go()).cwd) / "keepme.txt").exists()


def _finished_job(manager):
    async def go():
        run = await start(manager, "true")
        assert await until(lambda: run.status in TERMINAL)
        return run

    return asyncio.run(go())


def _suggested(manager, run, harsh):
    return {f["path"] for f in manager.cleanup_candidates(run.run_id, harsh=harsh) if f["suggested"]}


def test_clean_removes_every_iteration_but_the_last_like_relion(manager):
    run = _finished_job(manager)
    job_dir = Path(run.cwd)
    _populate(job_dir, [f"run_it{i:03d}_{k}" for i in (0, 1, 2, 3) for k in ("data.star", "model.star", "class001.mrc")]
              + ["run_data.star", "run_class001.mrc", "job.star", "note.txt"])
    for harsh in (False, True):
        got = _suggested(manager, run, harsh)
        assert {f"run_it{i:03d}_{k}" for i in (0, 1, 2) for k in ("data.star", "model.star", "class001.mrc")} <= got
        assert not any(p.startswith("run_it003") for p in got)          # the last iteration stays
        assert not got & {"run_data.star", "run_class001.mrc", "job.star", "note.txt"}


def test_continued_runs_keep_only_the_last_continue_iteration(manager):
    run = _finished_job(manager)
    _populate(Path(run.cwd), ["run_it010_data.star", "run_ct1_it011_data.star", "run_ct2_it012_data.star",
                              "run_ct2_it013_data.star", "run_ct2_it013_model.star"])
    got = {p for p in _suggested(manager, run, harsh=False) if p.startswith("run_")}
    assert got == {"run_it010_data.star", "run_ct1_it011_data.star", "run_ct2_it012_data.star"}


def test_an_iteration_another_job_uses_is_spared(manager):
    async def go():
        a = await start(manager, "true")
        assert await until(lambda: a.status in TERMINAL)
        _populate(Path(a.cwd), [f"run_it{i:03d}_optimiser.star" for i in (1, 2, 3)])
        rel = Path(a.cwd).relative_to(manager.project_dir)
        b = await start(manager, f"echo --ios {rel}/run_it002_optimiser.star")     # a later job reads iteration 2
        assert await until(lambda: b.status in TERMINAL)
        return a

    run = asyncio.run(go())
    assert {p for p in _suggested(manager, run, harsh=True) if p.startswith("run_it")} == {"run_it001_optimiser.star"}


def test_harsh_adds_large_files_but_gentle_does_not(manager):
    run = _finished_job(manager)
    _populate(Path(run.cwd), ["big.mrc", "small.mrc", "run_it001_data.star", "run_it002_data.star"], size=10)
    (Path(run.cwd) / "big.mrc").write_bytes(b"x" * 2048)
    gentle = {f["path"] for f in manager.cleanup_candidates(run.run_id, harsh=False) if f["suggested"]}
    harsh = {f["path"] for f in manager.cleanup_candidates(run.run_id, harsh=True, harsh_size_threshold=1000) if f["suggested"]}
    assert "big.mrc" not in gentle and "big.mrc" in harsh and "small.mrc" not in harsh


def test_clean_keeps_logs_suggestion_and_never_bookkeeping(manager):
    run = _finished_job(manager)
    _populate(Path(run.cwd), ["run.out", "run.err", "x.log", "job.star", "note.txt", "default_pipeline.star", "job_pipeline.star"])
    got = _suggested(manager, run, harsh=False)
    assert {"run.out", "run.err", "x.log"} <= got
    assert not got & {"job.star", "note.txt", "default_pipeline.star", "job_pipeline.star"}


def test_each_suggestion_says_why(manager):
    run = _finished_job(manager)
    _populate(Path(run.cwd), ["run_it001_data.star", "run_it002_data.star", "x.log"])
    why = {f["path"]: f["reason"] for f in manager.cleanup_candidates(run.run_id, harsh=False)}
    assert why["run_it001_data.star"] == "an earlier iteration"
    assert why["x.log"] == "a log or temporary file" and why["run_it002_data.star"] is None
