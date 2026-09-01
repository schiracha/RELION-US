"""Tests for terminal_session.TerminalSession (issue #3, Terminal popup)."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal_session import TerminalSession


async def _read_until(session: TerminalSession, needle: str, timeout: float = 5.0) -> str:
    buf = ""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while needle not in buf:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(f"Timed out waiting for {needle!r} in {buf!r}")
        chunk = await asyncio.wait_for(session.read(), timeout=remaining)
        buf += chunk.decode("utf-8", errors="replace")
    return buf


def test_write_and_read_round_trips_through_the_shell(tmp_path):
    async def go():
        session = TerminalSession()
        session.start(tmp_path)
        try:
            session.write(b"echo hello-terminal-test\n")
            output = await _read_until(session, "hello-terminal-test")
            assert "hello-terminal-test" in output
        finally:
            session.close()
    asyncio.run(go())


def test_start_sets_cwd_to_the_given_directory(tmp_path):
    async def go():
        session = TerminalSession()
        session.start(tmp_path)
        try:
            session.write(b"pwd\n")
            output = await _read_until(session, str(tmp_path))
            assert str(tmp_path) in output
        finally:
            session.close()
    asyncio.run(go())


def test_start_seeds_a_default_80x24_winsize(tmp_path):
    # pty.openpty() itself leaves winsize at 0x0. Without an explicit
    # default, a client that connects and never sends a "resize" message
    # in time (e.g. the frontend's own resize arrives after the socket
    # handshake) would leave programs querying `stty size`/COLUMNS/LINES
    # seeing 0x0 during that window -- confirmed via manual browser testing
    # before this fix was added.
    async def go():
        session = TerminalSession()
        session.start(tmp_path)
        try:
            session.write(b"stty size\n")
            output = await _read_until(session, "24 80")
            assert "0 0" not in output
        finally:
            session.close()
    asyncio.run(go())


def test_resize_does_not_raise(tmp_path):
    async def go():
        session = TerminalSession()
        session.start(tmp_path)
        try:
            session.resize(120, 40)
        finally:
            session.close()
    asyncio.run(go())


def test_resize_before_start_is_a_noop():
    session = TerminalSession()
    session.resize(80, 24)  # must not raise even though start() was never called


def test_close_actually_terminates_the_shell_process(tmp_path):
    async def go():
        session = TerminalSession()
        session.start(tmp_path)
        proc = session.proc
        assert proc.poll() is None
        session.close()
        # SIGTERM delivery + reaping isn't instantaneous.
        for _ in range(50):
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        assert proc.poll() is not None
    asyncio.run(go())


def test_write_after_close_does_not_raise(tmp_path):
    async def go():
        session = TerminalSession()
        session.start(tmp_path)
        session.close()
        session.write(b"echo still alive\n")  # fd is closed; must be swallowed, not raised
    asyncio.run(go())


def test_start_failure_does_not_leak_the_master_fd(tmp_path, monkeypatch):
    # Popen can raise (bad $SHELL, cwd gone, fork() resource limits) --
    # pty.openpty()'s master fd used to only ever get assigned to
    # self.master_fd (and thus ever get closed) AFTER Popen succeeded, so a
    # failed start() leaked one fd per attempt with no way to reclaim it.
    monkeypatch.setenv("SHELL", "/nonexistent/not-a-real-shell")
    session = TerminalSession()
    fd_dir = Path(f"/proc/{os.getpid()}/fd")
    before = len(list(fd_dir.iterdir()))
    with pytest.raises(OSError):
        session.start(tmp_path)
    after = len(list(fd_dir.iterdir()))
    assert after == before
    assert session.master_fd is None


def test_close_reaps_the_child_instead_of_leaving_a_zombie(tmp_path):
    # close() sends SIGTERM but (before this fix) never called wait() on
    # the child anywhere -- the shell became a zombie entry in the process
    # table until something UNRELATED in this process happened to reap it
    # (e.g. job_runner.py spawning its own subprocess), which isn't
    # guaranteed for a terminal-only session.
    async def go():
        session = TerminalSession()
        session.start(tmp_path)
        pid = session.proc.pid
        session.close()
        # close() reaps in a background asyncio task; give the loop time
        # to actually run it before checking.
        for _ in range(50):
            await asyncio.sleep(0.1)
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return  # no longer a tracked child of this process -- reaped
        raise AssertionError(f"pid {pid} was never reaped (still a zombie)")
    asyncio.run(go())


def test_pty_output_queue_is_bounded(tmp_path):
    # The queue used to be unbounded: a pty reader fed it as fast as the
    # kernel would deliver bytes, with no coordination with how fast the
    # websocket handler could actually drain it. An ordinary command a
    # user might run (`yes`, `cat` on a big file) would then grow this
    # process's memory without limit for as long as it kept producing
    # output faster than it was consumed.
    async def go():
        session = TerminalSession()
        session.start(tmp_path)
        try:
            session.write(b"yes | head -c 5000000\n")
            await asyncio.sleep(1.0)  # let output pile up without draining it
            assert session._queue.qsize() <= 64
        finally:
            session.close()
    asyncio.run(go())
