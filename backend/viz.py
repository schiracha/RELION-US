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


def _names_match(a: str, b: str) -> bool:
    """Do these two tomogram names refer to the same tomogram?

    Compares on the filename stem, so `TS_01.mrc`, `TS_01`, and
    `Tomograms/job005/TS_01.mrc` all match each other. Deliberately does NOT
    do bare substring matching: `TS_1 in TS_10` is True as a substring, which
    would silently overlay tomogram TS_10's particles onto TS_1 — and
    `TS_1`/`TS_10`/`TS_11` naming is completely normal in tomography. A
    substring is only accepted when it ends at a separator boundary, so
    `TS_01` still matches `rec_TS_01` but never matches `TS_010`."""
    if a == b:
        return True
    sa, sb = _stem(a), _stem(b)
    if sa == sb:
        return True
    for short, long_ in ((sa, sb), (sb, sa)):
        if short and short in long_:
            idx = long_.index(short)
            before = long_[idx - 1] if idx > 0 else ""
            after_i = idx + len(short)
            after = long_[after_i] if after_i < len(long_) else ""
            # both edges must be a boundary (start/end of string, or a
            # non-alphanumeric separator) -- rejects TS_1 inside TS_10
            if (not before or not before.isalnum()) and (not after or not after.isalnum()):
                return True
    return False


# --------------------------------------------------------------------------
# STAR inspection: figure out what tomograms + picks a file points at
# --------------------------------------------------------------------------


def _read_star_blocks(path: Path) -> dict:
    import starfile

    # always_dict=True already returns a dict; no copy needed.
    return starfile.read(path, always_dict=True)


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
      {kind, tomograms: [{name, mrc_path}], particles_path}
    Handles: an optimisation set (points at tomograms.star + particles.star),
    a tomograms.star (rlnTomoName + rlnTomoReconstructedTomogram), a
    particles.star (rlnTomoName + coords), or a bare coords STAR."""
    blocks = _read_star_blocks(star_path)

    # 1) optimisation set: single-row block referencing the other star files.
    opt = _first_block_with_any(blocks, ("rlnTomoParticlesFile",), ("rlnTomoTomogramsFile",))
    tomograms: list[dict] = []
    particles_path: Optional[Path] = None
    warnings: list[str] = []
    if opt is not None:
        row = opt.iloc[0]
        if "rlnTomoTomogramsFile" in opt.columns and str(row["rlnTomoTomogramsFile"]):
            try:
                tpath = _safe(project_dir, str(row["rlnTomoTomogramsFile"]))
                tomograms = _tomograms_from_star(project_dir, tpath)
            except VizError as exc:
                # Report why rather than silently showing an empty tomogram
                # list -- inspect() surfaces these to the user.
                warnings.append(f"Could not read the tomograms file it points at: {exc}")
        if "rlnTomoParticlesFile" in opt.columns and str(row["rlnTomoParticlesFile"]):
            try:
                particles_path = _safe(project_dir, str(row["rlnTomoParticlesFile"]))
            except VizError as exc:
                particles_path = None
                warnings.append(f"Could not read the particles file it points at: {exc}")
        return {
            "kind": "optimisation_set", "tomograms": tomograms,
            "particles_path": particles_path, "warnings": warnings,
        }

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
        return {"kind": "particles", "tomograms": [], "particles_path": star_path}

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
    # `needs_mrc` is always present so the returned shape is uniform.
    result: dict = {
        "tomograms": [], "particles_path": None, "warnings": [], "needs_mrc": False,
    }

    if suffix in VOLUME_SUFFIXES:
        result["kind"] = "volume"
        result["tomograms"] = [{"name": src.stem, "mrc_path": str(src)}]
    elif suffix in STAR_SUFFIXES:
        info = _resolve_star(project_dir, src)
        result["kind"] = info["kind"]
        result["tomograms"] = info.get("tomograms", [])
        pp = info.get("particles_path")
        result["particles_path"] = str(pp) if pp else None
        result["warnings"].extend(info.get("warnings", []))
        if info["kind"] == "particles" and not result["tomograms"]:
            result["warnings"].append(
                "This is a particles/coordinates STAR with no tomogram volume in it. "
                "Also provide the MRC tomogram to view the picks on it."
            )
            result["needs_mrc"] = True
    else:
        raise VizError(f"unsupported file type: {suffix} (expected an MRC volume or a STAR file)")

    # An explicitly supplied particles file always wins over one discovered
    # from the STAR (single place, rather than once per branch).
    if particles_path:
        ppath = _safe(project_dir, particles_path)
        if not ppath.is_file():
            raise VizError(f"particles file not found: {particles_path}")
        result["particles_path"] = str(ppath)

    return result


# --------------------------------------------------------------------------
# Volume + slice rendering
# --------------------------------------------------------------------------


# In-plane stride for the contrast sample. Fancy-indexing a memmap
# materializes the result, so sampling 24 full-resolution slices of an
# unbinned 4096^2 tomogram would allocate ~1.6 GB -- against this module's
# whole "never load the volume" premise. A 0.5/99.5 percentile estimate does
# not need every voxel.
_CONTRAST_SAMPLE_SLICES = 24
_CONTRAST_SAMPLE_STRIDE = 4


def _as_3d(data, name: str):
    """Normalize an mrcfile array to (nz, ny, nx). A 2D image (a plain SPA
    micrograph, not a tomogram) becomes a 1-slice "volume" -- nz=1 -- so
    volume_info/_extract_slice/render_slice_png don't need a separate code
    path for it; the frontend recognizes nz==1 and shows a single flat pane
    instead of the tri-view (there is no Z axis to browse). Raises for
    anything that isn't a 2D image or a 3D volume (e.g. a 4D stack)."""
    if data.ndim == 2:
        return data[np.newaxis, :, :]
    if data.ndim == 3:
        return data
    raise VizError(f"{name}: not a 2D image or 3D MRC volume (got {data.ndim}D)")


def volume_info(project_dir: Path, mrc_path: str) -> dict:
    """Header dims, voxel size, and a robust default contrast (0.5-99.5%
    percentile from a strided slice sample) — without loading the whole
    volume.

    `sample_min`/`sample_max` are the range of that SAMPLE, not of the whole
    volume (reading every voxel to get a true min/max would defeat the
    memory-mapped design). They exist to give the contrast sliders a sensible
    span, and are named so no caller mistakes them for the volume's true
    dynamic range. For a 2D micrograph (see _as_3d) the "sample" is just the
    one slice -- still cheap, a micrograph is orders of magnitude smaller
    than a tomogram, so there's no need for the tomogram path's Z-striding."""
    import mrcfile

    p = _safe(project_dir, mrc_path)
    if not p.is_file():
        raise VizError(f"tomogram not found: {mrc_path}")
    with mrcfile.mmap(p, mode="r", permissive=True) as mrc:
        raw = mrc.data
        if raw is None:
            raise VizError(f"{p.name}: not a 2D image or 3D MRC volume")
        data = _as_3d(raw, p.name)
        nz, ny, nx = data.shape
        try:
            voxel = float(mrc.voxel_size.x)
        except Exception:  # noqa: BLE001
            voxel = 0.0
        # Evenly spaced Z slices, strided in-plane. np.array (not asarray) so
        # the sample is a real copy and nothing references the mmap after the
        # `with` block closes it.
        n_sample = min(nz, _CONTRAST_SAMPLE_SLICES)
        idxs = np.linspace(0, nz - 1, n_sample).astype(int)
        s = _CONTRAST_SAMPLE_STRIDE
        sample = np.array(data[idxs, ::s, ::s], dtype=np.float32)

    finite = sample[np.isfinite(sample)]
    if finite.size:
        # one partition pass for both percentiles instead of two
        lo, hi = (float(v) for v in np.percentile(finite, (0.5, 99.5)))
        vmin, vmax = float(finite.min()), float(finite.max())
    else:
        lo, hi, vmin, vmax = 0.0, 1.0, 0.0, 1.0
    if hi <= lo:
        hi = lo + 1.0
    if vmax <= vmin:
        vmax = vmin + 1.0
    return {
        "nx": int(nx), "ny": int(ny), "nz": int(nz),
        "voxel_size": voxel,
        "contrast_lo": lo, "contrast_hi": hi,
        "sample_min": vmin, "sample_max": vmax,
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
        return np.array(data[index, :, :], dtype=np.float32)
    if axis == "y":
        index = max(0, min(index, ny - 1))
        return np.array(data[:, index, :], dtype=np.float32)
    if axis == "x":
        index = max(0, min(index, nx - 1))
        return np.array(data[:, :, index], dtype=np.float32)
    raise VizError(f"bad axis: {axis!r} (expected x, y, or z)")


def render_slice_png(
    project_dir: Path,
    mrc_path: str,
    axis: str,
    index: int,
    lo: Optional[float] = None,
    hi: Optional[float] = None,
    max_dim: int = 1200,
    transpose: bool = False,
) -> bytes:
    """Memory-map the volume, extract one slice, contrast-stretch to 8-bit,
    downsample if larger than max_dim, and return PNG bytes.

    `transpose` swaps the returned image's rows and columns. The orthogonal
    viewer's left-hand panel needs the x-axis slice with Y running vertically
    (so it shares the main XY panel's vertical axis) rather than the natural
    [z, y] order, and transposing the small 2D slice here is cheaper and less
    error-prone than rotating the PNG plus its overlay in the browser.
    """
    import mrcfile
    from PIL import Image

    p = _safe(project_dir, mrc_path)
    if not p.is_file():
        raise VizError(f"tomogram not found: {mrc_path}")
    with mrcfile.mmap(p, mode="r", permissive=True) as mrc:
        raw = mrc.data
        if raw is None:
            raise VizError(f"{p.name}: not a 2D image or 3D MRC volume")
        data = _as_3d(raw, p.name)
        sl = _extract_slice(data, axis, int(index))
    if transpose:
        sl = np.ascontiguousarray(sl.T)

    # lo and hi are independent query params, each filled in separately so
    # supplying only one doesn't discard it and re-derive both.
    # nanpercentile because NaN voxels are real in cryo-ET (failed CTF
    # weighting, masked reconstructions); a plain percentile returns NaN,
    # `hi <= lo` is then False (NaN comparisons always are), and the whole
    # slice renders black with no error.
    if lo is None or hi is None:
        p_lo, p_hi = (float(v) for v in np.nanpercentile(sl, (0.5, 99.5)))
        if lo is None:
            lo = p_lo
        if hi is None:
            hi = p_hi
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        # all-NaN slice, or a degenerate/inverted range from the caller
        finite = sl[np.isfinite(sl)]
        lo = float(finite.min()) if finite.size else 0.0
        hi = float(finite.max()) if finite.size else 1.0
        if hi <= lo:
            hi = lo + 1.0
    clipped = np.clip((np.nan_to_num(sl, nan=lo) - lo) / (hi - lo), 0.0, 1.0)
    img8 = np.rint(clipped * 255.0).astype(np.uint8)

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

    # Filter to the requested tomogram. If the picks file names tomograms and
    # NONE of them is this one, return no picks rather than falling back to
    # the whole DataFrame -- that would silently draw every tomogram's
    # particles on top of one tomogram, indistinguishable from a real match.
    sel = df
    if tomo_name and TOMO_NAME_COL in df.columns:
        mask = df[TOMO_NAME_COL].astype(str).apply(lambda v: _names_match(v, tomo_name))
        sel = df[mask]

    if centered:
        # Convert centred Angstrom coords -> voxel indices, if we have the
        # volume dims + voxel size. voxel = dim/2 + angst/voxel_size.
        if not volume or not volume.get("voxel_size"):
            raise VizError(
                "This STAR stores centred Angstrom coordinates; a tomogram with a "
                "valid pixel size is needed to place them. Load the MRC first."
            )
        vs = float(volume["voxel_size"])
        xs = volume["nx"] / 2.0 + sel[CENTERED_ANGST_COLS[0]].to_numpy(dtype=float) / vs
        ys = volume["ny"] / 2.0 + sel[CENTERED_ANGST_COLS[1]].to_numpy(dtype=float) / vs
        zs = volume["nz"] / 2.0 + sel[CENTERED_ANGST_COLS[2]].to_numpy(dtype=float) / vs
    else:
        xs = sel[COORD_COLS[0]].to_numpy(dtype=float)
        ys = sel[COORD_COLS[1]].to_numpy(dtype=float)
        zs = sel[COORD_COLS[2]].to_numpy(dtype=float)

    # Vectorized: a tomogram can carry 10^5 particles, and .iterrows() boxes
    # every row as a Series.
    if "rlnClassNumber" in sel.columns:
        classes = sel["rlnClassNumber"].to_numpy(dtype=int)
    else:
        classes = np.zeros(len(sel), dtype=int)

    picks = [
        {"x": float(x), "y": float(y), "z": float(z), "class": int(c)}
        for x, y, z, c in zip(xs, ys, zs, classes)
    ]

    return {"picks": picks, "tomo_names": tomo_names, "matched": matched, "message": message}


def _match_check(tomo_name: Optional[str], tomo_names: list[str]) -> tuple[bool, str]:
    """Does the chosen tomogram correspond to any rlnTomoName in the picks
    file? Returns (matched, human message). matched is True when we can't
    tell (no tomo_name column or no chosen name) — we only warn on a
    positive mismatch, never block on missing info."""
    if not tomo_name or not tomo_names:
        return True, ""
    for tn in tomo_names:
        if _names_match(tn, tomo_name):
            return True, ""
    return False, (
        f"The tomogram '{tomo_name}' doesn't match any tomogram named in the "
        f"picks file (found: {', '.join(tomo_names[:6])}"
        f"{'…' if len(tomo_names) > 6 else ''}). The picks may belong to a "
        f"different tomogram."
    )
