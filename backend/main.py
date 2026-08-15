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
                                              Optional overwrite_run_id
                                              re-runs into an earlier run's
                                              own output directory + job
                                              number (Command Center
                                              'Overwrite' action) instead of
                                              allocating a new one.
  GET  /api/runs                          -> list all runs (for the Command
                                              Center table/timeline)
  GET  /api/runs/{run_id}                 -> full state of one run
                                              (stdout/stderr buffers so far)
  WS   /ws/runs/{run_id}                  -> live stdout/stderr/status
                                              stream for one run
  POST /api/runs/{run_id}/abort           -> Command Center 'Abort' action
  PATCH /api/runs/{run_id}                -> Command Center 'Alias'/'Edit
                                              Note'/'Mark as finished'/'Mark
                                              as failed' actions. Body: any
                                              of {alias, note, status}
  DELETE /api/runs/{run_id}               -> Command Center 'Delete' action.
                                              ?remove_files=true also deletes
                                              the job's own output directory
  GET  /api/runs/{run_id}/files           -> Outputs tab file listing.
                                              ?harsh=true/false instead
                                              returns the Clean/Harsh Clean
                                              review list (files annotated
                                              with a pre-checked `suggested`
                                              bool -- see job_runner.py's
                                              cleanup_candidates() for why
                                              this is a suggestion, not a
                                              port of RELION's own per-job
                                              -type cleanup rules)
  GET  /api/runs/{run_id}/files/download  -> download one output file
                                              (?path=relative/path)
  POST /api/runs/{run_id}/files/delete    -> delete a user-confirmed set of
                                              output files (Clean/Harsh
                                              Clean, or manual selection)
  GET  /api/runs/{run_id}/files/zip       -> download a user-selected subset
                                              of output files as one .zip
                                              (repeat ?path=... per file)
  GET  /api/runs/{run_id}/progress        -> live progress for iterative jobs
                                              (per-iteration resolution + class
                                              distribution, from RELION's own
                                              run_it###_model.star)
  GET  /api/runs/{run_id}/progress/thumbnail
                                           -> one class average / central slice
                                              of a class volume, as a small PNG
                                              (?reference=<rlnReferenceImage>)
  POST /api/viz/inspect                   -> visualizer: classify an MRC/STAR
                                              input, list its tomogram(s)
  GET  /api/viz/volume-info               -> visualizer: MRC dims, voxel size,
                                              default contrast
  GET  /api/viz/slice                     -> visualizer: one slice as a PNG
                                              (?mrc_path&axis&index&lo&hi)
  POST /api/viz/picks                     -> visualizer: picks (voxel coords)
                                              for a tomogram + filename-match
                                              check
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
  GET  /api/project/recent                -> recently opened project dirs,
                                              most recent first, for the
                                              Change Project dialog's
                                              quick-switch list
  POST /api/project/recent/remove         -> drop one entry from that list
                                              (bookmark only; never touches
                                              the directory)
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
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

import job_registry
import progress
import project_manager
import viz
from custom_jobs import CUSTOM_JOB_DEFINITIONS, CUSTOM_JOB_RUNNERS
from job_runner import MANUALLY_SETTABLE_STATUSES, JobRunManager

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

# The project the app starts in counts as opened, so the very first entry in
# the Change Project dialog's recent list is the one already on screen.
project_manager.remember_project(PROJECT_DIR)


@app.get("/api/catalog")
def get_catalog():
    return {"categories": job_registry.categories(), "jobs": job_registry.list_catalog()}


def _custom_job_definition(internal_name: str) -> dict:
    """A custom job's definition, with `default_values` derived from its own
    options. Real RELION jobs get this key from
    job_registry.build_job_definition(); without it the frontend's
    `def.default_values || {}` fell back to an empty dict, so every custom
    job's popup opened with EVERY field blank -- a blank numeric field parses
    to NaN and a blank output path resolves to the job directory itself.
    Derived here rather than hand-written per job so the declared `default`
    on each option stays the single source of truth."""
    definition = dict(CUSTOM_JOB_DEFINITIONS[internal_name])
    definition["default_values"] = {
        opt["key"]: opt.get("default", "") for opt in definition.get("options", [])
    }
    return definition


@app.get("/api/jobs/{internal_name}")
def get_job_definition(internal_name: str):
    if internal_name in CUSTOM_JOB_DEFINITIONS:
        return _custom_job_definition(internal_name)
    try:
        # Prospective RELION-style output dir (<JobDir>/jobNNN) for the draft's
        # --o, matching RELION's run-from-project-root convention. Finalized at
        # Run time (see job_runner.start_subprocess_job).
        output_subdir = run_manager.prospective_subdir(internal_name)
        return job_registry.build_job_definition(internal_name, output_subdir=output_subdir)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job type: {internal_name}")


class DraftRequest(BaseModel):
    field_values: dict
    # The output dir the popup is currently targeting (from the job
    # definition's output_subdir). Kept stable across recomputes so the
    # command's --o doesn't jump job numbers while the user edits fields.
    output_subdir: str | None = None


@app.post("/api/jobs/{internal_name}/draft")
def recompute_draft(internal_name: str, req: DraftRequest):
    if internal_name in CUSTOM_JOB_DEFINITIONS:
        raise HTTPException(status_code=400, detail="Custom jobs don't use draft commands")
    try:
        raw = job_registry._load_raw()[internal_name]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job type: {internal_name}")
    output_subdir = req.output_subdir or run_manager.prospective_subdir(internal_name)
    draft, unmapped = job_registry._build_draft_command(
        raw, req.field_values, internal_name, output_subdir
    )
    return {"draft_command": draft, "unmapped_fields": unmapped, "output_subdir": output_subdir}


class StartRunRequest(BaseModel):
    internal_name: str
    command: str | None = None
    field_values: dict | None = None
    subdir: str | None = None
    # Command Center "Overwrite" job action (see job_runner.py's
    # start_subprocess_job/start_custom_job docstrings): re-runs into the
    # SAME output directory + job number as an earlier run, instead of
    # allocating a new one.
    overwrite_run_id: str | None = None


@app.post("/api/runs")
async def start_run(req: StartRunRequest):
    if req.internal_name in CUSTOM_JOB_DEFINITIONS:
        runner = CUSTOM_JOB_RUNNERS[req.internal_name]
        display_name = CUSTOM_JOB_DEFINITIONS[req.internal_name]["display_name"]
        values = req.field_values or {}

        async def factory(job_dir):
            # Use run_manager.project_dir (not the startup PROJECT_DIR
            # constant) so a project switched *after* this popup was opened
            # but *before* Run was clicked lands in the right place.
            # job_dir is this run's own <JobDir>/jobNNN, so converter outputs
            # land where the Outputs tab / Clean / Delete look for them.
            return await runner(run_manager.project_dir, values, job_dir)

        try:
            run = await run_manager.start_custom_job(
                req.internal_name, display_name, factory,
                field_values=values, overwrite_run_id=req.overwrite_run_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return run.to_summary()

    if not req.command:
        raise HTTPException(status_code=400, detail="command is required for RELION job types")
    try:
        meta = job_registry.JOB_CATALOG[req.internal_name]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job type: {req.internal_name}")
    display_name = meta[1]
    try:
        run = await run_manager.start_subprocess_job(
            req.internal_name, display_name, req.command,
            subdir=req.subdir, field_values=req.field_values,
            overwrite_run_id=req.overwrite_run_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return run.to_summary()


@app.get("/api/runs")
def list_runs():
    return run_manager.list_runs()


@app.post("/api/runs/{run_id}/abort")
async def abort_run(run_id: str):
    """Command Center 'Abort' action (real RELION 'Abort running')."""
    ok = await run_manager.abort_run(run_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Run is not currently running (or doesn't exist)")
    return {"ok": True}


class RunUpdateRequest(BaseModel):
    alias: str | None = None
    note: str | None = None
    status: str | None = None


@app.patch("/api/runs/{run_id}")
def update_run(run_id: str, req: RunUpdateRequest):
    """Command Center 'Alias' / 'Edit Note' / 'Mark as finished'/'Mark as
    failed' actions, combined into one endpoint since they're all small,
    independent metadata edits. Any combination of fields may be given in
    one call; each is applied in turn.

    Everything is validated BEFORE anything is written: an invalid status
    used to be rejected only after the alias/note edits had already been
    persisted, so the caller got an error while two of three changes had
    silently landed on disk."""
    if req.alias is None and req.note is None and req.status is None:
        raise HTTPException(
            status_code=400,
            detail="Nothing to update -- provide alias, note, and/or status",
        )
    if req.status is not None and req.status not in MANUALLY_SETTABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"status must be one of {sorted(MANUALLY_SETTABLE_STATUSES)} "
                f"(got {req.status!r})"
            ),
        )

    updated: dict | None = None
    if req.alias is not None:
        updated = run_manager.set_alias(run_id, req.alias)
        if updated is None:
            raise HTTPException(status_code=404, detail="Unknown run_id")
    if req.note is not None:
        updated = run_manager.set_note(run_id, req.note)
        if updated is None:
            raise HTTPException(status_code=404, detail="Unknown run_id")
    if req.status is not None:
        try:
            updated = run_manager.set_status(run_id, req.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if updated is None:
            raise HTTPException(status_code=404, detail="Unknown run_id")
    return updated


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str, remove_files: bool = False):
    """Command Center 'Delete' action (real RELION 'Delete' job action).
    remove_files=true also removes the job's own output directory -- see
    job_runner.JobRunManager.delete_run's docstring for the safety checks."""
    ok, reason = run_manager.delete_run(run_id, remove_files=remove_files)
    if not ok:
        status_code = 404 if reason == "Unknown run_id" else 409
        raise HTTPException(status_code=status_code, detail=reason)
    return {"ok": True, "message": reason}


@app.get("/api/runs/{run_id}/files")
def list_run_files(run_id: str, harsh: bool | None = None):
    """Outputs tab file listing. Pass `harsh` (true/false) to get the Clean
    / Harsh Clean review list instead (each file annotated with a
    `suggested` bool for the pre-checked selection -- see
    JobRunManager.cleanup_candidates' docstring for exactly what that
    means and why it's a suggestion, not RELION's own per-job-type rule)."""
    if harsh is None:
        files = run_manager.list_output_files(run_id)
    else:
        files = run_manager.cleanup_candidates(run_id, harsh=harsh)
    if files is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    return {"files": files}


@app.get("/api/runs/{run_id}/files/download")
def download_run_file(run_id: str, path: str):
    """Single-file download for the Outputs tab (star/mrc/mrcs/etc -- any
    file actually present in the job's output directory)."""
    resolved = run_manager.resolve_output_file(run_id, path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="File not found in this job's output directory")
    return FileResponse(resolved, filename=resolved.name)


class DeleteFilesRequest(BaseModel):
    relative_paths: list[str]


@app.post("/api/runs/{run_id}/files/delete")
def delete_run_files(run_id: str, req: DeleteFilesRequest):
    """Executes a user-confirmed Clean / Harsh Clean selection (or any
    manual file selection from the Outputs tab) -- see
    JobRunManager.delete_output_files' docstring."""
    if not req.relative_paths:
        raise HTTPException(status_code=400, detail="relative_paths is empty -- nothing to delete")
    return run_manager.delete_output_files(run_id, req.relative_paths)


@app.get("/api/runs/{run_id}/files/zip")
def download_run_files_zip(run_id: str, path: list[str] = Query(...)):
    """'Download selected as .zip' for the Outputs tab -- bounded to
    whatever the user checked (not a recursive zip of the whole output
    directory, which for cryo-EM/tomography outputs can be many GB): built
    to a temp file rather than in memory, and cleaned up after the response
    is sent via FastAPI's BackgroundTask."""
    resolved_paths = []
    for rel in path:
        resolved = run_manager.resolve_output_file(run_id, rel)
        if resolved is None:
            raise HTTPException(status_code=404, detail=f"File not found in this job's output directory: {rel}")
        resolved_paths.append((rel, resolved))

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel, resolved in resolved_paths:
                zf.write(resolved, arcname=rel)
    except Exception:
        # Only the success path gets a BackgroundTask cleanup, so without this
        # a failed zip (disk full, file removed mid-write) would leave the temp
        # file behind permanently -- and these are cryo-EM sized.
        tmp_path.unlink(missing_ok=True)
        raise

    def _cleanup():
        tmp_path.unlink(missing_ok=True)

    return FileResponse(
        tmp_path, filename=f"{run_id}_outputs.zip", media_type="application/zip",
        background=BackgroundTask(_cleanup),
    )


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


@app.get("/api/project/recent")
def recent_projects():
    """Recently opened project directories, most recent first, for the Change
    Project dialog's quick-switch list. `exists`/`is_project` are recomputed on
    every call — a folder can be deleted, or become a real RELION project, with
    this app uninvolved."""
    return {"recent": project_manager.load_recent_projects()}


@app.post("/api/project/recent/remove")
def forget_recent_project(req: ProjectPathRequest):
    """Remove one entry from the recent list. Bookmark only — the directory
    itself is never touched."""
    project_manager.forget_project(req.path)
    return {"ok": True, "recent": project_manager.load_recent_projects()}


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
    project_manager.remember_project(target)
    return {"ok": True, "path": str(target), "history": run_manager.list_runs()}


@app.post("/api/project/init")
def init_project(req: ProjectPathRequest):
    target = Path(req.path).expanduser().resolve()
    try:
        project_manager.init_new_project(target)
    except project_manager.PermissionDeniedError as exc:
        return {"ok": False, "reason": "permission_denied", "message": str(exc)}
    run_manager.set_project_dir(target)
    project_manager.remember_project(target)
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

    async def watch_for_disconnect() -> None:
        # Starlette only raises WebSocketDisconnect from receive*(), never
        # from send*(). Without this reader the send loop below would park on
        # queue.get() forever once the run finished -- leaking a task, a queue
        # and a subscribers entry for every popup the user ever opened.
        try:
            while True:
                await websocket.receive_text()
        except Exception:  # noqa: BLE001 - any disconnect/protocol error ends it
            pass

    reader = asyncio.create_task(watch_for_disconnect())
    try:
        while True:
            get_next = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {get_next, reader}, return_when=asyncio.FIRST_COMPLETED
            )
            if reader in done:          # client went away
                get_next.cancel()
                break
            await websocket.send_json(get_next.result())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader.cancel()
        if queue in run.subscribers:
            run.subscribers.remove(queue)


# --------------------------------------------------------------------------
# Live progress for iterative jobs (Class2D/Class3D/Refine3D/InitialModel/...)
# Reads the per-iteration run_it###_model.star RELION writes itself; renders
# class thumbnails on demand from the MRCs it already wrote. Nothing is cached
# to disk. See progress.py.
# --------------------------------------------------------------------------


@app.get("/api/runs/{run_id}/progress")
def run_progress(run_id: str):
    cwd = run_manager._resolve_run_cwd(run_id)
    if cwd is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    run = run_manager.get(run_id)
    internal_name = run.internal_name if run is not None else None
    if internal_name is None:
        entry = next(
            (h for h in run_manager.list_runs() if h.get("run_id") == run_id), None
        )
        internal_name = (entry or {}).get("internal_name")
    if not internal_name or not progress.supports_progress(internal_name):
        return {"available": False, "supported": False, "iterations": [], "latest": None}
    try:
        data = progress.read_progress(Path(cwd))
    except progress.ProgressError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    data["supported"] = True
    return data


@app.get("/api/runs/{run_id}/progress/thumbnail")
def run_progress_thumbnail(run_id: str, reference: str = Query(...)):
    cwd = run_manager._resolve_run_cwd(run_id)
    if cwd is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    try:
        png = progress.render_class_thumbnail(Path(cwd), reference)
    except progress.ProgressError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Immutable: RELION never rewrites a completed iteration's class images, so
    # the browser can keep these without re-fetching while the popup is open.
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


# --------------------------------------------------------------------------
# Tomogram / particle-pick visualizer (a plain tool, NOT a RELION job — it
# never appears in the Command Center and writes nothing). See viz.py.
# --------------------------------------------------------------------------


class VizInspectRequest(BaseModel):
    path: str
    particles_path: str | None = None


@app.post("/api/viz/inspect")
def viz_inspect(req: VizInspectRequest):
    try:
        return viz.inspect(run_manager.project_dir, req.path, req.particles_path)
    except viz.VizError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/viz/volume-info")
def viz_volume_info(mrc_path: str = Query(...)):
    try:
        return viz.volume_info(run_manager.project_dir, mrc_path)
    except viz.VizError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/viz/slice")
def viz_slice(
    mrc_path: str = Query(...),
    axis: str = Query("z"),
    index: int = Query(0),
    lo: float | None = Query(None),
    hi: float | None = Query(None),
    # The orthogonal viewer's left panel needs [y, z] rather than [z, y]; see
    # viz.render_slice_png().
    transpose: bool = Query(False),
):
    try:
        png = viz.render_slice_png(
            run_manager.project_dir, mrc_path, axis, index, lo, hi, transpose=transpose
        )
    except viz.VizError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(content=png, media_type="image/png")


class VizPicksRequest(BaseModel):
    particles_path: str
    tomo_name: str | None = None
    # volume dims + voxel size, needed only to place centred-Angstrom coords
    volume: dict | None = None


@app.post("/api/viz/picks")
def viz_picks(req: VizPicksRequest):
    try:
        return viz.load_picks(
            run_manager.project_dir, req.particles_path, req.tomo_name, req.volume
        )
    except viz.VizError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers request this automatically on every page load; without a
    route it's a spurious 404 in server logs and in browser-automation
    error collectors (Playwright, etc.). No icon file yet, so just answer
    with an empty 204 instead of a 404."""
    return Response(status_code=204)


# Serve the frontend last, so /api/* and /ws/* above take precedence.
app.mount("/", StaticFiles(directory=str(APP_DIR.parent / "frontend"), html=True), name="frontend")
