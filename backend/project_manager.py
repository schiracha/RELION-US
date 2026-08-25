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
import os
import re
import time
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


# --------------------------------------------------------------------------
# Reading RELION's own pipeline
#
# A project built in RELION's own GUI has a `default_pipeline.star` holding
# the whole job history: a global job counter and one row per process. Opening
# such a project without reading it is what made RELION-US restart numbering at
# job001 in a project already on job012 -- i.e. draft an output path pointing
# straight at somebody's existing results.
#
# This module only ever *reads* the file. When two-way sync is on, RELION-US's
# own runs do get recorded in it -- but through RELION's own `relion_pipeliner`
# binary, never by writing the STAR format here (see pipeline_bridge.py for
# why: five linked tables, a node graph computed by RELION's C++, and a lock
# protocol shared with any open RELION GUI).
#
# Schema verified against RELION's PipeLine::write() (src/pipeliner.cpp) and
# the label table in src/metadata_label.h.
# --------------------------------------------------------------------------

PIPELINE_GENERAL_BLOCK = "pipeline_general"
PIPELINE_PROCESSES_BLOCK = "pipeline_processes"
PIPELINE_INPUT_EDGES_BLOCK = "pipeline_input_edges"
PIPELINE_OUTPUT_EDGES_BLOCK = "pipeline_output_edges"
JOB_DIR_RE = re.compile(r"job(\d+)/?$")

# procstatus_type2label in RELION's src/pipeline_jobs.h, mapped onto the
# statuses this app already uses in its own history.
RELION_STATUS_MAP = {
    "Running": "running",
    "Scheduled": "pending",
    "Succeeded": "completed",
    "Failed": "failed",
    "Aborted": "aborted",
}


def _job_number_from_name(name: str) -> int:
    """5 from "Class2D/job005/". RELION's numbering is one counter for the
    whole project, so the number alone is meaningful."""
    m = JOB_DIR_RE.search(str(name).rstrip("/"))
    return int(m.group(1)) if m else 0


def read_relion_pipeline(project_dir: Path) -> dict[str, Any]:
    """RELION's own job history for this project.

    Returns {"job_counter": int|None, "processes": [...], "producers": {...}};
    an empty result for a project with no `default_pipeline.star` (one
    RELION-US started itself) or an unreadable one. Never raises: a project
    must still open when its pipeline file is from a newer RELION,
    half-written, or corrupt.

    `job_counter` is RELION's `rlnPipeLineJobCounter` -- the number it would
    give the *next* job.

    `producers` is `{process_name: [producer_process_name, ...]}`, RELION's
    own computed job graph -- not a guess from directory paths, but read
    straight from `pipeline_input_edges`/`pipeline_output_edges`, the tables
    RELION's own `getCommands<Job>Job()` populated when each job ran. This is
    what lets the Command Center's network view draw real lineage for a
    project built entirely in RELION's GUI, where nothing here ever ran
    `_detect_inputs()` on any job's options.
    """
    star_path = project_dir / RELION_PIPELINE_STAR
    if not star_path.exists():
        return {"job_counter": None, "processes": [], "producers": {}}

    try:
        from converters.star_io import StarDocument

        doc = StarDocument.read(star_path)
    except Exception:
        return {"job_counter": None, "processes": [], "producers": {}}

    job_counter = None
    try:
        # `pipeline_general` is a STAR *list* block (one label per line, no
        # loop_), which `starfile` hands back as a plain dict rather than a
        # DataFrame -- so this cannot assume the DataFrame API the loop blocks
        # use. Both shapes are accepted here rather than relying on which one
        # a given starfile version returns.
        general = doc.block(PIPELINE_GENERAL_BLOCK)
        if isinstance(general, dict):
            raw = general.get("rlnPipeLineJobCounter")
        elif general is not None and not general.empty:
            raw = general.iloc[0].get("rlnPipeLineJobCounter")
        else:
            raw = None
        if raw is not None:
            job_counter = int(float(raw))
    except Exception:
        job_counter = None

    processes: list[dict[str, Any]] = []
    try:
        df = doc.block(PIPELINE_PROCESSES_BLOCK)
    except Exception:
        df = None
    if df is not None and not df.empty and "rlnPipeLineProcessName" in df.columns:
        for _, row in df.iterrows():
            name = str(row["rlnPipeLineProcessName"]).strip()
            if not name:
                continue
            alias = str(row.get("rlnPipeLineProcessAlias", "") or "").strip()
            processes.append({
                "name": name.rstrip("/"),
                # RELION writes the literal string "None" for "no alias".
                "alias": "" if alias in ("", "None") else alias.rstrip("/"),
                "type_label": str(row.get("rlnPipeLineProcessTypeLabel", "") or ""),
                "status_label": str(row.get("rlnPipeLineProcessStatusLabel", "") or ""),
                "job_number": _job_number_from_name(name),
            })

    # RELION's own node graph: which process produced each named output
    # file ("Import/job001/tilt_series.star" -> "Import/job001"), then which
    # processes consumed that file as an input ("Import/job001" ->
    # "MotionCorr/job002"). Chaining the two gives process-to-process edges
    # -- the same graph RELION's own GUI draws, computed by each job's real
    # command builder rather than guessed from directory names.
    node_producer: dict[str, str] = {}
    try:
        df_out = doc.block(PIPELINE_OUTPUT_EDGES_BLOCK)
    except Exception:
        df_out = None
    if df_out is not None and not df_out.empty and "rlnPipeLineEdgeProcess" in df_out.columns:
        for _, row in df_out.iterrows():
            proc = str(row.get("rlnPipeLineEdgeProcess", "") or "").strip().rstrip("/")
            node = str(row.get("rlnPipeLineEdgeToNode", "") or "").strip()
            if proc and node:
                node_producer[node] = proc

    producers: dict[str, list[str]] = {}
    try:
        df_in = doc.block(PIPELINE_INPUT_EDGES_BLOCK)
    except Exception:
        df_in = None
    if df_in is not None and not df_in.empty and "rlnPipeLineEdgeProcess" in df_in.columns:
        for _, row in df_in.iterrows():
            proc = str(row.get("rlnPipeLineEdgeProcess", "") or "").strip().rstrip("/")
            node = str(row.get("rlnPipeLineEdgeFromNode", "") or "").strip()
            src = node_producer.get(node)
            if proc and src and src != proc:
                bucket = producers.setdefault(proc, [])
                if src not in bucket:
                    bucket.append(src)

    return {"job_counter": job_counter, "processes": processes, "producers": producers}


def relion_job_numbers(project_dir: Path) -> set[int]:
    """Every job number RELION's own pipeline already accounts for, plus the
    counter it would hand out next. Used so this app's numbering continues a
    project rather than colliding with it."""
    info = read_relion_pipeline(project_dir)
    numbers = {p["job_number"] for p in info["processes"] if p["job_number"]}
    if info["job_counter"]:
        # The counter is the *next* number RELION would use, so everything
        # below it is spoken for -- including jobs deleted from the pipeline,
        # whose directories may well still be on disk.
        numbers.add(info["job_counter"] - 1)
    return numbers


def read_relion_job_options(job_dir: Path) -> dict[str, str]:
    """The option values RELION saved for one job, from its own `job.star`.

    RELION writes every JobOption into a `joboptions_values` block
    (`rlnJobOptionVariable` / `rlnJobOptionValue`, see RelionJob::write in
    src/pipeline_jobs.cpp) when a job runs -- the same file its GUI reads back
    to reopen a job. Keys are the same option keys this app's forms use, so an
    old job reopens with the settings it actually ran with.

    Returns {} when there's no job.star: RELION 3.0 and earlier wrote a
    `run.job` in a different format, and a job directory can be missing either.
    """
    star_path = Path(job_dir) / "job.star"
    if not star_path.exists():
        return {}
    try:
        from converters.star_io import StarDocument

        df = StarDocument.read(star_path).block("joboptions_values")
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    if "rlnJobOptionVariable" not in df.columns or "rlnJobOptionValue" not in df.columns:
        return {}
    return {
        str(k): str(v)
        for k, v in zip(df["rlnJobOptionVariable"], df["rlnJobOptionValue"])
    }


_NOTE_COMMAND_RE = re.compile(r"with the following command\(s\):\s*\n(.+?)\s*\n")


def read_relion_last_command(job_dir: Path) -> str:
    """The exact command RELION's own GUI most recently ran for this job,
    read straight from its own `note.txt`.

    RELION appends (never overwrites, src/pipeliner.cpp's fn_note is opened
    with std::ofstream::app) a block to note.txt every time a job runs or
    is overwritten:

        ++++ Executing new job on <date>
        ++++ with the following command(s):
        <the real command, verbatim>
        ++++

    -- so a job that's been overwritten several times has one block per
    run; the LAST one is what actually produced the job's current output.
    This is the one place a RELION-native job's real command survives at
    all (RELION's own pipeline file records none, same as timestamps --
    see estimate_job_timestamps), which is what makes an old job's command
    readable/editable/copy-pasteable here rather than starting blank.

    Returns "" if note.txt doesn't exist or doesn't match this format
    (RELION 3.0 and earlier wrote a differently-shaped note, or the file
    was hand-edited) -- not an error, just nothing to show.
    """
    note_path = Path(job_dir) / "note.txt"
    if not note_path.exists():
        return ""
    try:
        text = note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    matches = _NOTE_COMMAND_RE.findall(text)
    return matches[-1].strip() if matches else ""


# RELION writes one of these into a job's own directory the moment the
# program exits (src/pipeline_control.cpp) -- see estimate_job_timestamps.
_EXIT_MARKER_FILENAMES = (
    "RELION_JOB_EXIT_SUCCESS",
    "RELION_JOB_EXIT_FAILURE",
    "RELION_JOB_EXIT_ABORTED",
)
# Written once, at job start, and (for these two) never touched again --
# job.star is RELION's own registration file; run.out/run.err are RELION-US's
# equivalent tee target for jobs it ran itself (see job_runner.py's
# _run_subprocess). Checked in this order because job.star is the most
# specific signal.
_START_MARKER_FILENAMES = ("job.star", "run.job", "run.out", "run.err")


def estimate_job_timestamps(job_dir: Path, status: str) -> tuple[float | None, float | None]:
    """Best-effort (started_at, ended_at) for a job with no recorded timing
    of its own, inferred from the mtimes of files RELION (or RELION-US)
    writes once at a specific moment rather than the job directory's own
    mtime (which changes on literally any touch -- browsing it, an rsync,
    RELION's own GUI re-saving a note -- and is what the earlier "leave it
    blank" design here was specifically avoiding).

    This is still only an ESTIMATE, not ground truth: if the job directory
    was copied, rsynced, or the whole project migrated to a new machine
    *after* the job actually ran, every file's mtime reflects the copy, not
    the original run -- confirmed against a real downloaded EMPIAR project,
    where every job's job.star mtime landed within the same few minutes
    (the download), regardless of when the original lab actually ran them
    over what would have been weeks. Callers must treat and label whatever
    this returns as an estimate, never as an authoritative value -- see the
    `timestamp_estimated` flag on the callers of this function.

    Returns (None, None) if the directory doesn't exist or has none of the
    expected marker files. `ended_at` is only ever estimated for a status
    that RELION itself would call finished (not "running"/"pending" --
    there is no sensible "end" for a job still going).
    """
    job_dir = Path(job_dir)
    if not job_dir.is_dir():
        return None, None

    def _mtime(name: str) -> float | None:
        p = job_dir / name
        try:
            return p.stat().st_mtime if p.is_file() else None
        except OSError:
            return None

    started_at = next(
        (m for name in _START_MARKER_FILENAMES if (m := _mtime(name)) is not None), None
    )

    ended_at = None
    if status not in ("running", "pending"):
        ended_at = next(
            (m for name in _EXIT_MARKER_FILENAMES if (m := _mtime(name)) is not None), None
        )
        if ended_at is None:
            # No exit marker (a custom-bridge job, or one that predates
            # --pipeline_control being appended) -- the later of run.out/
            # run.err's mtime is the last moment anything was logged, which
            # is as close to "when it stopped" as this app can infer.
            candidates = [m for name in ("run.out", "run.err") if (m := _mtime(name)) is not None]
            ended_at = max(candidates) if candidates else None

    return started_at, ended_at


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

    # RELION appends a sub-label to the base type for many jobs
    # (`label += ".movies"`, `".em"`, `".topaz"` -- 35 places in
    # pipeline_jobs.cpp), so a real project records "relion.class2d.em" while
    # this app's catalog holds "relion.class2d". Match on the base label.
    def _matches(label: str, base_labels: set[str]) -> bool:
        return any(label == b or label.startswith(b + ".") for b in base_labels)

    labels_used = set(df["rlnPipeLineProcessTypeLabel"].astype(str))
    has_tomo = any(_matches(l, tomo_labels) for l in labels_used)
    has_spa = any(_matches(l, spa_labels) for l in labels_used)

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


# --------------------------------------------------------------------------
# Recent-projects cache
#
# Per *user*, not per project: it has to survive switching away from a project
# and outlive any single project directory, so it lives under the user's config
# dir (XDG_CONFIG_HOME, else ~/.config) rather than inside a `.relion_us/`
# marker. Holding it here also means a user with several projects on a shared
# cluster filesystem gets their own list, not their group's.
#
# Only paths and timestamps are stored — nothing about the data, so the file is
# safe to sync or delete. A missing/corrupt file is treated as "no recents".
# --------------------------------------------------------------------------

RECENTS_FILENAME = "recent_projects.json"
RECENTS_LIMIT = 15


def config_root() -> Path:
    """The user's RELION-US config directory: `$XDG_CONFIG_HOME/relion_us`,
    or `~/.config/relion_us` if that's unset. Shared by everything that is
    per-*user* rather than per-project -- the recent-projects cache here and
    `backend/auth.py`'s login config -- so both land wherever the user's
    other config does, and so tests can redirect either one (via
    XDG_CONFIG_HOME) without touching a real home directory."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "relion_us"


def recents_path() -> Path:
    """Location of the recent-projects cache."""
    return config_root() / RECENTS_FILENAME


def load_recent_projects() -> list[dict[str, Any]]:
    """Recently opened project directories, most recent first.

    Each entry gains two freshly-computed fields the cache does not store,
    because either can change without this app being involved (someone deletes
    a folder, or RELION creates default_pipeline.star in it):

      exists      — the directory is still there
      is_project  — it still looks like a RELION project

    Stale entries are returned rather than silently dropped, so the user can
    see that a project they remember is gone instead of wondering where it
    went in the list.
    """
    p = recents_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []

    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        path = Path(str(item["path"]))
        try:
            exists = path.is_dir()
        except OSError:
            exists = False
        out.append({
            "path": str(path),
            "name": path.name or str(path),
            "last_opened": item.get("last_opened"),
            "exists": exists,
            "is_project": is_relion_project(path) if exists else False,
        })
    return out[:RECENTS_LIMIT]


def _write_recents(entries: list[dict[str, Any]]) -> None:
    """Persist the cache, keeping only the stored fields. Never raises: a
    read-only or full home directory must not break opening a project, which
    is the actual task the user asked for."""
    slim = [
        {"path": e["path"], "last_opened": e.get("last_opened")}
        for e in entries[:RECENTS_LIMIT]
    ]
    p = recents_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(slim, indent=2), encoding="utf-8")
    except OSError:
        pass


def remember_project(project_dir: Path, when: float | None = None) -> None:
    """Record a project as most-recently-opened. Called on every successful
    switch/init and at startup, so the list reflects real use rather than
    needing the user to curate it. Re-opening an existing entry moves it to
    the top instead of duplicating it (compared on the resolved path, so
    `.`, a symlink and an absolute path are one entry)."""
    try:
        resolved = str(Path(project_dir).expanduser().resolve())
    except OSError:
        resolved = str(project_dir)
    stamp = time.time() if when is None else when
    kept = [e for e in load_recent_projects() if e["path"] != resolved]
    _write_recents([{"path": resolved, "last_opened": stamp}] + kept)


def forget_project(project_dir: str | Path) -> None:
    """Drop one entry from the cache (the ✕ next to a recent project). Only
    removes the bookmark — never touches the directory itself."""
    try:
        resolved = str(Path(project_dir).expanduser().resolve())
    except OSError:
        resolved = str(project_dir)
    raw = str(project_dir)
    _write_recents([
        e for e in load_recent_projects() if e["path"] not in (resolved, raw)
    ])


# --------------------------------------------------------------------------
# Two-way pipeline sync, per project
#
# Opt-in per project rather than globally: whether RELION-US should add entries
# to `default_pipeline.star` is a property of the project, not of the machine.
# A colleague's project you were asked to look at should not gain rows in
# RELION's own record because you ran one job in it.
# --------------------------------------------------------------------------

SETTINGS_FILENAME = "settings.json"
PIPELINE_SYNC_KEY = "pipeline_sync"


def _settings_path(project_dir: Path) -> Path:
    return Path(project_dir) / MARKER_DIRNAME / SETTINGS_FILENAME


def load_settings(project_dir: Path) -> dict[str, Any]:
    p = _settings_path(project_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(project_dir: Path, settings: dict[str, Any]) -> None:
    marker = Path(project_dir) / MARKER_DIRNAME
    try:
        marker.mkdir(parents=True, exist_ok=True)
        _settings_path(project_dir).write_text(
            json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        # A read-only project must still be usable; the setting simply won't
        # persist past this backend session.
        pass


def pipeline_sync_setting(project_dir: Path) -> bool:
    """Whether this project wants RELION-US's runs recorded in RELION's own
    pipeline. Default off: writing another tool's state file is something to
    ask for, not something to inherit."""
    return bool(load_settings(project_dir).get(PIPELINE_SYNC_KEY, False))


def set_pipeline_sync(project_dir: Path, enabled: bool) -> bool:
    settings = load_settings(project_dir)
    settings[PIPELINE_SYNC_KEY] = bool(enabled)
    save_settings(project_dir, settings)
    return pipeline_sync_setting(project_dir)


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
