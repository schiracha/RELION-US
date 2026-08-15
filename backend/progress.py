"""
progress.py — live progress for RELION's iterative jobs (2D/3D classification,
refinement, initial model), read from the files RELION itself writes each
iteration.

What this reads, and why that's the right source:

RELION's iterative refiners write, per iteration, into the job directory
(verified against src/ml_optimiser.cpp MlOptimiser::write(), ~line 1364, and
src/ml_model.cpp MlModel::write(), RELION cloned 2026-08-14):

    run_it###_model.star      (or run_it###_half1_model.star when refining
                               with split half-sets, i.e. Refine3D)
    run_it###_optimiser.star
    run_it###_data.star
    run_it###_classes.mrcs    2D: ALL classes in one stack
    run_it###_class###.mrc    3D: one volume per class

The `model.star` is small (a few KB) and carries exactly the numbers a user
wants to watch:

  model_general block
    rlnCurrentResolution        current resolution, in 1/ANGSTROM (not Å!)
    rlnNrClasses                number of references
    rlnReferenceDimensionality  2 or 3
    rlnPixelSize                Å/pixel
  model_classes block (one row per class)
    rlnReferenceImage           path to the class image (see above)
    rlnClassDistribution        fraction of particles in this class
    rlnEstimatedResolution      per-class resolution, in ANGSTROM
    rlnAccuracyRotations        degrees
    rlnAccuracyTranslationsAngst

Every one of those label names was read out of RELION's own
src/metadata_label.h rather than assumed.

Cost control (the user's explicit constraint — "we don't want to be taking up
too much memory or storage"):

  * NOTHING is written to disk. Charts come from parsing the small per-iteration
    model.star files; thumbnails are rendered on demand straight from the MRC
    files RELION already wrote, and are never cached server-side.
  * Thumbnails are downsampled to THUMBNAIL_MAX_PX and returned as PNG.
  * A 3D class shows one central slice, not a rendering.
  * Parsed iteration summaries are cached in-process keyed by (path, mtime,
    size), so re-polling an unchanged run costs a stat() rather than a reparse.
"""
from __future__ import annotations

import functools
import io
import re
from pathlib import Path
from typing import Optional

import numpy as np

# Job types with a meaningful per-iteration progress view. Deliberately not
# every job: an Import or a MaskCreate has nothing to plot, and the user asked
# for this only on "the more important jobs that people are going to like to
# see some progress" on.
PROGRESS_JOBS = {
    "Class2D",
    "Class3D",
    "Autorefine",     # RELION's Refine3D
    "Inimodel",       # 3D initial model
    "MultiBody",
    "TomoReconPart",  # tomo subtomogram reconstruction/refinement
}

THUMBNAIL_MAX_PX = 128

# run_it025_model.star / run_it025_half1_model.star
_MODEL_RE = re.compile(r"^run_it(\d+)(?:_half(\d))?_model\.star$")
# "000007@run_it025_classes.mrcs" (2D stack) or "run_it025_class003.mrc" (3D)
_STACK_REF_RE = re.compile(r"^(\d+)@(.+)$")


def supports_progress(internal_name: str) -> bool:
    return internal_name in PROGRESS_JOBS


class ProgressError(Exception):
    """Bad request (unreadable job dir, missing iteration, etc.) -> HTTP 400."""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _iteration_files(job_dir: Path) -> list[tuple[int, Path]]:
    """Every (iteration, model.star) in this job dir, ascending. For a
    half-set refinement only half1 is used — the two halves track each other
    and plotting both would double the work to say the same thing."""
    found: dict[int, Path] = {}
    try:
        entries = list(job_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        m = _MODEL_RE.match(entry.name)
        if not m:
            continue
        half = m.group(2)
        if half is not None and half != "1":
            continue
        found[int(m.group(1))] = entry
    return sorted(found.items())


@functools.lru_cache(maxsize=512)
def _parse_model_star_cached(path_str: str, mtime: float, size: int) -> dict:
    """Parse one model.star. Keyed by (path, mtime, size) so polling a run
    whose earlier iterations haven't changed costs a stat(), not a reparse.
    RELION never rewrites a completed iteration's file, so this is safe."""
    import starfile

    raw = starfile.read(path_str, always_dict=True)
    general = raw.get("model_general")
    classes = raw.get("model_classes")

    out: dict = {"classes": []}
    if general is not None and len(general):
        row = general.iloc[0]

        def num(col):
            try:
                return float(row[col]) if col in general.columns else None
            except (TypeError, ValueError):
                return None

        # rlnCurrentResolution is in 1/Angstrom; convert to Angstrom so both
        # resolution numbers on the chart share one unit.
        inv = num("rlnCurrentResolution")
        out["current_resolution_A"] = (1.0 / inv) if inv and inv > 0 else None
        out["nr_classes"] = int(num("rlnNrClasses") or 0)
        out["dimensionality"] = int(num("rlnReferenceDimensionality") or 0)
        out["pixel_size"] = num("rlnPixelSize")

    if classes is not None and len(classes):
        cols = classes.columns

        def col(name, default=None):
            return classes[name].tolist() if name in cols else [default] * len(classes)

        refs = col("rlnReferenceImage", "")
        dist = col("rlnClassDistribution", 0.0)
        res = col("rlnEstimatedResolution", None)
        acc_rot = col("rlnAccuracyRotations", None)
        acc_trans = col("rlnAccuracyTranslationsAngst", None)
        for i in range(len(classes)):
            out["classes"].append({
                "index": i + 1,
                "reference": str(refs[i]) if refs[i] is not None else "",
                "distribution": float(dist[i] or 0.0),
                "resolution_A": float(res[i]) if res[i] is not None else None,
                "accuracy_rot": float(acc_rot[i]) if acc_rot[i] is not None else None,
                "accuracy_trans": float(acc_trans[i]) if acc_trans[i] is not None else None,
            })
    return out


def _parse_model_star(path: Path) -> dict:
    try:
        st = path.stat()
    except OSError as exc:
        raise ProgressError(f"cannot read {path.name}: {exc}") from exc
    return _parse_model_star_cached(str(path), st.st_mtime, st.st_size)


def read_progress(job_dir: Path, max_iterations: int = 200) -> dict:
    """Summarize a job's iterations for the Progress tab.

    Returns {available, iterations: [...], latest: {...}, nr_classes,
    dimensionality}. `available` is False (rather than an error) when the job
    simply hasn't written its first iteration yet — that's the normal state for
    the first minute of a run, not a failure.
    """
    files = _iteration_files(job_dir)
    if not files:
        return {"available": False, "iterations": [], "latest": None}

    # Cap defensively: a very long run shouldn't make one poll unbounded. The
    # most recent iterations are the interesting ones.
    if len(files) > max_iterations:
        files = files[-max_iterations:]

    iterations = []
    latest_parsed = None
    for iteration, path in files:
        try:
            parsed = _parse_model_star(path)
        except ProgressError:
            continue
        classes = parsed.get("classes", [])
        # One point per iteration for the charts. Per-class resolution is
        # summarized as the best (smallest Å) achieved that iteration.
        resolutions = [c["resolution_A"] for c in classes if c["resolution_A"]]
        iterations.append({
            "iteration": iteration,
            "resolution_A": parsed.get("current_resolution_A"),
            "best_class_resolution_A": min(resolutions) if resolutions else None,
            "nr_classes": parsed.get("nr_classes") or len(classes),
        })
        latest_parsed = (iteration, parsed)

    if latest_parsed is None:
        return {"available": False, "iterations": [], "latest": None}

    latest_iter, latest = latest_parsed
    return {
        "available": True,
        "iterations": iterations,
        "nr_classes": latest.get("nr_classes") or len(latest.get("classes", [])),
        "dimensionality": latest.get("dimensionality"),
        "pixel_size": latest.get("pixel_size"),
        "latest": {
            "iteration": latest_iter,
            "resolution_A": latest.get("current_resolution_A"),
            "classes": latest.get("classes", []),
        },
    }


# --------------------------------------------------------------------------
# Thumbnails
# --------------------------------------------------------------------------


def _resolve_reference(job_dir: Path, reference: str) -> tuple[Path, Optional[int]]:
    """Turn a rlnReferenceImage value into (path, stack_index_0based).

    2D: "000007@run_it025_classes.mrcs" -> (that .mrcs, 6)
    3D: "run_it025_class003.mrc"        -> (that .mrc, None)
    RELION writes these relative to the project root, but the basename always
    lives in the job directory, so resolve against the job dir and fall back to
    the raw path.
    """
    if not reference:
        raise ProgressError("class has no reference image")
    index = None
    path_part = reference
    m = _STACK_REF_RE.match(reference)
    if m:
        index = int(m.group(1)) - 1  # RELION stack indices are 1-based
        path_part = m.group(2)

    candidate = job_dir / Path(path_part).name
    if not candidate.is_file():
        candidate = Path(path_part)
        if not candidate.is_absolute():
            candidate = job_dir / path_part
    if not candidate.is_file():
        raise ProgressError(f"class image not found: {path_part}")
    return candidate, index


def _to_png(array2d: np.ndarray, max_px: int = THUMBNAIL_MAX_PX) -> bytes:
    """Contrast-stretch a 2D array to 8-bit and return a downsampled PNG.

    Same robust percentile approach as the tomogram viewer (viz.py): raw
    cryo-EM min/max is dominated by a few outlier pixels, and NaNs are real.
    """
    from PIL import Image

    a = np.asarray(array2d, dtype=np.float32)
    finite = a[np.isfinite(a)]
    if finite.size:
        lo, hi = (float(v) for v in np.percentile(finite, (0.5, 99.5)))
    else:
        lo, hi = 0.0, 1.0
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo = float(finite.min()) if finite.size else 0.0
        hi = float(finite.max()) if finite.size else 1.0
        if hi <= lo:
            hi = lo + 1.0
    norm = np.clip((np.nan_to_num(a, nan=lo) - lo) / (hi - lo), 0.0, 1.0)
    img = Image.fromarray(np.rint(norm * 255.0).astype(np.uint8), mode="L")
    if max(img.size) > max_px:
        scale = max_px / max(img.size)
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.BILINEAR,
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_class_thumbnail(job_dir: Path, reference: str, max_px: int = THUMBNAIL_MAX_PX) -> bytes:
    """One class as a small PNG — a 2D class average, or the central slice of a
    3D class volume. Rendered on demand from the file RELION already wrote and
    never cached to disk, so this feature adds no storage at all."""
    import mrcfile

    path, index = _resolve_reference(job_dir, reference)
    with mrcfile.mmap(path, mode="r", permissive=True) as mrc:
        data = mrc.data
        if data is None:
            raise ProgressError(f"{path.name}: no image data")
        if data.ndim == 2:
            plane = np.array(data, dtype=np.float32)
        elif data.ndim == 3:
            if index is not None:
                # a 2D class stack: one image per class
                if not 0 <= index < data.shape[0]:
                    raise ProgressError(f"class index {index + 1} outside {path.name}")
                plane = np.array(data[index], dtype=np.float32)
            else:
                # a 3D class volume: central Z slice
                plane = np.array(data[data.shape[0] // 2], dtype=np.float32)
        else:
            raise ProgressError(f"{path.name}: unsupported dimensionality {data.ndim}")
    return _to_png(plane, max_px=max_px)
