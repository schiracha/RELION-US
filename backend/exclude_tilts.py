"""
exclude_tilts.py — writes RELION-compatible tilt-series STAR output for the
browser-based tilt-image reviewer (TomoExcludeTiltImages), which replaces
relion_tomo_exclude_tilt_images' own napari GUI (a desktop window -- see
tomography_python_programs/exclude_tilt_images/_cli.py: it unconditionally
does `napari.Viewer(); ...; napari.run()`, with no headless/non-interactive
flag at all) with a plain checkbox list rendered in the in-browser viewer.

Data-level shape confirmed by reading the real Python package directly (not
guessed): tomography_python_programs._metadata_models.relion.tilt_series_set.
RlnTiltSeriesSet (from_star_file / write_star_file) and .exclude_tilt_images.
relion_tilt_image_excluder.RelionTiltImageExcluderWidget.save_output --
"excluding" an image is nothing more than a boolean-mask filter dropping rows
from each tilt series' own per-image DataFrame (sorted by
rlnTomoNominalStageTiltAngle), then writing one job-level "global" STAR table
(one row per tilt series, pointing at that series' own STAR file) plus one
per-series STAR file each. No napari/GUI dependency is needed to REPRODUCE
this file format, only to let a human interactively choose which rows to
drop -- exactly the same reasoning manual_pick.py documents for Manualpick.

Row identity: rlnMicrographMovieName (unique per image within one tilt
series -- confirmed against a real CtfFind tilt_series/*.star, e.g.
"frames/TS_01_000_0.0.mrc"). There is no separate "excluded" flag anywhere in
the real output format -- an excluded row is simply absent from the written
per-series STAR -- so THIS module never invents one either. Instead, "is
image X currently excluded" is answered by comparing the ORIGINAL per-series
STAR (read fresh from the job's own `in_tiltseries` input every time, never
cached) against whatever this job has itself written so far into its own
`tilt_series/<name>.star` copy: present in the original but absent from the
job's own copy == currently excluded. This needs no sidecar state file, and
naturally supports re-including something excluded in an earlier save.

Immediately-usable default: unlike SPA/tomo manual picking (where an unpicked
job has literally no particles yet), a tilt-series job has an obvious,
non-destructive "nothing chosen yet" state -- keep every tilt image. See
write_passthrough(), called once at job start (custom_jobs.
run_exclude_tilt_images) so this job's output is already valid for
downstream jobs (Reconstruct Tomograms, TomoSubtomo, ...) even before the
user opens the reviewer at all.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Optional

import pandas as pd

import viz

TILT_SERIES_DIRNAME = "tilt_series"
JOB_GLOBAL_STAR_NAME = "selected_tilt_series.star"
JOB_GLOBAL_BLOCK_NAME = "global"

# Columns on the job's own "global" table (one row per tilt series) -- the
# same shape TomoImport/TomoMotioncorr/TomoCtffind all produce/consume, per
# data/job_definitions_raw.json's TomoExcludeTiltImages.in_tiltseries help
# text ("Input global tilt series star file.").
GLOBAL_TOMO_NAME_COL = "rlnTomoName"
GLOBAL_STARFILE_COL = "rlnTomoTiltSeriesStarFile"

# Per-image columns on one tilt series' own STAR file -- confirmed against a
# real CtfFind tilt_series/TS_*.star (RELION 5.0.1 tomography tutorial).
MOVIE_COL = "rlnMicrographMovieName"
MIC_NAME_COL = "rlnMicrographName"
TILT_ANGLE_COL = "rlnTomoNominalStageTiltAngle"
PRE_EXPOSURE_COL = "rlnMicrographPreExposure"
CTF_MAXRES_COL = "rlnCtfMaxResolution"
MOTION_COL = "rlnAccumMotionTotal"


class ExcludeTiltsError(Exception):
    """Raised for a bad request (unreadable path, unknown tomogram, etc.);
    the API turns this into a 400."""


# Per-job_dir lock guarding every function below that reads-then-writes this
# job's own output files (write_passthrough, clear_exclusions,
# save_tilt_series_exclusions). Job start (custom_jobs.run_exclude_tilt_
# images, via asyncio.to_thread) and a save/overwrite request from the
# browser (main.py's plain `def` endpoints, which FastAPI already runs in
# its own threadpool) can genuinely run concurrently on separate threads --
# without this, one could delete/overwrite files the other is mid-read on
# (e.g. clear_exclusions unlinking selected_tilt_series.star while a save's
# _upsert_job_global_star is reading it), corrupting or losing other tilt
# series' already-saved state.
_JOB_LOCKS: dict[str, threading.Lock] = {}
_JOB_LOCKS_GUARD = threading.Lock()


def _job_lock(job_dir: Path) -> threading.Lock:
    key = str(Path(job_dir).resolve())
    with _JOB_LOCKS_GUARD:
        lock = _JOB_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _JOB_LOCKS[key] = lock
        return lock


def _sanitize(name: str) -> str:
    """A tilt series name, made safe as a flat filename fragment -- same
    rule as manual_pick._sanitize_relpath, kept collision-free even though
    real RELION-5 tomogram names are already filesystem-safe in practice."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name).strip("/"))


def _read_star_blocks(path: Path) -> dict:
    import starfile

    # rlnTomoName is often purely numeric (e.g. "01") in real tutorial
    # datasets -- without this, starfile/pandas infers it as an int column,
    # silently corrupting any name with a leading zero or non-numeric
    # variant on round-trip (confirmed against a real CtfFind tilt_series_
    # ctf.star: rlnTomoName values like "TS_01" survive, but a purely
    # numeric project's "01" would come back as 1).
    return starfile.read(path, always_dict=True, parse_as_string=[GLOBAL_TOMO_NAME_COL])


def _read_global_table(project_dir: Path, in_tiltseries: str):
    """The job's input `in_tiltseries` -- a RELION-5 tomography tilt-series
    SET star, one row per tilt series, each pointing at that series' own
    per-image STAR file (rlnTomoTiltSeriesStarFile)."""
    p = viz._safe(project_dir, in_tiltseries)
    if not p.is_file():
        raise ExcludeTiltsError(f"tilt series STAR not found: {in_tiltseries}")
    blocks = _read_star_blocks(p)
    for df in blocks.values():
        if hasattr(df, "columns") and GLOBAL_TOMO_NAME_COL in df.columns and GLOBAL_STARFILE_COL in df.columns:
            return df
    raise ExcludeTiltsError(
        f"{p.name}: no {GLOBAL_TOMO_NAME_COL}/{GLOBAL_STARFILE_COL} columns found "
        f"(expected a RELION-5 tomography tilt-series-set STAR, e.g. CtfFind's own "
        f"tilt_series_ctf.star)."
    )


def _global_row(df, tomo_name: str):
    matches = df[df[GLOBAL_TOMO_NAME_COL].astype(str) == str(tomo_name)]
    if matches.empty:
        raise ExcludeTiltsError(f"tomogram '{tomo_name}' not found in the input tilt series STAR")
    return matches.iloc[0]


def list_tilt_series(project_dir: Path, in_tiltseries: str) -> list[str]:
    """Every tilt series this job's `in_tiltseries` names, in file order."""
    df = _read_global_table(project_dir, in_tiltseries)
    return [str(v) for v in df[GLOBAL_TOMO_NAME_COL].tolist()]


def _load_original_series(project_dir: Path, in_tiltseries: str, tomo_name: str):
    """This tomogram's own per-image STAR, read fresh from the job's input
    (NEVER from this job's own already-written copy -- that copy only ever
    holds the KEPT rows, so re-deriving from it would make an excluded image
    permanently unrecoverable). Returns (full DataFrame, global-table row)."""
    gdf = _read_global_table(project_dir, in_tiltseries)
    row = _global_row(gdf, tomo_name)
    star_rel = str(row[GLOBAL_STARFILE_COL])
    p = viz._safe(project_dir, star_rel)
    if not p.is_file():
        raise ExcludeTiltsError(f"per-tilt-series STAR not found: {star_rel}")
    blocks = _read_star_blocks(p)
    df = blocks.get(tomo_name)
    if df is None:
        df = next(iter(blocks.values()), None)
    if df is None or MOVIE_COL not in df.columns:
        raise ExcludeTiltsError(f"{p.name}: no {MOVIE_COL} column found")
    return df, row


def _job_series_path(job_dir: Path, tomo_name: str) -> Path:
    return Path(job_dir) / TILT_SERIES_DIRNAME / f"{_sanitize(tomo_name)}.star"


def _read_job_series(job_dir: Path, tomo_name: str):
    """This job's own already-saved copy of one tilt series (the KEPT rows
    only), or None if nothing has been saved for it yet."""
    p = _job_series_path(Path(job_dir), tomo_name)
    if not p.is_file():
        return None
    blocks = _read_star_blocks(p)
    df = blocks.get(tomo_name)
    if df is None:
        df = next(iter(blocks.values()), None)
    return df


def _write_series_output(project_dir: Path, job_dir: Path, tomo_name: str, global_row, df) -> Path:
    """Write one tilt series' own STAR (the block-name/filename convention a
    real relion_tomo_exclude_tilt_images run produces: data_<TomoName>, at
    <job_dir>/tilt_series/<TomoName>.star) -- ALWAYS, even with zero rows
    (an empty per-image STAR is what tells _read_job_series/list_images this
    series has been explicitly fully excluded, as opposed to never touched
    at all) -- and update this tomogram's row in the job-level
    selected_tilt_series.star (data_global). A fully-excluded series is
    DROPPED from that global table rather than kept with zero rows, matching
    real RELION's own RlnTiltSeriesSet.write_star_file convention (a
    downstream job like Reconstruct Tomograms has nothing to do with a tilt
    series that has no images left)."""
    import starfile

    job_dir = Path(job_dir)
    series_path = _job_series_path(job_dir, tomo_name)
    series_path.parent.mkdir(parents=True, exist_ok=True)
    starfile.write({tomo_name: df.reset_index(drop=True)}, series_path, overwrite=True)
    if len(df) > 0:
        _upsert_job_global_star(project_dir, job_dir, tomo_name, global_row, series_path)
    else:
        _remove_from_job_global_star(job_dir, tomo_name)
    return series_path


def _load_job_global_rows(job_dir: Path) -> dict:
    """This job's own already-written selected_tilt_series.star, as
    {tomo_name: row_dict}. A read failure (missing/corrupt file, unexpected
    shape) is NOT swallowed here -- letting it propagate is safer than
    silently returning {}, which would make the caller's next write discard
    every other tilt series' already-saved row (confirmed for real:
    write_passthrough's loop wrote only the LAST series processed, before
    this was ever guarded, because a broad `except Exception` reset the
    whole accumulator on any hiccup)."""
    job_dir = Path(job_dir)
    job_star = job_dir / JOB_GLOBAL_STAR_NAME
    rows: dict[str, dict] = {}
    if job_star.is_file():
        blocks = _read_star_blocks(job_star)
        existing = blocks.get(JOB_GLOBAL_BLOCK_NAME)
        if existing is None:
            existing = next(iter(blocks.values()), None)
        if existing is not None and GLOBAL_TOMO_NAME_COL in existing.columns:
            for _, r in existing.iterrows():
                rows[str(r[GLOBAL_TOMO_NAME_COL])] = r.to_dict()
    return rows


def _write_job_global_rows(job_dir: Path, rows: dict) -> None:
    import starfile

    job_star = Path(job_dir) / JOB_GLOBAL_STAR_NAME
    if not rows:
        # No tilt series has any kept image left -- nothing for a
        # downstream job to read. Drop the file rather than writing a
        # column-less STAR (there is no schema left to derive columns
        # from once every row is gone).
        if job_star.is_file():
            job_star.unlink()
        return
    df = pd.DataFrame(list(rows.values()))
    starfile.write({JOB_GLOBAL_BLOCK_NAME: df}, job_star, overwrite=True)


def _upsert_job_global_star(project_dir: Path, job_dir: Path, tomo_name: str, global_row, series_path: Path) -> None:
    rows = _load_job_global_rows(job_dir)
    new_row = global_row.to_dict()
    new_row[GLOBAL_TOMO_NAME_COL] = str(tomo_name)
    new_row[GLOBAL_STARFILE_COL] = str(series_path.relative_to(project_dir))
    rows[str(tomo_name)] = new_row
    _write_job_global_rows(job_dir, rows)


def _remove_from_job_global_star(job_dir: Path, tomo_name: str) -> None:
    rows = _load_job_global_rows(job_dir)
    rows.pop(str(tomo_name), None)
    _write_job_global_rows(job_dir, rows)


def _write_passthrough_locked(project_dir: Path, job_dir: Path, in_tiltseries: str) -> int:
    names = list_tilt_series(project_dir, in_tiltseries)
    for name in names:
        df, row = _load_original_series(project_dir, in_tiltseries, name)
        _write_series_output(project_dir, job_dir, name, row, df)
    return len(names)


def write_passthrough(project_dir: Path, job_dir: Path, in_tiltseries: str) -> int:
    """Write every tilt series this job's input names, with NOTHING
    excluded -- the job's immediately-usable default (see module docstring).
    Called once at job start (custom_jobs.run_exclude_tilt_images), and
    again (via clear_exclusions first) on Overwrite. Returns the number of
    tilt series written."""
    with _job_lock(job_dir):
        return _write_passthrough_locked(project_dir, job_dir, in_tiltseries)


def list_images(project_dir: Path, job_dir: Path, in_tiltseries: str, tomo_name: str) -> list[dict]:
    """Every image in this tilt series, with its current excluded state --
    see module docstring for how "excluded" is derived (no stored flag,
    just "in the original, not in this job's own kept copy")."""
    df, _ = _load_original_series(project_dir, in_tiltseries, tomo_name)
    df = df.reset_index(drop=True)
    kept = _read_job_series(job_dir, tomo_name)
    kept_names: Optional[set] = None
    if kept is not None and MOVIE_COL in kept.columns:
        kept_names = set(kept[MOVIE_COL].astype(str))

    def _f(row, col):
        if col not in df.columns:
            return None
        v = row[col]
        return None if pd.isna(v) else float(v)

    images = []
    for i, row in df.iterrows():
        movie = str(row[MOVIE_COL])
        images.append({
            "index": int(i),
            "movie_name": movie,
            "mic_name": str(row[MIC_NAME_COL]) if MIC_NAME_COL in df.columns else "",
            "tilt_angle": _f(row, TILT_ANGLE_COL),
            "pre_exposure": _f(row, PRE_EXPOSURE_COL),
            "ctf_max_resolution": _f(row, CTF_MAXRES_COL),
            "accum_motion_total": _f(row, MOTION_COL),
            "excluded": (movie not in kept_names) if kept_names is not None else False,
        })
    return images


def series_summary(project_dir: Path, job_dir: Path, in_tiltseries: str) -> list[dict]:
    """One row per tilt series -- name, total image count, and how many are
    currently excluded -- for the reviewer's tilt-series list."""
    out = []
    for name in list_tilt_series(project_dir, in_tiltseries):
        df, _ = _load_original_series(project_dir, in_tiltseries, name)
        n_total = len(df)
        kept = _read_job_series(job_dir, name)
        n_kept = len(kept) if kept is not None else n_total
        out.append({"name": name, "n_images": n_total, "n_excluded": n_total - n_kept})
    return out


def save_tilt_series_exclusions(
    project_dir: Path, job_dir: Path, in_tiltseries: str, tomo_name: str,
    excluded_movie_names: list[str],
) -> dict:
    """Drop the named images (by rlnMicrographMovieName) from this tilt
    series and rewrite its own STAR + the job-level selected_tilt_series.star
    row for it. Always re-derives from the ORIGINAL input (see
    _load_original_series), so re-including a previously-excluded image is
    just calling this again without it in `excluded_movie_names` -- there is
    no accumulation across saves.

    Rows are sorted by rlnTomoNominalStageTiltAngle before writing, matching
    RelionTiltImageExcluderWidget.save_output's own convention."""
    df, global_row = _load_original_series(project_dir, in_tiltseries, tomo_name)
    excluded = {str(v) for v in (excluded_movie_names or [])}
    mask = ~df[MOVIE_COL].astype(str).isin(excluded)
    filtered = df[mask]
    if TILT_ANGLE_COL in filtered.columns:
        filtered = filtered.sort_values(TILT_ANGLE_COL, kind="stable")
    with _job_lock(job_dir):
        series_path = _write_series_output(project_dir, job_dir, tomo_name, global_row, filtered)
    return {
        "series_path": str(series_path),
        "n_total": len(df),
        "n_kept": len(filtered),
        "n_excluded": len(df) - len(filtered),
    }


def _clear_exclusions_locked(job_dir: Path) -> int:
    job_dir = Path(job_dir)
    removed = 0
    global_star = job_dir / JOB_GLOBAL_STAR_NAME
    if global_star.is_file():
        global_star.unlink()
        removed += 1
    series_dir = job_dir / TILT_SERIES_DIRNAME
    if series_dir.is_dir():
        for f in series_dir.glob("*.star"):
            f.unlink()
            removed += 1
        try:
            series_dir.rmdir()
        except OSError:
            pass
    return removed


def clear_exclusions(job_dir: Path) -> int:
    """Delete everything this job has written -- every per-series STAR plus
    the job-level selected_tilt_series.star. Called at the start of an
    Overwrite (see custom_jobs.run_exclude_tilt_images), mirroring manual_
    pick.clear_spa_picks/clear_tomo_picks: real RELION's own "Overwrite"
    re-runs into the SAME directory, so a fresh session needs a clean slate.
    A fresh (never-run) job's directory has nothing to clear, so this is a
    safe no-op there too. Returns how many files were removed."""
    with _job_lock(job_dir):
        return _clear_exclusions_locked(job_dir)


def reset_and_write_passthrough(project_dir: Path, job_dir: Path, in_tiltseries: str) -> tuple[int, int]:
    """clear_exclusions() + write_passthrough() as ONE atomic step under a
    single lock acquisition -- what job start (custom_jobs.
    run_exclude_tilt_images) actually needs. Calling the two public
    functions back-to-back would release the lock in between them, leaving
    a window where a concurrent save request could write into (or read out
    of) a job directory that has been cleared but not yet reseeded. Returns
    (n_removed, n_series_written)."""
    with _job_lock(job_dir):
        removed = _clear_exclusions_locked(job_dir)
        n_series = _write_passthrough_locked(project_dir, job_dir, in_tiltseries)
    return removed, n_series
