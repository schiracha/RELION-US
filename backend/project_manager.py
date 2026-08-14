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
    be running on a different, remote machine (e.g. a Rivanna/Afton login
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
