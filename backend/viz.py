"""
viz.py — a lightweight tomogram + particle-pick visualizer, callable from the
GUI as a plain tool (NOT a RELION job: it never appears in the Command Center
or writes anything).

Why this design (and not napari/PyQt in the browser): napari and DeepETPicker's
own picker GUI are desktop Qt/pyqtgraph apps — they can't run in a browser tab
at any usable speed. But the *interaction model* DeepETPicker uses for looking
at picks is simple and very effective, and it's what this reproduces:

  * browse the tomogram one 2D slice at a time (DeepETPicker shows an orthoslice
    tri-view via pyqtgraph; we do a single pane with an XY/XZ/YZ axis toggle),
  * overlay each particle on every slice within +/-(diameter/2) of its centre,
    drawing the true spherical cross-section radius sqrt(r^2 - d^2) so a marker
    grows toward the particle's centre slice and shrinks away from it. This
    +/-radius rule and the sqrt sizing are exactly what DeepETPicker's
    utils.annotate_particle does (github.com/cbmi-group/DeepETPicker,
    main.py / utils/utils.py, read 2026-08-15).

Performance approach: the volume is NEVER loaded whole. `mrcfile.mmap` memory-
maps the file, so each slice request touches only that slice's bytes; the
server normalizes it to 8-bit and returns a PNG. Picks are sent once as JSON
and the +/-radius overlay is drawn client-side on a <canvas>, so scrubbing
through Z is one small image fetch per slice with no per-slice pick round-trip.

Contrast: DeepETPicker's base display is a global min/max stretch with an
optional 1-99% percentile clip. Raw cryo-ET min/max is usually washed out by
a few outlier voxels, so here the DEFAULT contrast is a robust percentile
(0.5-99.5%) estimated from a strided sample of slices at open time; the caller
can override lo/hi. Nothing is denoised — this is a viewer, not a processor.

Safety: every path is resolved against the active project directory and must
stay inside it (RELION projects reference their files project-root-relatively
anyway); the viewer only ever READS.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

import numpy as np

VOLUME_SUFFIXES = {".mrc", ".mrcs", ".rec", ".st", ".ali"}
STAR_SUFFIXES = {".star"}
# Columns we understand for picks (voxel coordinates in the tomogram grid).
COORD_COLS = ("rlnCoordinateX", "rlnCoordinateY", "rlnCoordinateZ")
CENTERED_ANGST_COLS = ("rlnCenteredCoordinateXAngst", "rlnCenteredCoordinateYAngst", "rlnCenteredCoordinateZAngst")
TOMO_NAME_COL = "rlnTomoName"


class VizError(Exception):
    """Raised for a bad request (unreadable path, escapes project, etc.);
    the API turns this into a 400."""


def _safe(project_dir: Path, raw: str) -> Path:
    """Resolve `raw` against the project directory and refuse anything that
    escapes it. Read-only viewer, but we still don't reach outside the
    project."""
    if not raw:
        raise VizError("no path given")
    p = Path(raw)
    if not p.is_absolute():
        p = project_dir / p
    try:
        resolved = p.resolve()
        project_resolved = project_dir.resolve()
    except OSError as exc:
        raise VizError(f"cannot resolve path: {exc}") from exc
    if resolved != project_resolved and project_resolved not in resolved.parents:
        raise VizError(
            f"path is outside the project directory: {raw} (viewer only reads files under the project)"
        )
    return resolved


def _stem(path: str) -> str:
    return Path(path).stem


# --------------------------------------------------------------------------
# STAR inspection: figure out what tomograms + picks a file points at
# --------------------------------------------------------------------------


def _read_star_blocks(path: Path) -> dict:
    import starfile

    raw = starfile.read(path, always_dict=True)
    return {name: df for name, df in raw.items()}


def _first_block_with(blocks: dict, columns) -> Optional["object"]:
    for df in blocks.values():
        if hasattr(df, "columns") and all(c in df.columns for c in columns):
            return df
    return None


def _first_block_with_any(blocks: dict, *column_sets) -> Optional["object"]:
    """First block matching ANY of the given column sets. (Avoids
    `df_a or df_b`, which raises on DataFrame truthiness.)"""
    for cols in column_sets:
        found = _first_block_with(blocks, cols)
        if found is not None:
            return found
    return None


def _resolve_star(project_dir: Path, star_path: Path) -> dict:
    """Best-effort classification of a RELION-5 tomo STAR file. Returns:
      {kind, tomograms: [{name, mrc_path}], particles_path, picks_df?}
    Handles: an optimisation set (points at tomograms.star + particles.star),
    a tomograms.star (rlnTomoName + rlnTomoReconstructedTomogram), a
    particles.star (rlnTomoName + coords), or a bare coords STAR."""
    blocks = _read_star_blocks(star_path)

    # 1) optimisation set: single-row block referencing the other star files.
    opt = _first_block_with_any(blocks, ("rlnTomoParticlesFile",), ("rlnTomoTomogramsFile",))
    tomograms: list[dict] = []
    particles_path: Optional[Path] = None
    if opt is not None:
        row = opt.iloc[0]
        if "rlnTomoTomogramsFile" in opt.columns and str(row["rlnTomoTomogramsFile"]):
            try:
                tpath = _safe(project_dir, str(row["rlnTomoTomogramsFile"]))
                tomograms = _tomograms_from_star(project_dir, tpath)
            except VizError:
                pass
        if "rlnTomoParticlesFile" in opt.columns and str(row["rlnTomoParticlesFile"]):
            try:
                particles_path = _safe(project_dir, str(row["rlnTomoParticlesFile"]))
            except VizError:
                particles_path = None
        return {"kind": "optimisation_set", "tomograms": tomograms, "particles_path": particles_path}

    # 2) tomograms.star (has reconstructed tomogram paths)
    tomo_df = _first_block_with(blocks, (TOMO_NAME_COL, "rlnTomoReconstructedTomogram"))
    if tomo_df is not None:
        return {
            "kind": "tomograms",
            "tomograms": _tomograms_from_df(tomo_df),
            "particles_path": None,
        }

    # 3) particles.star / coords star
    picks_df = _first_block_with_any(blocks, COORD_COLS, CENTERED_ANGST_COLS)
    if picks_df is not None:
        return {"kind": "particles", "tomograms": [], "particles_path": star_path, "picks_df": picks_df}

    raise VizError(
        f"{star_path.name}: couldn't find tomogram or coordinate columns "
        f"(looked for an optimisation set, tomograms.star, or particles.star)."
    )


def _tomograms_from_star(project_dir: Path, tomo_star: Path) -> list[dict]:
    blocks = _read_star_blocks(tomo_star)
    df = _first_block_with(blocks, (TOMO_NAME_COL, "rlnTomoReconstructedTomogram"))
    return _tomograms_from_df(df) if df is not None else []


def _tomograms_from_df(df) -> list[dict]:
    out = []
    for _, row in df.iterrows():
        out.append({
            "name": str(row[TOMO_NAME_COL]),
            "mrc_path": str(row["rlnTomoReconstructedTomogram"]),
        })
    return out


def inspect(project_dir: Path, path: str, particles_path: Optional[str] = None) -> dict:
    """What can we visualize from these inputs? Returns a structure the
    frontend uses to populate the tomogram selector + decide on a filename
    mismatch warning. Never raises for a mismatch (that's a warning, not an
    error) — only for unreadable/invalid inputs."""
    src = _safe(project_dir, path)
    if not src.is_file():
        raise VizError(f"file not found: {path}")

    suffix = src.suffix.lower()
    result: dict = {"tomograms": [], "particles_path": None, "warnings": []}

    if suffix in VOLUME_SUFFIXES:
        result["kind"] = "volume"
        result["tomograms"] = [{"name": src.stem, "mrc_path": str(src)}]
        if particles_path:
            ppath = _safe(project_dir, particles_path)
            if not ppath.is_file():
                raise VizError(f"particles file not found: {particles_path}")
            result["particles_path"] = str(ppath)
    elif suffix in STAR_SUFFIXES:
        info = _resolve_star(project_dir, src)
        result["kind"] = info["kind"]
        result["tomograms"] = info.get("tomograms", [])
        pp = info.get("particles_path")
        result["particles_path"] = str(pp) if pp else None
        if particles_path:  # explicit override
            ppath = _safe(project_dir, particles_path)
            if not ppath.is_file():
                raise VizError(f"particles file not found: {particles_path}")
            result["particles_path"] = str(ppath)
        if info["kind"] == "particles" and not result["tomograms"]:
            result["warnings"].append(
                "This is a particles/coordinates STAR with no tomogram volume in it. "
                "Also provide the MRC tomogram to view the picks on it."
            )
            result["needs_mrc"] = True
    else:
        raise VizError(f"unsupported file type: {suffix} (expected an MRC volume or a STAR file)")

    return result


# --------------------------------------------------------------------------
# Volume + slice rendering
# --------------------------------------------------------------------------


def volume_info(project_dir: Path, mrc_path: str) -> dict:
    """Header dims, voxel size, and a robust default contrast (0.5-99.5%
    percentile from a strided slice sample) — without loading the whole
    volume."""
    import mrcfile

    p = _safe(project_dir, mrc_path)
    if not p.is_file():
        raise VizError(f"tomogram not found: {mrc_path}")
    with mrcfile.mmap(p, mode="r", permissive=True) as mrc:
        data = mrc.data
        if data is None or data.ndim != 3:
            raise VizError(f"{p.name}: not a 3D MRC volume")
        nz, ny, nx = data.shape
        try:
            voxel = float(mrc.voxel_size.x)
        except Exception:  # noqa: BLE001
            voxel = 0.0
        # Sample up to ~24 evenly spaced Z slices for a global contrast guess.
        n_sample = min(nz, 24)
        idxs = np.linspace(0, nz - 1, n_sample).astype(int)
        sample = np.asarray(data[idxs, :, :], dtype=np.float32)
        lo = float(np.percentile(sample, 0.5))
        hi = float(np.percentile(sample, 99.5))
        vmin = float(sample.min())
        vmax = float(sample.max())
    if hi <= lo:
        hi = lo + 1.0
    return {
        "nx": int(nx), "ny": int(ny), "nz": int(nz),
        "voxel_size": voxel,
        "contrast_lo": lo, "contrast_hi": hi,
        "value_min": vmin, "value_max": vmax,
    }


def _extract_slice(data, axis: str, index: int):
    """Return a 2D array for the requested plane. Array is indexed [z, y, x]
    (mrcfile convention). Row/col of the returned image:
      axis 'z': [y, x]  (index is z)
      axis 'y': [z, x]  (index is y)   -> XZ view
      axis 'x': [z, y]  (index is x)   -> ZY view
    """
    nz, ny, nx = data.shape
    if axis == "z":
        index = max(0, min(index, nz - 1))
        return np.asarray(data[index, :, :], dtype=np.float32)
    if axis == "y":
        index = max(0, min(index, ny - 1))
        return np.asarray(data[:, index, :], dtype=np.float32)
    if axis == "x":
        index = max(0, min(index, nx - 1))
        return np.asarray(data[:, :, index], dtype=np.float32)
    raise VizError(f"bad axis: {axis!r} (expected x, y, or z)")


def render_slice_png(
    project_dir: Path,
    mrc_path: str,
    axis: str,
    index: int,
    lo: Optional[float] = None,
    hi: Optional[float] = None,
    max_dim: int = 1200,
) -> bytes:
    """Memory-map the volume, extract one slice, contrast-stretch to 8-bit,
    downsample if larger than max_dim, and return PNG bytes."""
    import mrcfile
    from PIL import Image

    p = _safe(project_dir, mrc_path)
    if not p.is_file():
        raise VizError(f"tomogram not found: {mrc_path}")
    with mrcfile.mmap(p, mode="r", permissive=True) as mrc:
        data = mrc.data
        if data is None or data.ndim != 3:
            raise VizError(f"{p.name}: not a 3D MRC volume")
        sl = _extract_slice(data, axis, int(index))

    if lo is None or hi is None:
        lo = float(np.percentile(sl, 0.5))
        hi = float(np.percentile(sl, 99.5))
    if hi <= lo:
        hi = lo + 1.0
    clipped = np.clip((sl - lo) / (hi - lo), 0.0, 1.0)
    img8 = (clipped * 255.0).astype(np.uint8)

    im = Image.fromarray(img8, mode="L")
    # Downsample large slices for transfer (overlay coords are scaled client-side).
    if max(im.size) > max_dim:
        scale = max_dim / max(im.size)
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# Picks
# --------------------------------------------------------------------------


def load_picks(
    project_dir: Path,
    particles_path: str,
    tomo_name: Optional[str] = None,
    volume: Optional[dict] = None,
) -> dict:
    """Load picks (voxel coords) for one tomogram from a particles/coords STAR.

    Returns {picks: [{x,y,z,class}], tomo_names: [...], matched: bool,
    message: str}. `matched`/`message` support the filename-mismatch warning:
    if `tomo_name` is given and the STAR has an rlnTomoName column, we check
    whether any rows correspond to it (exact, or by filename stem)."""
    p = _safe(project_dir, particles_path)
    if not p.is_file():
        raise VizError(f"particles file not found: {particles_path}")
    blocks = _read_star_blocks(p)
    df = _first_block_with(blocks, COORD_COLS)
    centered = False
    if df is None:
        df = _first_block_with(blocks, CENTERED_ANGST_COLS)
        centered = df is not None
    if df is None:
        raise VizError(
            f"{p.name}: no coordinate columns (need rlnCoordinateX/Y/Z or "
            f"rlnCenteredCoordinateX/Y/ZAngst)."
        )

    tomo_names = []
    if TOMO_NAME_COL in df.columns:
        tomo_names = sorted({str(v) for v in df[TOMO_NAME_COL].tolist()})

    matched, message = _match_check(tomo_name, tomo_names)

    # Filter to the requested tomogram when possible.
    sel = df
    if tomo_name and TOMO_NAME_COL in df.columns:
        stem = _stem(tomo_name)
        mask = df[TOMO_NAME_COL].astype(str).apply(
            lambda v: v == tomo_name or _stem(v) == stem or stem in v or _stem(v) in tomo_name
        )
        if mask.any():
            sel = df[mask]

    picks = []
    if centered:
        # Convert centred Angstrom coords -> voxel indices, if we have the
        # volume dims + voxel size. voxel = dim/2 + angst/voxel_size.
        if not volume or not volume.get("voxel_size"):
            raise VizError(
                "This STAR stores centred Angstrom coordinates; a tomogram with a "
                "valid pixel size is needed to place them. Load the MRC first."
            )
        vs = float(volume["voxel_size"]) or 1.0
        cx, cy, cz = volume["nx"] / 2.0, volume["ny"] / 2.0, volume["nz"] / 2.0
        for _, r in sel.iterrows():
            picks.append({
                "x": cx + float(r[CENTERED_ANGST_COLS[0]]) / vs,
                "y": cy + float(r[CENTERED_ANGST_COLS[1]]) / vs,
                "z": cz + float(r[CENTERED_ANGST_COLS[2]]) / vs,
                "class": int(r["rlnClassNumber"]) if "rlnClassNumber" in sel.columns else 0,
            })
    else:
        for _, r in sel.iterrows():
            picks.append({
                "x": float(r[COORD_COLS[0]]),
                "y": float(r[COORD_COLS[1]]),
                "z": float(r[COORD_COLS[2]]),
                "class": int(r["rlnClassNumber"]) if "rlnClassNumber" in sel.columns else 0,
            })

    return {"picks": picks, "tomo_names": tomo_names, "matched": matched, "message": message}


def _match_check(tomo_name: Optional[str], tomo_names: list[str]) -> tuple[bool, str]:
    """Does the chosen tomogram correspond to any rlnTomoName in the picks
    file? Returns (matched, human message). matched is True when we can't
    tell (no tomo_name column or no chosen name) — we only warn on a
    positive mismatch, never block on missing info."""
    if not tomo_name or not tomo_names:
        return True, ""
    stem = _stem(tomo_name)
    for tn in tomo_names:
        if tn == tomo_name or _stem(tn) == stem or stem in tn or _stem(tn) in tomo_name:
            return True, ""
    return False, (
        f"The tomogram '{tomo_name}' doesn't match any tomogram named in the "
        f"picks file (found: {', '.join(tomo_names[:6])}"
        f"{'…' if len(tomo_names) > 6 else ''}). The picks may belong to a "
        f"different tomogram."
    )
