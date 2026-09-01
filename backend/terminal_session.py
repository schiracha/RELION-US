"""A single PTY-backed interactive shell, for the Terminal popup.

Unlike JobRunManager's subprocess handling, a terminal isn't a job run --
no run_history entry, no status/exit-code tracking -- so it lives here
rather than in job_runner.py. Interactive programs (vim, top, tab
completion) need a real pseudo-terminal, not a plain pipe, hence pty
instead of asyncio.create_subprocess_shell.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
from pathlib import Path
from typing import Optional

# Cap on buffered-but-not-yet-sent output chunks (each up to 64KB, so this
# bounds server memory to a few MB per open terminal). Without this, an
# ordinary command a user might actually run (`yes`, `cat` on a big file)
# fills the queue faster than the websocket can drain it, growing without
# limit -- this is the same backend process that runs every RELION job, so
# an unbounded queue here can OOM the whole app, not just one terminal.
_MAX_BUFFERED_CHUNKS = 64
_REAP_TIMEOUT_SECONDS = 5.0


class TerminalSession:
    def __init__(self) -> None:
        self.master_fd: Optional[int] = None
        self.proc: Optional[subprocess.Popen] = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_MAX_BUFFERED_CHUNKS)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reader_paused = False

    def start(self, cwd: Path) -> None:
        master_fd, slave_fd = pty.openpty()
        shell = os.environ.get("SHELL", "/bin/bash")
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        try:
            try:
                self.proc = subprocess.Popen(
                    [shell, "-i"],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=str(cwd),
                    env=env,
                    start_new_session=True,
                )
            finally:
                # The child inherited its own copy across fork(); the parent's
                # copy must be closed or the master end never sees EOF.
                os.close(slave_fd)
        except Exception:
            # Popen can raise (bad $SHELL, cwd gone, fork() resource limits)
            # -- self.master_fd is only ever set below, so without this the
            # already-opened master_fd would never be assigned anywhere and
            # therefore never closed, leaking one fd per failed attempt.
            os.close(master_fd)
            raise
        os.set_blocking(master_fd, False)
        self.master_fd = master_fd
        # pty.openpty() leaves winsize at 0x0 until someone sets it. The
        # frontend sends its own real size once its first WS message lands,
        # but that message can arrive well after the shell has already
        # started producing output (a prompt, a MOTD) -- programs that query
        # size on startup would otherwise see a bogus 0x0 in the meantime.
        self.resize(80, 24)
        self._loop = asyncio.get_event_loop()
        self._loop.add_reader(master_fd, self._on_readable)

    def _on_readable(self) -> None:
        assert self.master_fd is not None
        try:
            data = os.read(self.master_fd, 65536)
        except OSError:
            data = b""
        if not data:
            # EOF: shell exited. Stop polling a dead fd and let the
            # websocket handler notice via poll()/wait() on self.proc.
            if self._loop is not None:
                self._loop.remove_reader(self.master_fd)
            return
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            # Stop polling the pty until read() below has drained at least
            # one chunk -- lets the pty's own kernel buffer (and the
            # child's own write() blocking) apply real backpressure instead
            # of this process's memory absorbing output the client hasn't
            # been able to render yet.
            if self._loop is not None:
                self._loop.remove_reader(self.master_fd)
            self._reader_paused = True

    async def read(self) -> bytes:
        data = await self._queue.get()
        if self._reader_paused and self.master_fd is not None and self._loop is not None:
            self._reader_paused = False
            self._loop.add_reader(self.master_fd, self._on_readable)
        return data

    def write(self, data: bytes) -> None:
        if self.master_fd is not None:
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

    def resize(self, cols: int, rows: int) -> None:
        if self.master_fd is None:
            return
        try:
            fcntl.ioctl(
                self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
            )
        except OSError:
            pass

    def poll(self) -> Optional[int]:
        return self.proc.poll() if self.proc is not None else None

    def close(self) -> None:
        if self._loop is not None and self.master_fd is not None:
            try:
                self._loop.remove_reader(self.master_fd)
            except (ValueError, OSError):
                pass
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if proc is not None and self._loop is not None:
            # SIGTERM doesn't reap synchronously -- without an explicit
            # wait() the child sits as a zombie until something else in
            # this process happens to reap it (nothing guarantees that).
            # close() itself is synchronous (called from the websocket
            # handler's `finally`), so reap in the background instead of
            # blocking it.
            self._loop.create_task(_reap(proc))


async def _reap(proc: subprocess.Popen) -> None:
    loop = asyncio.get_event_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(None, proc.wait), timeout=_REAP_TIMEOUT_SECONDS)
        return
    except asyncio.TimeoutError:
        pass
    except Exception:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        await loop.run_in_executor(None, proc.wait)
    except Exception:
        pass
