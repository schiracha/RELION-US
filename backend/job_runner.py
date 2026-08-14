"""
job_runner.py — executes the exact, user-approved command string for a job
popup and streams stdout/stderr back live, plus a separate errors buffer.

Design principle (this is the whole point of the app): the backend NEVER
re-assembles or "fixes up" the command you approved in the popup. Whatever
string was in the editable command box when you clicked Run is exactly what
gets executed — via the shell, since real RELION commands legitimately
contain shell constructs (e.g. `` `which relion_run_motioncorr_mpi` ``, see
job_registry.py). If that's wrong, it's wrong the way you wrote it, not
because something was silently inserted or duplicated under the hood.

Custom (non-RELION) jobs — the IMOD/Warp-M/DeepETPicker import bridges —
don't spawn a subprocess at all; they call directly into backend/converters/
in a worker thread, and their "live output" is progress text this module
formats from the converter's return value/exception. They share the same
JobRun/streaming interface so the frontend popup code doesn't need to know
the difference.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import project_manager

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


@dataclass
class JobRun:
    run_id: str
    internal_name: str
    display_name: str
    command: str
    cwd: str
    project_dir: str
    status: str = STATUS_PENDING
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)

    def to_summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "internal_name": self.internal_name,
            "display_name": self.display_name,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "project_dir": self.project_dir,
        }

    async def broadcast(self, message: dict) -> None:
        for q in list(self.subscribers):
            await q.put(message)


class JobRunManager:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.runs: dict[str, JobRun] = {}

    def new_run_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def get(self, run_id: str) -> Optional[JobRun]:
        return self.runs.get(run_id)

    def set_project_dir(self, project_dir: Path) -> None:
        """Switch the active project directory. In-flight runs already have
        their own `cwd`/`project_dir` baked in (see start_subprocess_job)
        and keep running/streaming exactly as before — they just stop
        showing up in list_runs() for the *new* project, the same way a
        job you started in project A doesn't disappear when you point the
        GUI at project B, it just isn't "in" B."""
        self.project_dir = project_dir

    def list_runs(self, project_dir: Optional[Path] = None) -> list[dict]:
        """Runs for one project: persisted history (past sessions, summary
        only) merged with any still-tracked in-memory runs (current session,
        which may have moved past what was last persisted, e.g. still
        running). In-memory wins on conflict since it's more current."""
        target = str(project_dir if project_dir is not None else self.project_dir)
        merged: dict[str, dict] = {}
        for entry in project_manager.load_history(Path(target)):
            run_id = entry.get("run_id")
            if run_id:
                merged[run_id] = entry
        for run in self.runs.values():
            if run.project_dir == target:
                merged[run.run_id] = run.to_summary()
        return sorted(merged.values(), key=lambda r: r.get("started_at") or 0)

    def _persist(self, run: JobRun) -> None:
        """Best-effort: append/update this run's summary in its project's
        on-disk history. A history-file write failure should never take
        down a job run, so failures here are swallowed."""
        try:
            project_dir = Path(run.project_dir)
            history = [h for h in project_manager.load_history(project_dir) if h.get("run_id") != run.run_id]
            history.append(run.to_summary())
            project_manager.save_history(project_dir, history)
        except OSError:
            pass

    async def start_subprocess_job(
        self, internal_name: str, display_name: str, command: str, subdir: Optional[str] = None
    ) -> JobRun:
        """
        Launch `command` (the exact, user-edited string) via the shell.
        subdir, if given, is created under project_dir and used as cwd —
        mirrors RELION's own convention of one output directory per job run.
        """
        run_id = self.new_run_id()
        project_dir = self.project_dir
        cwd = project_dir / (subdir or f"{internal_name}_{run_id}")
        cwd.mkdir(parents=True, exist_ok=True)

        run = JobRun(
            run_id=run_id,
            internal_name=internal_name,
            display_name=display_name,
            command=command,
            cwd=str(cwd),
            project_dir=str(project_dir),
        )
        self.runs[run_id] = run
        self._persist(run)
        asyncio.create_task(self._run_subprocess(run))
        return run

    async def _run_subprocess(self, run: JobRun) -> None:
        run.status = STATUS_RUNNING
        run.started_at = time.time()
        self._persist(run)
        await run.broadcast({"type": "status", "status": run.status})

        try:
            proc = await asyncio.create_subprocess_shell(
                run.command,
                cwd=run.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:  # noqa: BLE001
            run.status = STATUS_FAILED
            run.ended_at = time.time()
            run.stderr_lines.append(f"Failed to launch: {exc}")
            self._persist(run)
            await run.broadcast({"type": "stderr", "line": f"Failed to launch: {exc}"})
            await run.broadcast({"type": "status", "status": run.status})
            return

        async def pump(stream, sink: list[str], msg_type: str):
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace").rstrip("\n")
                sink.append(decoded)
                await run.broadcast({"type": msg_type, "line": decoded})

        await asyncio.gather(
            pump(proc.stdout, run.stdout_lines, "stdout"),
            pump(proc.stderr, run.stderr_lines, "stderr"),
        )
        exit_code = await proc.wait()
        run.exit_code = exit_code
        run.status = STATUS_COMPLETED if exit_code == 0 else STATUS_FAILED
        run.ended_at = time.time()
        self._persist(run)
        await run.broadcast({"type": "status", "status": run.status, "exit_code": exit_code})

    async def start_custom_job(
        self, internal_name: str, display_name: str, runner_coro_factory
    ) -> JobRun:
        """
        runner_coro_factory: a zero-arg callable returning a coroutine that
        does the actual work and returns a human-readable summary string
        (or raises). Used by custom_jobs.py for the IMOD/Warp/DeepETPicker
        bridges, which call converters/ directly instead of spawning a
        subprocess.
        """
        run_id = self.new_run_id()
        project_dir = self.project_dir
        run = JobRun(
            run_id=run_id,
            internal_name=internal_name,
            display_name=display_name,
            command=f"<in-process: {internal_name}>",
            cwd=str(project_dir),
            project_dir=str(project_dir),
        )
        self.runs[run_id] = run
        self._persist(run)
        asyncio.create_task(self._run_custom(run, runner_coro_factory))
        return run

    async def _run_custom(self, run: JobRun, runner_coro_factory) -> None:
        run.status = STATUS_RUNNING
        run.started_at = time.time()
        self._persist(run)
        await run.broadcast({"type": "status", "status": run.status})
        try:
            result = await runner_coro_factory()
            for line in str(result).splitlines() or ["(no output)"]:
                run.stdout_lines.append(line)
                await run.broadcast({"type": "stdout", "line": line})
            run.status = STATUS_COMPLETED
            run.exit_code = 0
        except Exception as exc:  # noqa: BLE001
            run.stderr_lines.append(f"{type(exc).__name__}: {exc}")
            await run.broadcast({"type": "stderr", "line": f"{type(exc).__name__}: {exc}"})
            run.status = STATUS_FAILED
            run.exit_code = 1
        finally:
            run.ended_at = time.time()
            self._persist(run)
            await run.broadcast({"type": "status", "status": run.status, "exit_code": run.exit_code})
