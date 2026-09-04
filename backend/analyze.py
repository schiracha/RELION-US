"""
analyze.py — backend for the Analyze popup (Menu > Tools > Analyze), a
read-only window that reads across a project's whole run history rather than
progress.py's narrower scope (the currently-open job popup's own live
progress). Sibling to progress.py, not a growth of it, since the STAR shapes
here (run_it###_optimiser.star, and eventually model_class_N sub-blocks,
corrected_micrographs.star, particles.star) are ones progress.py never
touches. Reuses progress.py's own per-iteration model.star cache
(_parse_model_star_cached) rather than re-parsing.

Tab layout and technique are inspired by CNIO_Relion_Tools' relion_analyse.py
— see NOTICE.md for the full attribution; no source from that project is
copied here (different stack entirely: this stays on starfile + this app's
own hand-rolled frontend rendering).
"""
from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Optional

import progress
import viz


class AnalyzeError(Exception):
    """Bad request (unreadable/unsafe path, no numeric columns, etc.) ->
    HTTP 400, same convention as progress.ProgressError/viz.VizError."""

# run_it025_optimiser.star -- confirmed against MlOptimiser::write()
# (src/ml_optimiser.cpp ~1359-1378, RELION 5.0.1 checkout).
_OPTIMISER_RE = re.compile(r"^run_it(\d+)_optimiser\.star$")

# All three unconditionally written to data_optimiser_general every
# iteration (src/ml_optimiser.cpp ~1480-1482, confirmed against real source,
# not assumed from any third-party tool's naming) -- "how much changed since
# last iteration" is exactly what a convergence chart wants to plot.
CONVERGENCE_COLUMNS = [
    "rlnChangesOptimalOrientations",
    "rlnChangesOptimalOffsets",
    "rlnChangesOptimalClasses",
]


def _optimiser_files(job_dir: Path) -> list[tuple[int, Path]]:
    """Every (iteration, optimiser.star) in this job dir, ascending -- same
    shape as progress.py's own _iteration_files, one file per iteration."""
    found: dict[int, Path] = {}
    try:
        entries = list(job_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        m = _OPTIMISER_RE.match(entry.name)
        if m:
            found[int(m.group(1))] = entry
    return sorted(found.items())


@functools.lru_cache(maxsize=512)
def _parse_optimiser_star_cached(path_str: str, mtime: float, size: int) -> dict:
    """data_optimiser_general is a STAR "list" block (single row of `_key
    value` pairs, no loop_ -- confirmed: MlOptimiser::write() calls
    MD.setIsList(true) before writing it), the same shape model_general in
    model.star turned out to be (see progress.py's own
    _parse_model_star_cached comment for why that distinction matters:
    starfile returns a list block as a plain dict, never a DataFrame).
    Cached by (path, mtime, size) like every other per-iteration parse in
    this app, since RELION never rewrites a completed iteration's file."""
    import starfile

    raw = starfile.read(path_str, always_dict=True)
    general = raw.get("optimiser_general")
    if general is None:
        return {}
    row = general if isinstance(general, dict) else (
        general.iloc[0].to_dict() if len(general) else {}
    )

    out: dict = {}
    for col in CONVERGENCE_COLUMNS:
        val = row.get(col)
        try:
            out[col] = float(val) if val is not None else None
        except (TypeError, ValueError):
            out[col] = None
    return out


def read_optimiser_series(job_dir: Path) -> dict:
    """One point per iteration for the convergence chart. `columns` lists
    only the CONVERGENCE_COLUMNS actually found with a real value in at
    least one file, so the frontend's column picker never offers a dead
    choice for a job type that doesn't write one of these."""
    files = _optimiser_files(job_dir)
    if not files:
        return {"available": False, "columns": [], "series": []}

    series = []
    seen_columns: set = set()
    for iteration, path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        parsed = _parse_optimiser_star_cached(str(path), st.st_mtime, st.st_size)
        point = {"iteration": iteration, **parsed}
        for col, val in parsed.items():
            if val is not None:
                seen_columns.add(col)
        series.append(point)

    return {
        "available": bool(series),
        "columns": [c for c in CONVERGENCE_COLUMNS if c in seen_columns],
        "series": series,
    }


def read_class_distribution_series(job_dir: Path) -> dict:
    """rlnClassDistribution per class per iteration, for the stacked-area
    chart. Reuses progress.py's _iteration_files/_parse_model_star_cached
    directly -- no half-set flag needed: _iteration_files already prefers a
    plain run_it###_model.star when one exists and falls back to
    run_it###_half1_model.star only when it doesn't (Refine3D's
    half-set-only convention), the same file selection progress.py's own
    read_progress() relies on for every PROGRESS_JOBS type uniformly."""
    files = progress._iteration_files(job_dir)
    if not files:
        return {"available": False, "iterations": [], "classes": {}}

    iterations: list = []
    classes: dict = {}
    for iteration, path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        parsed = progress._parse_model_star_cached(str(path), st.st_mtime, st.st_size)
        class_list = parsed.get("classes", [])
        if not class_list:
            continue
        iterations.append(iteration)
        for c in class_list:
            classes.setdefault(c["index"], []).append(c["distribution"])

    return {"available": bool(iterations), "iterations": iterations, "classes": classes}


# model_class_1, model_class_2, ... (one-indexed -- confirmed against
# MlModel::write(), src/ml_model.cpp ~748-771: MDsigma.setName("model_class_"
# + integerToString(iclass+1))). A loop_ table, one row per Fourier shell.
_MODEL_CLASS_RE = re.compile(r"^model_class_(\d+)$")


@functools.lru_cache(maxsize=256)
def _parse_model_class_fsc_cached(path_str: str, mtime: float, size: int) -> dict:
    """Per-class resolution/FSC/SSNR arrays from one iteration's model.star.
    rlnAngstromResolution, rlnGoldStandardFsc, and rlnSsnrMap are all THREE
    unconditionally written together to every model_class_N block, every
    iteration (ml_model.cpp ~757-768, confirmed against real source) --
    NOT mutually exclusive alternatives gated on half-set vs. plain
    classification, despite what the column names alone might suggest; a
    plain classification without random halves still gets a (less
    meaningful, but present) rlnGoldStandardFsc column. Both are returned
    here and the frontend picks which curve to plot by default."""
    import starfile

    raw = starfile.read(path_str, always_dict=True)
    out: dict = {}
    for key, block in raw.items():
        m = _MODEL_CLASS_RE.match(key)
        if not m or block is None or not len(block):
            continue
        class_idx = int(m.group(1))
        cols = block.columns
        if "rlnAngstromResolution" not in cols:
            continue
        resolution = block["rlnAngstromResolution"].tolist()
        entry = {"resolution": [float(v) for v in resolution]}
        if "rlnGoldStandardFsc" in cols:
            entry["fsc"] = [float(v) for v in block["rlnGoldStandardFsc"].tolist()]
        if "rlnSsnrMap" in cols:
            entry["ssnr"] = [float(v) for v in block["rlnSsnrMap"].tolist()]
        if "fsc" in entry or "ssnr" in entry:
            out[class_idx] = entry
    return out


def read_class_fsc(job_dir: Path, iteration: Optional[int] = None) -> dict:
    """Per-class FSC/SSNR-vs-resolution for the last (or a given) iteration.
    Reuses progress.py's own _iteration_files for which file to read (same
    half-set handling as read_class_distribution_series above) -- but a
    dedicated parse (_parse_model_class_fsc_cached), not
    _parse_model_star_cached, since model_class_N is a different block
    shape (loop_, one per class) that module has no reason to know about."""
    files = progress._iteration_files(job_dir)
    if not files:
        return {"available": False, "iteration": None, "classes": {}}
    if iteration is not None:
        match = next(((it, p) for it, p in files if it == iteration), None)
        if match is None:
            return {"available": False, "iteration": None, "classes": {}}
        it, path = match
    else:
        it, path = files[-1]

    try:
        st = path.stat()
    except OSError:
        return {"available": False, "iteration": None, "classes": {}}
    classes = _parse_model_class_fsc_cached(str(path), st.st_mtime, st.st_size)
    return {"available": bool(classes), "iteration": it, "classes": classes}


# --------------------------------------------------------------------------
# Particle scatter plot (Analyze popup's Particles tab -- C4). Not tied to a
# specific run's own iteration files like everything above: the user points
# this at any particles STAR directly (Extract's particles.star, a Select
# job's output, an optimisation set, ...), the same "type a path or Browse"
# input the tomogram viewer already uses for its own STAR fields (see
# viz.inspect). Reuses viz._safe for the same project-directory containment
# check the viewer already enforces on every path it's given, rather than
# inventing a second one.
# --------------------------------------------------------------------------


def read_particle_scatter_columns(project_dir: Path, raw_path: str) -> dict:
    """A particles STAR's particles+optics blocks, listed for the scatter
    plot's axis pickers. Column list excludes anything containing "Name"
    (matches CNIO's own dropdown-population rule in relion_analyse.py --
    those are string/path fields like rlnMicrographName/rlnImageName, not
    meaningful scatter axes) and anything not numeric (a scatter plot needs
    real numbers on both axes; a handful of RELION's own columns, e.g.
    rlnOpticsGroupName, aren't caught by the "Name" filter alone but also
    aren't numeric)."""
    import pandas as pd
    import starfile

    try:
        path = viz._safe(project_dir, raw_path)
    except viz.VizError as exc:
        raise AnalyzeError(str(exc)) from exc
    try:
        raw = starfile.read(path, always_dict=True)
    except Exception as exc:
        raise AnalyzeError(f"cannot read {path.name}: {exc}") from exc

    particles = raw.get("particles")
    if particles is None or not len(particles):
        return {"available": False, "columns": [], "rows": []}

    numeric_cols = [
        c for c in particles.columns
        if "Name" not in c and pd.api.types.is_numeric_dtype(particles[c])
    ]
    if not numeric_cols:
        return {"available": False, "columns": [], "rows": []}

    rows = []
    for i, row in particles[numeric_cols].iterrows():
        entry = {c: (None if pd.isna(row[c]) else float(row[c])) for c in numeric_cols}
        entry["_row_index"] = int(i)
        rows.append(entry)
    return {"available": True, "columns": numeric_cols, "rows": rows}


def export_star_subset(
    project_dir: Path, source_raw_path: str, row_indices: list[int],
    complement: bool, out_filename: str, block: str = "particles",
) -> dict:
    """Writes selected (or, if complement=True, everything EXCEPT selected)
    rows of `block` (the "particles" or "micrographs" loop_ table) to a new
    STAR file, optics block (and anything else in the source file) carried
    over unchanged. This is the first STAR-*writing* code in this repo --
    everything else here, and in viz.py/progress.py, is read-only.

    Row indices are always positions in the SOURCE file's own `block` table,
    re-read fresh here -- for the Micrographs tab specifically, that's the
    picked STAR's own row order, not the wider merged-with-corrected_
    micrographs.star view read_micrograph_scatter_columns plots (see that
    function's own _row_index handling for why the two line up correctly
    without this function needing to redo any merge itself).

    The destination is deliberately never a caller-supplied path the way
    the source is: out_filename is constrained to a bare filename (no `/`,
    no `..`) and joined under the SOURCE file's own directory. A read that
    escapes the project is a privacy problem; a WRITE that escapes it is a
    much worse one, so this doesn't reuse viz._safe's "resolve then check
    it's still inside the project" pattern for the output side at all --
    there's no path to resolve, just a filename component, which is a
    stronger guarantee than re-deriving the same check would be."""
    import starfile

    try:
        source_path = viz._safe(project_dir, source_raw_path)
    except viz.VizError as exc:
        raise AnalyzeError(str(exc)) from exc

    if not out_filename or "/" in out_filename or "\\" in out_filename or out_filename in (".", ".."):
        raise AnalyzeError("invalid output filename")
    if not out_filename.endswith(".star"):
        out_filename += ".star"
    out_path = source_path.parent / out_filename
    if out_path.exists():
        raise AnalyzeError(f"{out_filename} already exists in that folder -- choose a different name")

    try:
        raw = starfile.read(source_path, always_dict=True)
    except Exception as exc:
        raise AnalyzeError(f"cannot read {source_path.name}: {exc}") from exc
    table = raw.get(block)
    if table is None or not len(table):
        raise AnalyzeError(f"no {block} block to export from")

    all_indices = set(range(len(table)))
    selected = set(row_indices) & all_indices
    keep = (all_indices - selected) if complement else selected
    if not keep:
        raise AnalyzeError("selection is empty -- nothing to export")

    subset = table.iloc[sorted(keep)].reset_index(drop=True)
    out_blocks = dict(raw)
    out_blocks[block] = subset
    try:
        starfile.write(out_blocks, out_path, overwrite=False)
    except Exception as exc:
        raise AnalyzeError(f"could not write {out_filename}: {exc}") from exc
    return {"path": str(out_path.relative_to(project_dir)), "rows": len(subset)}


# --------------------------------------------------------------------------
# Micrograph scatter plot (Analyze popup's Micrographs tab -- rest of C4).
# Same "type a path or Browse" input as the Particles tab, but the STAR
# picked here (typically CTFFind's own micrographs_ctf.star) is joined with
# each producing MotionCorr job's own corrected_micrographs.star, so
# CTF-derived and motion-correction-derived columns can be plotted against
# each other -- same technique CNIO's relion_analyse.py uses
# (pd.merge(ctf_df, motion_df)), ported here as a plain pandas merge since
# starfile already returns DataFrames for loop_ blocks.
# --------------------------------------------------------------------------

# "MotionCorr/job002" out of "MotionCorr/job002/Micrographs/mic_0001.mrc" --
# RELION's own <JobTypeDir>/job<NNN>/ convention (job_registry.py and
# progress.py both already rely on the same shape elsewhere in this repo).
_JOB_DIR_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*/job\d+)/")


def read_micrograph_scatter_columns(project_dir: Path, raw_path: str) -> dict:
    """A picked micrographs STAR's own `micrographs` block, left-joined with
    every producing MotionCorr job's corrected_micrographs.star (motion
    columns rlnAccumMotionTotal/Early/Late -- confirmed against
    MotionCorr::run(), src/motioncorr_runner.cpp ~980-1022) on
    rlnMicrographName. `_row_index` is stamped onto the picked file's own
    rows BEFORE the merge, so it survives regardless of how pandas orders
    the joined result -- export_star_subset re-reads the picked file's own
    `micrographs` block directly and never sees the merge at all, so the
    indices this returns must mean "row N of the picked file", not "row N
    of this merged view"."""
    import pandas as pd
    import starfile

    try:
        path = viz._safe(project_dir, raw_path)
    except viz.VizError as exc:
        raise AnalyzeError(str(exc)) from exc
    try:
        raw = starfile.read(path, always_dict=True)
    except Exception as exc:
        raise AnalyzeError(f"cannot read {path.name}: {exc}") from exc

    micrographs = raw.get("micrographs")
    if micrographs is None or not len(micrographs) or "rlnMicrographName" not in micrographs.columns:
        return {"available": False, "columns": [], "rows": []}

    merged = micrographs.copy()
    merged["_row_index"] = range(len(merged))

    job_dirs: set = set()
    for name in merged["rlnMicrographName"].astype(str):
        m = _JOB_DIR_RE.match(name)
        if m:
            job_dirs.add(m.group(1))
    for job_dir in sorted(job_dirs):
        motion_path = project_dir / job_dir / "corrected_micrographs.star"
        if not motion_path.is_file():
            continue
        try:
            motion_raw = starfile.read(motion_path, always_dict=True)
        except Exception:
            continue
        motion = motion_raw.get("micrographs")
        if motion is None or "rlnMicrographName" not in motion.columns:
            continue
        # The picked file's own columns always win -- only pull in columns
        # not already present, besides the join key itself.
        extra_cols = [c for c in motion.columns if c == "rlnMicrographName" or c not in merged.columns]
        if len(extra_cols) <= 1:
            continue  # nothing new to add from this job
        merged = merged.merge(motion[extra_cols], on="rlnMicrographName", how="left")

    numeric_cols = [
        c for c in merged.columns
        if c != "_row_index" and "Name" not in c and pd.api.types.is_numeric_dtype(merged[c])
    ]
    if not numeric_cols:
        return {"available": False, "columns": [], "rows": []}

    rows = []
    for _, row in merged.iterrows():
        entry = {c: (None if pd.isna(row[c]) else float(row[c])) for c in numeric_cols}
        entry["_row_index"] = int(row["_row_index"])
        rows.append(entry)
    return {"available": True, "columns": numeric_cols, "rows": rows}
