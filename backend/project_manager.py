"""
project_manager.py — lets RELION-US point at any RELION project directory
at runtime instead of a folder hardcoded relative to where the backend
happens to live ("Do I have to run this from the working directory?").
Also owns lightweight persistence of run-history *summaries* per project,
so switching projects and reloading the GUI shows that project's own job
history rather than whatever the app's in-memory state happens to contain
(which is reset on every backend restart).

What counts as a RELION project directory:
A folder is recognized as a RELION project the same way RELION's own GUI
effectively does — it already has `default_pipeline.star` (RELION's own
pipeline-state file, written by the real GUI or by `relion_pipeliner`), OR
it has a `.relion_us/` marker directory created by RELION-US the first
time it was pointed at that folder. Anything else is treated as "not a
project yet" and the frontend prompts: start a new project here, or pick
a different folder.

(If you have a project folder from before this app was renamed from
"RELION Job Manager" to RELION-US, it'll have an old `.relion_job_manager/`
marker instead — either rename that folder to `.relion_us/`, or just start
the project fresh here; either is safe, since the marker only ever holds a
history summary, never anything RELION itself reads.)

We deliberately do NOT fabricate a `default_pipeline.star` ourselves when
starting a "new project" — that's RELION's own file format, and RELION's
own tools create it correctly the first time a job actually runs there.
Writing a fake/empty one here risks the exact class of problem this whole
app exists to avoid (something inserted under the hood that the user didn't
ask for and can't see). We only ever create our own marker + history file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MARKER_DIRNAME = ".relion_us"
HISTORY_FILENAME = "run_history.json"
RELION_PIPELINE_STAR = "default_pipeline.star"

PERMISSION_ERROR_MESSAGE = (
    "Improper permissions for this location, check user and either change "
    "location or create folder in another program"
)


class PermissionDeniedError(Exception):
    """Raised when a filesystem write (new folder, new project marker)
    fails because of permissions, so callers can show a specific, actionable
    message instead of a raw OSError string."""


def is_relion_project(path: Path) -> bool:
    """True if `path` already looks like a RELION project (real RELION
    pipeline file present) or has already been initialised as one by this
    app on a previous visit."""
    if not path.is_dir():
        return False
    return (path / RELION_PIPELINE_STAR).exists() or (path / MARKER_DIRNAME).is_dir()


def detect_pipeline_hint(project_dir: Path) -> str:
    """Best-effort 'tomo' | 'spa' | 'mixed' | 'unknown' guess, used only to
    optionally auto-select the Jobs-list SPA/Tomo/All toggle when a project
    loads (see job_catalog.py's PIPELINE_SPA_ONLY/PIPELINE_TOMO_ONLY
    docstring for why there's no single project-type flag to just read).

    The signal used: which job *types* this project has actually run,
    recorded per-process as rlnPipeLineProcessTypeLabel in
    default_pipeline.star's `pipeline_processes` block. 'unknown' covers a
    brand-new project (no default_pipeline.star yet) or one whose
    pipeline_processes block is empty/unreadable — the frontend leaves the
    toggle on its current/manual setting in that case, since there's
    nothing here to hint at. 'mixed' covers a project that has run both
    SPA-only and Tomo-only job types (e.g. reused for two datasets) — the
    frontend also leaves the toggle alone there, since neither is a better
    default than the other.

    This is intentionally a local import: is_relion_project() above (which
    every folder-browse listing calls, via list_dir()) stays dependency
    -light (json/pathlib only); only this one best-effort, once-per-project
    -load call needs pandas/starfile."""
    star_path = project_dir / RELION_PIPELINE_STAR
    if not star_path.exists():
        return "unknown"

    try:
        from converters.star_io import StarDocument

        doc = StarDocument.read(star_path)
        df = doc.block("pipeline_processes")
    except Exception:
        # Any parse hiccup (unexpected schema, corrupt file, missing
        # optional dependency) just falls back to "no hint" rather than
        # blocking project load over a purely cosmetic feature.
        return "unknown"

    if df.empty or "rlnPipeLineProcessTypeLabel" not in df.columns:
        return "unknown"

    from job_catalog import CUSTOM_JOBS, JOB_CATALOG, PIPELINE_SPA_ONLY, PIPELINE_TOMO_ONLY

    tomo_labels = {JOB_CATALOG[n][0] for n in PIPELINE_TOMO_ONLY if n in JOB_CATALOG}
    tomo_labels |= {CUSTOM_JOBS[n]["label_new"] for n in PIPELINE_TOMO_ONLY if n in CUSTOM_JOBS}
    spa_labels = {JOB_CATALOG[n][0] for n in PIPELINE_SPA_ONLY if n in JOB_CATALOG}

    labels_used = set(df["rlnPipeLineProcessTypeLabel"].astype(str))
    has_tomo = not labels_used.isdisjoint(tomo_labels)
    has_spa = not labels_used.isdisjoint(spa_labels)

    if has_tomo and has_spa:
        return "mixed"
    if has_tomo:
        return "tomo"
    if has_spa:
        return "spa"
    return "unknown"


def create_folder(path: Path) -> None:
    """Create a new folder (and any missing parents) — used by the Change
    Project browser's "Create Folder" action. Raises PermissionDeniedError
    on a permissions failure rather than letting a raw OSError/PermissionError
    propagate, so the frontend can show something the user can act on."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise PermissionDeniedError(PERMISSION_ERROR_MESSAGE) from exc


def init_new_project(path: Path) -> None:
    """Mark `path` as a project this app manages. Idempotent/safe to call
    on a folder that's already a real RELION project (e.g. one that has
    default_pipeline.star but was never opened in this app before) — it
    just adds our own bookkeeping alongside it."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        marker = path / MARKER_DIRNAME
        marker.mkdir(exist_ok=True)
        history_file = marker / HISTORY_FILENAME
        if not history_file.exists():
            history_file.write_text("[]")
    except PermissionError as exc:
        raise PermissionDeniedError(PERMISSION_ERROR_MESSAGE) from exc


def _history_path(project_dir: Path) -> Path:
    return project_dir / MARKER_DIRNAME / HISTORY_FILENAME


def load_history(project_dir: Path) -> list[dict[str, Any]]:
    """Best-effort load of persisted run summaries for a project. Returns
    [] for a project that has no history yet (or whose history file is
    missing/corrupt) rather than raising — a bad history file should never
    block the GUI from opening a project."""
    p = _history_path(project_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_history(project_dir: Path, entries: list[dict[str, Any]]) -> None:
    marker = project_dir / MARKER_DIRNAME
    marker.mkdir(exist_ok=True)
    _history_path(project_dir).write_text(json.dumps(entries, indent=2))


def list_dir(path: Path) -> dict[str, Any]:
    """Server-side folder listing for the 'select folder' UI. This has to
    be server-side (not a plain HTML file picker) because the backend may
    be running on a different, remote machine (e.g. an HPC cluster login
    node) than the browser viewing the page — the browser's own filesystem
    isn't the one the project lives on.

    Raises FileNotFoundError / NotADirectoryError for the caller to turn
    into a clean 4xx response.
    """
    if not path.exists():
        raise FileNotFoundError(str(path))
    if not path.is_dir():
        raise NotADirectoryError(str(path))

    entries = []
    try:
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith("."):
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            entries.append({"name": child.name, "is_dir": is_dir})
    except PermissionError:
        pass

    parent = path.parent
    return {
        "path": str(path),
        "parent": str(parent) if parent != path else None,
        "entries": entries,
        "is_relion_project": is_relion_project(path),
    }
