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

import progress

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
