"""
main.py — FastAPI backend for the RELION-US web app.

Run with:  uvicorn main:app --host 127.0.0.1 --port 8420 --reload
Then open  http://localhost:8420/  in a browser. Run-RelionUS binds
127.0.0.1 by default -- reach it from another machine via an SSH tunnel
(`ssh -L 8420:localhost:8420 <host>`) or by opting in explicitly with
`--host 0.0.0.0` (see Run-RelionUS's own usage comment and
backend/auth.py's module docstring for the security tradeoffs of doing
that).

Endpoints:
  GET  /api/catalog                       -> Jobs list (grouped by category)
  GET  /api/jobs/{internal_name}          -> full job definition (fields,
                                              standard/advanced split,
                                              draft command, real RELION
                                              source reference)
  GET  /api/jobs/{internal_name}/cli-options
                                          -> options the job's program accepts
                                             that RELION's own form does not
                                             expose (the Advanced section of
                                             the Inputs tab), read by running
                                             the installed binary with --help
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
  GET  /api/project/pipeline-sync         -> is this project's history shared
                                              with RELION's own
                                              default_pipeline.star, and is
                                              relion_pipeliner available
  POST /api/project/pipeline-sync         -> turn that on/off for this project
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
  GET  /api/auth/status                   -> {enabled: bool} -- is password
                                              protection turned on right now
                                              (see backend/auth.py; managed
                                              from the terminal via
                                              Run-RelionUS --set-password /
                                              --enable-auth / --disable-auth,
                                              never from the browser)
  POST /api/auth/login                    -> body {password}. Sets the
                                              session cookie on success.
  POST /api/auth/logout                   -> clears the session cookie.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

import analyze
import auth
import ctf_qc
import job_registry
import progress
import pipeline_bridge
import program_help
import project_manager
import manual_pick
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


# --- Password protection (backend/auth.py) ----------------------------------
# Off unless a password has been set AND turned on from the terminal (see
# Run-RelionUS --set-password / --enable-auth) -- most of the time this
# middleware is a single dict lookup that changes nothing.

# Reachable with no session cookie at all, so there's somewhere to log in
# from and something the frontend can ask to avoid a redirect loop.
_AUTH_PUBLIC_PATHS = {"/login.html", "/api/auth/status", "/api/auth/login", "/favicon.ico"}


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    cfg = auth.load_config()
    if not auth.is_enabled(cfg):
        return await call_next(request)
    path = request.url.path
    if path in _AUTH_PUBLIC_PATHS or auth.session_is_valid(request.cookies.get(auth.COOKIE_NAME), cfg):
        return await call_next(request)
    if path.startswith("/api/") or path.startswith("/ws/"):
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return RedirectResponse(url="/login.html", status_code=302)


class LoginRequest(BaseModel):
    password: str


@app.get("/api/auth/status")
def auth_status():
    return {"enabled": auth.is_enabled()}


@app.post("/api/auth/login")
async def auth_login(req: LoginRequest):
    cfg = auth.load_config()
    if not auth.verify_password(req.password, cfg):
        # A small fixed delay on a wrong guess -- cheap insurance against a
        # trivial guessing script, in keeping with this being a deterrent
        # rather than a hardened login (see auth.py's module docstring).
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=401, detail="Incorrect password")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.new_session_token(cfg),
        max_age=auth.SESSION_LIFETIME_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/api/auth/logout")
def auth_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME)
    return response


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
    # Command Center "Overwrite" job action: recompute the draft for the
    # SAME output directory as an existing (e.g. failed) run, not a fresh
    # job number -- see StartRunRequest.overwrite_run_id. Takes priority
    # over output_subdir when both are somehow set.
    overwrite_run_id: str | None = None


@app.get("/api/jobs/{internal_name}/cli-options")
def job_cli_options(internal_name: str, nr_mpi: int = Query(1)):
    """Options the job's program accepts that its RELION form does not offer —
    the Inputs tab's Advanced section.

    Discovered by running the installed binary with --help (see
    program_help.py), not from the extracted job definitions: the GUI shows a
    subset of what the program accepts, and the extra ones are precisely the
    flags you would otherwise hunt for in `--help` output or the source.

    nr_mpi picks which binary to ask, since RELION's parallel variant can
    accept flags the serial one doesn't.
    """
    if internal_name in CUSTOM_JOB_DEFINITIONS:
        return {
            "available": False,
            "reason": "custom_job",
            "message": "This is a RELION-US import bridge, not a command-line "
                       "program — it has no extra CLI options.",
            "options": [],
        }
    try:
        raw = job_registry.raw_job(internal_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job type: {internal_name}")

    program = raw.get("program_mpi") if (nr_mpi > 1 and raw.get("program_mpi")) else raw.get("program_guess")
    if not program:
        return {
            "available": False,
            "reason": "no_program",
            "message": "This job has no fixed program to ask — it runs the "
                       "executable you configure in its own fields.",
            "options": [],
        }
    try:
        payload = program_help.extra_options_for_job(raw, program)
    except program_help.ProgramHelpError as exc:
        return {"available": False, "reason": "not_runnable", "message": str(exc),
                "program": program, "options": []}
    return {"available": True, **payload}


@app.post("/api/jobs/{internal_name}/draft")
def recompute_draft(internal_name: str, req: DraftRequest):
    if internal_name in CUSTOM_JOB_DEFINITIONS:
        raise HTTPException(status_code=400, detail="Custom jobs don't use draft commands")
    try:
        # raw_job() (not _load_raw()[internal_name] directly) resolves a
        # job_catalog.TOMO_VARIANT_OF entry (TomoMotioncorr/TomoCtffind) to
        # its real RELION job class's raw data -- see its own docstring.
        raw = job_registry.raw_job(internal_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job type: {internal_name}")
    if req.overwrite_run_id:
        # Deliberately NOT guarded by _reject_relion_run here, unlike the
        # real start-run/abort/delete endpoints: recomputing a draft is
        # read-only (nothing is submitted or written), and a job RELION
        # itself ran still has real settings worth showing, editing, and
        # copying -- readable straight from its own job.star, the same way
        # note.txt is. overwrite_target_subdir now resolves a RELION-native
        # run_id too (see _resolve_overwrite_target), so this Just Works;
        # the ACTUAL Overwrite action stays blocked for these jobs via
        # start_run's own _reject_relion_run check below.
        try:
            output_subdir = run_manager.overwrite_target_subdir(req.overwrite_run_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    else:
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
    if req.overwrite_run_id:
        # Overwriting means re-running into that job's own directory. For a job
        # RELION owns, that would silently replace results its pipeline still
        # describes, with no way for this app to update RELION's record.
        _reject_relion_run(req.overwrite_run_id, "overwritten")

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
                # The picking jobs (Manualpick/TomoManualPick) only validate
                # their inputs here -- the real work is picking, done
                # afterward against this job's own directory via the Picker
                # button, not by this coroutine -- so a successful return
                # shouldn't complete the run. See start_custom_job's own
                # stays_running docstring; the "Done" button (set_status)
                # is what actually finishes it.
                stays_running=CUSTOM_JOB_DEFINITIONS[req.internal_name].get("is_picker", False),
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


def _reject_relion_run(run_id: str, action: str) -> None:
    """Imported RELION jobs are read-only here.

    RELION-US never writes `default_pipeline.star`, so it cannot keep RELION's
    own record straight if it aborts, re-runs or deletes a job RELION owns.
    Refusing is better than half-doing it and leaving the project's pipeline
    describing something that is no longer true.
    """
    if JobRunManager.is_relion_run(run_id):
        raise HTTPException(
            status_code=409,
            detail=(
                f"This job was run in RELION itself, not in RELION-US, so it "
                f"cannot be {action} from here — RELION-US doesn't write "
                f"RELION's pipeline file and can't keep its record consistent. "
                f"Use RELION's own GUI for that, or re-run the job here as a "
                f"new job."
            ),
        )


@app.get("/api/runs")
def list_runs():
    return run_manager.list_runs()


@app.post("/api/runs/{run_id}/abort")
async def abort_run(run_id: str):
    """Command Center 'Abort' action (real RELION 'Abort running')."""
    _reject_relion_run(run_id, "aborted")
    ok = await run_manager.abort_run(run_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Run is not currently running (or doesn't exist)")
    return {"ok": True}


@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str):
    """The picking jobs' "Continue" toolbar action -- see
    JobRunManager.resume_run's own docstring. Restricted to Manualpick/
    TomoManualPick here (not exposed generically): resuming a finished
    subprocess/compute job back to "running" has no real meaning -- there
    is no process left to have stopped."""
    _reject_relion_run(run_id, "resumed")
    run = run_manager.get(run_id)
    internal_name = run.internal_name if run else None
    if internal_name is None:
        history = project_manager.load_history(run_manager.project_dir)
        entry = next((h for h in history if h.get("run_id") == run_id), None)
        internal_name = entry.get("internal_name") if entry else None
    if internal_name is None or not CUSTOM_JOB_DEFINITIONS.get(internal_name, {}).get("is_picker"):
        raise HTTPException(
            status_code=400,
            detail="Only a manual-picking job (Manualpick/TomoManualPick) can be resumed.",
        )
    try:
        updated = await run_manager.resume_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if updated is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    return updated


class RunUpdateRequest(BaseModel):
    alias: str | None = None
    note: str | None = None
    status: str | None = None


@app.patch("/api/runs/{run_id}")
async def update_run(run_id: str, req: RunUpdateRequest):
    """Command Center 'Alias' / 'Edit Note' / 'Mark as finished'/'Mark as
    failed' actions, combined into one endpoint since they're all small,
    independent metadata edits. Any combination of fields may be given in
    one call; each is applied in turn.

    Everything is validated before anything is written, so a rejected status
    can't leave the alias/note edits from the same call partially applied."""
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

    _reject_relion_run(run_id, "edited")
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
            updated = await run_manager.set_status(run_id, req.status)
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
    _reject_relion_run(run_id, "deleted")
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


PREVIEW_MAX_ROWS_CAP = 2000


@app.get("/api/runs/{run_id}/files/preview")
def preview_run_file(run_id: str, path: str, max_rows: int = 300):
    """Outputs tab: click a .star filename (not its checkbox) to see its
    contents without downloading it. Parses every block in the file --
    RELION STAR files mix "list" blocks (single row of `_key value` pairs,
    e.g. model_general) with "loop_" blocks (multi-row tables, e.g.
    particles) -- and returns each in whatever shape it actually is, rather
    than assuming one or the other (see progress.py's
    _parse_model_star_cached for the bug that shape assumption caused
    elsewhere). Loop blocks are capped at max_rows: a data.star can have
    millions of particle rows, and this is a preview, not the download."""
    resolved = run_manager.resolve_output_file(run_id, path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="File not found in this job's output directory")
    if resolved.suffix.lower() != ".star":
        raise HTTPException(status_code=400, detail="Preview is only available for .star files")

    max_rows = max(1, min(max_rows, PREVIEW_MAX_ROWS_CAP))

    import json as _json
    import starfile

    try:
        raw = starfile.read(resolved, always_dict=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse STAR file: {exc}")

    blocks = []
    for name, block in raw.items():
        if isinstance(block, dict):
            blocks.append({
                "name": name,
                "kind": "list",
                "fields": [{"key": k, "value": v} for k, v in block.items()],
            })
        else:
            total_rows = len(block)
            preview_df = block.head(max_rows)
            # Round-tripping through pandas' own to_json/loads (rather than
            # .values.tolist()) is what turns NaN into null and numpy
            # scalars (int64/float64) into plain JSON-safe types -- both of
            # which FastAPI's default encoder chokes on straight from a
            # DataFrame.
            split = _json.loads(preview_df.to_json(orient="split", date_format="iso"))
            blocks.append({
                "name": name,
                "kind": "loop",
                "columns": split["columns"],
                "rows": split["data"],
                "total_rows": total_rows,
                "truncated": total_rows > max_rows,
            })

    return {"path": path, "blocks": blocks}


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


@app.get("/api/project/pipeline-sync")
def get_pipeline_sync():
    """Whether this project's jobs are recorded in RELION's own
    `default_pipeline.star`, so both GUIs see the same history.

    `available` is whether `relion_pipeliner` is installed at all — without it
    there is no safe way to touch that file, since RELION's own binary is what
    computes the node graph and honours the lock protocol.
    """
    pd = run_manager.project_dir
    return {
        "enabled": project_manager.pipeline_sync_setting(pd),
        "available": pipeline_bridge.is_available(),
        "pipeliner_path": pipeline_bridge.pipeliner_path(),
        "locked": pipeline_bridge.is_locked(pd),
        "project": str(pd),
    }


class PipelineSyncRequest(BaseModel):
    enabled: bool


@app.post("/api/project/pipeline-sync")
def set_pipeline_sync(req: PipelineSyncRequest):
    pd = run_manager.project_dir
    if req.enabled and not pipeline_bridge.is_available():
        raise HTTPException(
            status_code=409,
            detail=(
                f"{pipeline_bridge.PIPELINER_BINARY} is not on this machine's PATH. "
                "It ships with RELION, and RELION-US uses it rather than writing "
                "default_pipeline.star itself."
            ),
        )
    enabled = project_manager.set_pipeline_sync(pd, req.enabled)
    return {"enabled": enabled, "available": pipeline_bridge.is_available(),
            "locked": pipeline_bridge.is_locked(pd), "project": str(pd)}


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


class GlobalSettingsRequest(BaseModel):
    values: dict[str, Any]


@app.get("/api/settings")
def get_settings():
    """Global (per-user) stored defaults for the Settings popup — job-run
    field defaults and a few app-behavior knobs. Not project-scoped, unlike
    pipeline-sync above."""
    return {"settings": project_manager.load_global_settings()}


@app.put("/api/settings")
def put_settings(req: GlobalSettingsRequest):
    """Partial update — only keys present in `values` change; see
    save_global_settings' own merge behavior. Unknown keys are silently
    dropped, not an error, so a stale frontend build can't wedge bad data in."""
    return {"settings": project_manager.save_global_settings(req.values)}


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
    if JobRunManager.is_relion_run(run_id):
        # A job RELION itself ran: no live output to stream, but its real
        # option values come out of the job's own job.star.
        detail = run_manager.relion_run_detail(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown run_id")
        return {**detail, "stdout_lines": [], "stderr_lines": []}
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
    # @app.middleware("http") (the auth_gate above) only ever sees "http"
    # scope requests -- Starlette routes a websocket's ASGI scope around it
    # entirely, so the same check has to happen again here, before accept(),
    # or password protection would gate every page and every REST call except
    # the one carrying a job's live output.
    cfg = auth.load_config()
    if auth.is_enabled(cfg) and not auth.session_is_valid(
        websocket.cookies.get(auth.COOKIE_NAME), cfg
    ):
        await websocket.close(code=4401)
        return
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


@app.get("/api/runs/{run_id}/progress/iteration/{iteration}")
def run_progress_iteration(run_id: str, iteration: int):
    """One specific iteration's full class breakdown (see
    progress.read_iteration) -- what lets the Progress tab show any past
    iteration's images, not just whichever was newest when the popup opened
    or the last poll landed."""
    cwd = run_manager._resolve_run_cwd(run_id)
    if cwd is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    try:
        return progress.read_iteration(Path(cwd), iteration)
    except progress.ProgressError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/runs/{run_id}/orientation-distribution")
def run_orientation_distribution(run_id: str):
    """Viewing-direction 2D histogram for the most recent completed
    iteration (see progress.read_orientation_distribution) -- ON DEMAND
    only, triggered by a button in the frontend, never auto-polled: unlike
    every other Progress tab read, this one parses a per-PARTICLE file that
    can run into the tens of millions of rows."""
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
    if not internal_name or not progress.supports_orientation_distribution(internal_name):
        return {"available": False, "supported": False}
    data = progress.read_orientation_distribution(Path(cwd))
    data["supported"] = True
    return data


# --------------------------------------------------------------------------
# Analyze popup (Menu > Tools > Analyze) -- reads across a run's whole
# iteration history rather than just the latest, for the classification/
# refinement convergence charts. See analyze.py. Not gated by
# progress.supports_progress() the way the endpoints above are: the
# frontend's own tab/run-picker already only ever calls these for a run of
# the right job type, and a mismatched call just gets available: False back
# (no optimiser.star/model.star found), same as an unstarted run would.
# --------------------------------------------------------------------------


@app.get("/api/runs/{run_id}/analyze/convergence")
def analyze_convergence(run_id: str):
    cwd = run_manager._resolve_run_cwd(run_id)
    if cwd is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    return analyze.read_optimiser_series(Path(cwd))


@app.get("/api/runs/{run_id}/analyze/class-distribution")
def analyze_class_distribution(run_id: str):
    cwd = run_manager._resolve_run_cwd(run_id)
    if cwd is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    return analyze.read_class_distribution_series(Path(cwd))


@app.get("/api/runs/{run_id}/analyze/class-fsc")
def analyze_class_fsc(run_id: str, iteration: int | None = Query(None)):
    cwd = run_manager._resolve_run_cwd(run_id)
    if cwd is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    return analyze.read_class_fsc(Path(cwd), iteration)


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


@app.get("/api/runs/{run_id}/ctf-qc")
def run_ctf_qc(run_id: str):
    """Every micrograph/tilt-image's CTF fit numbers for a CTF Estimation
    job's QC charts (see ctf_qc.read_ctf_qc) -- end-of-job only, since
    RELION itself only writes this once, when the whole job finishes."""
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
    if not internal_name or not ctf_qc.supports_ctf_qc(internal_name):
        return {"available": False, "supported": False, "count": 0, "micrographs": []}
    data = ctf_qc.read_ctf_qc(Path(cwd))
    data["supported"] = True
    return data


@app.get("/api/runs/{run_id}/ctf-qc/thumbnail")
def run_ctf_qc_thumbnail(run_id: str, reference: str = Query(...)):
    cwd = run_manager._resolve_run_cwd(run_id)
    if cwd is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    try:
        png = ctf_qc.render_ctf_thumbnail(Path(cwd), reference)
    except ctf_qc.ProgressError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Immutable: RELION never rewrites a finished job's CTF images, so the
    # browser can keep these without re-fetching while the popup is open.
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


# --------------------------------------------------------------------------
# Manual picking (Manualpick / TomoManualPick) -- the in-browser picker
# (viz.py's viewer, with picking enabled) saves/loads into a specific job's
# own output directory, rather than the standalone Visualize tool's
# arbitrary-file read-only mode above. See manual_pick.py's module docstring
# for the STAR formats this writes.
# --------------------------------------------------------------------------


def _manual_pick_job_dir(run_id: str) -> Path:
    """The output directory of a manual-picking job, by run_id -- 404 if the
    run doesn't exist, matching every other run_id-scoped endpoint."""
    run = run_manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    return Path(run.cwd)


class SpaPickSaveRequest(BaseModel):
    mic_path: str
    picks: list[dict]


@app.get("/api/manual-pick/{run_id}/spa/micrographs")
def manual_pick_spa_micrographs(run_id: str, fn_in: str = Query(...)):
    try:
        return {"micrographs": manual_pick.list_spa_micrographs(run_manager.project_dir, fn_in)}
    except manual_pick.ManualPickError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/manual-pick/{run_id}/spa/load")
def manual_pick_spa_load(run_id: str, mic_path: str = Query(...)):
    job_dir = _manual_pick_job_dir(run_id)
    return {"picks": manual_pick.load_spa_picks(run_manager.project_dir, job_dir, mic_path)}


@app.post("/api/manual-pick/{run_id}/spa/save")
def manual_pick_spa_save(run_id: str, req: SpaPickSaveRequest):
    job_dir = _manual_pick_job_dir(run_id)
    try:
        return manual_pick.save_spa_picks(run_manager.project_dir, job_dir, req.mic_path, req.picks)
    except (manual_pick.ManualPickError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class TomoPickSaveRequest(BaseModel):
    tomo_name: str
    picks: list[dict]
    tomograms_star_path: str


@app.get("/api/manual-pick/{run_id}/tomo/load")
def manual_pick_tomo_load(run_id: str, tomo_name: str = Query(...)):
    job_dir = _manual_pick_job_dir(run_id)
    return {"picks": manual_pick.load_tomo_picks(run_manager.project_dir, job_dir, tomo_name)}


@app.post("/api/manual-pick/{run_id}/tomo/save")
def manual_pick_tomo_save(run_id: str, req: TomoPickSaveRequest):
    job_dir = _manual_pick_job_dir(run_id)
    try:
        return manual_pick.save_tomo_picks(
            run_manager.project_dir, job_dir, req.tomo_name, req.picks, req.tomograms_star_path)
    except (manual_pick.ManualPickError, viz.VizError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers request this automatically on every page load; without a
    route it's a spurious 404 in server logs and in browser-automation
    error collectors (Playwright, etc.). No icon file yet, so just answer
    with an empty 204 instead of a 404."""
    return Response(status_code=204)


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles sends only ETag/Last-Modified by default, no
    Cache-Control -- browsers then apply their own heuristic freshness
    lifetime and can silently keep serving a stale app.js/style.css for a
    long time after a code change, with no failed request to notice. Forcing
    revalidation on every request costs a conditional GET (cheap, usually a
    304) but guarantees the browser never runs code/styles older than what's
    on disk, which matters for a tool developed and used on the same
    machine like this one.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Serve the frontend last, so /api/* and /ws/* above take precedence.
app.mount("/", _NoCacheStaticFiles(directory=str(APP_DIR.parent / "frontend"), html=True), name="frontend")
