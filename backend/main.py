"""
main.py — FastAPI backend for the RELION-US web app.

Run with:  uvicorn main:app --host 0.0.0.0 --port 8420 --reload
Then open  http://<this-machine>:8420/  in a browser — including from
another machine on the network, since this binds 0.0.0.0 rather than
localhost-only (matches the "controllable via another machine" request).

Endpoints:
  GET  /api/catalog                       -> Jobs list (grouped by category)
  GET  /api/jobs/{internal_name}          -> full job definition (fields,
                                              standard/advanced split,
                                              draft command, real RELION
                                              source reference)
  POST /api/jobs/{internal_name}/draft    -> recompute the draft command
                                              for a given set of field values
                                              (called live as you edit the
                                              form, before you touch the
                                              editable command box)
  POST /api/runs                          -> start a job run. Body:
                                              {internal_name, command}     for RELION jobs
                                              {internal_name, field_values} for custom jobs
                                              Executes EXACTLY `command` as
                                              given — see job_runner.py.
  GET  /api/runs                          -> list all runs (for a "Running
                                              jobs" panel / reopening a
                                              popup after refresh)
  GET  /api/runs/{run_id}                 -> full state of one run
                                              (stdout/stderr buffers so far)
  WS   /ws/runs/{run_id}                  -> live stdout/stderr/status
                                              stream for one run
  GET  /api/project                       -> current project dir + whether
                                              it's a recognized RELION
                                              project + its run history +
                                              a best-effort SPA/Tomo/mixed/
                                              unknown pipeline_hint (see
                                              project_manager.detect_pipeline
                                              _hint) for auto-selecting the
                                              Jobs-list toggle
  POST /api/project/browse                -> server-side folder listing,
                                              for the "select folder" UI
                                              (the backend may be on a
                                              different machine than the
                                              browser, e.g. a cluster login
                                              node, so this can't be a plain
                                              HTML file picker)
  POST /api/project/switch                -> point the app at a different
                                              project dir. If it doesn't
                                              look like a RELION project,
                                              returns {ok: false} instead of
                                              switching, for the frontend to
                                              show the "start new project /
                                              pick a different folder" popup
  POST /api/project/init                  -> mark a folder as a new RELION-US
                                              project and switch to it
  POST /api/project/create-folder         -> create a new folder from the
                                              Change Project browser ("Create
                                              Folder" button). Returns
                                              {ok: false, reason:
                                              "permission_denied", message}
                                              instead of raising if the
                                              location isn't writable.
  GET  /                                  -> the frontend (static files)
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import job_registry
import project_manager
from custom_jobs import CUSTOM_JOB_DEFINITIONS, CUSTOM_JOB_RUNNERS
from job_runner import JobRunManager

APP_DIR = Path(__file__).resolve().parent
DEFAULT_PROJECT_DIR = APP_DIR.parent / "relion_project"


def _initial_project_dir() -> Path:
    """Answers "do I have to run this from the working directory?": no.
    If the shell's current directory is already a recognized RELION
    project, use it as-is (so `cd my_project && uvicorn ...` just works).
    Otherwise fall back to a default folder next to the app (auto-
    initialised as a fresh project) — same as before, but now just a
    fallback rather than the only option, and always changeable at
    runtime via Change Project in the top bar."""
    cwd = Path(os.getcwd()).resolve()
    if project_manager.is_relion_project(cwd):
        return cwd
    DEFAULT_PROJECT_DIR.mkdir(exist_ok=True)
    if not project_manager.is_relion_project(DEFAULT_PROJECT_DIR):
        project_manager.init_new_project(DEFAULT_PROJECT_DIR)
    return DEFAULT_PROJECT_DIR


PROJECT_DIR = _initial_project_dir()

app = FastAPI(title="RELION-US")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

run_manager = JobRunManager(PROJECT_DIR)


@app.get("/api/catalog")
def get_catalog():
    return {"categories": job_registry.categories(), "jobs": job_registry.list_catalog()}


@app.get("/api/jobs/{internal_name}")
def get_job_definition(internal_name: str):
    if internal_name in CUSTOM_JOB_DEFINITIONS:
        return CUSTOM_JOB_DEFINITIONS[internal_name]
    try:
        return job_registry.build_job_definition(internal_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job type: {internal_name}")


class DraftRequest(BaseModel):
    field_values: dict


@app.post("/api/jobs/{internal_name}/draft")
def recompute_draft(internal_name: str, req: DraftRequest):
    if internal_name in CUSTOM_JOB_DEFINITIONS:
        raise HTTPException(status_code=400, detail="Custom jobs don't use draft commands")
    try:
        raw = job_registry._load_raw()[internal_name]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job type: {internal_name}")
    draft, unmapped = job_registry._build_draft_command(raw, req.field_values)
    return {"draft_command": draft, "unmapped_fields": unmapped}


class StartRunRequest(BaseModel):
    internal_name: str
    command: str | None = None
    field_values: dict | None = None
    subdir: str | None = None


@app.post("/api/runs")
async def start_run(req: StartRunRequest):
    if req.internal_name in CUSTOM_JOB_DEFINITIONS:
        runner = CUSTOM_JOB_RUNNERS[req.internal_name]
        display_name = CUSTOM_JOB_DEFINITIONS[req.internal_name]["display_name"]
        values = req.field_values or {}

        async def factory():
            # Use run_manager.project_dir (not the startup PROJECT_DIR
            # constant) so a project switched *after* this popup was opened
            # but *before* Run was clicked lands in the right place.
            return await runner(run_manager.project_dir, values)

        run = await run_manager.start_custom_job(req.internal_name, display_name, factory)
        return run.to_summary()

    if not req.command:
        raise HTTPException(status_code=400, detail="command is required for RELION job types")
    try:
        meta = job_registry.JOB_CATALOG[req.internal_name]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job type: {req.internal_name}")
    display_name = meta[1]
    run = await run_manager.start_subprocess_job(
        req.internal_name, display_name, req.command, subdir=req.subdir
    )
    return run.to_summary()


@app.get("/api/runs")
def list_runs():
    return run_manager.list_runs()


class ProjectPathRequest(BaseModel):
    path: str


@app.get("/api/project")
def get_project():
    return {
        "path": str(run_manager.project_dir),
        "is_relion_project": project_manager.is_relion_project(run_manager.project_dir),
        "history": run_manager.list_runs(),
        # 'tomo' | 'spa' | 'mixed' | 'unknown' — best-effort guess from which
        # job types this project has actually run (there's no single SPA/
        # Tomo flag in RELION's own STAR files; see
        # project_manager.detect_pipeline_hint()). The frontend uses this to
        # optionally auto-select the Jobs-list toggle; it never gates which
        # jobs are runnable.
        "pipeline_hint": project_manager.detect_pipeline_hint(run_manager.project_dir),
    }


@app.post("/api/project/browse")
def browse_project(req: ProjectPathRequest):
    target = Path(req.path).expanduser() if req.path.strip() else run_manager.project_dir
    try:
        target = target.resolve()
        return project_manager.list_dir(target)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No such directory: {target}")
    except NotADirectoryError:
        raise HTTPException(status_code=400, detail=f"Not a directory: {target}")


@app.post("/api/project/switch")
def switch_project(req: ProjectPathRequest):
    target = Path(req.path).expanduser().resolve()
    # A path that doesn't exist yet is treated the same as "exists but
    # isn't a RELION project" -- both land on the same "start a new
    # project here / pick a different folder" prompt, since starting a
    # new project (POST /api/project/init) creates the directory anyway.
    if not target.is_dir() or not project_manager.is_relion_project(target):
        reason = "does_not_exist" if not target.is_dir() else "not_a_relion_project"
        return {"ok": False, "reason": reason, "path": str(target)}
    run_manager.set_project_dir(target)
    return {"ok": True, "path": str(target), "history": run_manager.list_runs()}


@app.post("/api/project/init")
def init_project(req: ProjectPathRequest):
    target = Path(req.path).expanduser().resolve()
    try:
        project_manager.init_new_project(target)
    except project_manager.PermissionDeniedError as exc:
        return {"ok": False, "reason": "permission_denied", "message": str(exc)}
    run_manager.set_project_dir(target)
    return {"ok": True, "path": str(target), "history": run_manager.list_runs()}


@app.post("/api/project/create-folder")
def create_folder_endpoint(req: ProjectPathRequest):
    """Backs the Change Project browser's "Create Folder" button. Creates
    the folder, then returns a fresh listing of it (same shape as
    /api/project/browse) so the frontend can drop straight into it without
    a second round trip."""
    target = Path(req.path).expanduser().resolve()
    try:
        project_manager.create_folder(target)
    except project_manager.PermissionDeniedError as exc:
        return {"ok": False, "reason": "permission_denied", "message": str(exc)}
    except OSError as exc:
        return {"ok": False, "reason": "error", "message": f"Could not create folder: {exc}"}

    try:
        listing = project_manager.list_dir(target)
    except (FileNotFoundError, NotADirectoryError) as exc:
        return {"ok": False, "reason": "error", "message": str(exc)}
    return {"ok": True, "path": str(target), "listing": listing}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = run_manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    return {
        **run.to_summary(),
        "stdout_lines": run.stdout_lines,
        "stderr_lines": run.stderr_lines,
    }


@app.websocket("/ws/runs/{run_id}")
async def run_websocket(websocket: WebSocket, run_id: str):
    await websocket.accept()
    run = run_manager.get(run_id)
    if run is None:
        await websocket.send_json({"type": "error", "line": "Unknown run_id"})
        await websocket.close()
        return

    # Replay what's already happened so a popup opened after the run
    # started (or reopened after a refresh) isn't missing history.
    for line in run.stdout_lines:
        await websocket.send_json({"type": "stdout", "line": line})
    for line in run.stderr_lines:
        await websocket.send_json({"type": "stderr", "line": line})
    await websocket.send_json({"type": "status", "status": run.status, "exit_code": run.exit_code})

    queue: asyncio.Queue = asyncio.Queue()
    run.subscribers.append(queue)
    try:
        while True:
            message = await queue.get()
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    finally:
        if queue in run.subscribers:
            run.subscribers.remove(queue)


# Serve the frontend last, so /api/* and /ws/* above take precedence.
app.mount("/", StaticFiles(directory=str(APP_DIR.parent / "frontend"), html=True), name="frontend")
