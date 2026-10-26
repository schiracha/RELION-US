"""
Make Mask progress preview: a PNG of the FIRST mask a Make Mask job writes,
laid over the tomogram it was made from, for the job's Progress tab.

isonet.py make_mask takes tens of seconds per tomogram and writes one
`<tomogram>_mask.mrc` as each finishes; a patch size / density / std / z-crop
setting that is wrong is wrong for every tomogram. So the first finished mask
is rendered as soon as it is complete, and the user can look at it and abort
while the rest are still being made instead of finding out at the end.

The picture is two orthogonal sections through the middle of the volume, the
tomogram in grey with the mask tinted and outlined:

    XY at mid-Z   -- which parts of the field of view are kept
    XZ at mid-Y   -- the z-crop and the specimen's thickness

The PNG (and a small JSON of what it shows) is written into the job's own
directory, once. It describes the first mask, so it is never re-rendered while
the job runs; an Overwrite clears the directory and so starts afresh.

Reads only slices through numpy memory maps -- the tomograms are gigabytes.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import numpy as np

PREVIEW_PNG = "first_mask_preview.png"
PREVIEW_JSON = "first_mask_preview.json"
MASK_SUFFIX = "_mask.mrc"
SETTLE_SECONDS = 1.5            # a file touched this recently may still be being written
PANEL_WIDTH = 720
TINT = (60, 220, 120)
EDGE = (255, 230, 60)

# Columns IsoNet reads the tomogram from, in the order make_mask falls back
# through them (isonet.py make_mask's mask_row).
TOMOGRAM_COLUMNS = ("rlnDeconvTomoName", "rlnTomoName", "rlnTomoReconstructedTomogramHalf1")

SETTING_KEYS = (
    ("patch_size", "patch size", ""),
    ("density_percentage", "density", "%"),
    ("std_percentage", "std", "%"),
    ("z_crop", "z-crop", ""),
)


def _mrc_is_complete(path: Path) -> bool:
    """The file has all the bytes its own header promises and has stopped
    changing -- make_mask writes each mask in one go, but a poll can land
    between the file appearing and its data being all there."""
    import mrcfile
    try:
        st = path.stat()
        if time.time() - st.st_mtime < SETTLE_SECONDS:
            return False
        with mrcfile.open(path, mode="r", permissive=True, header_only=True) as m:
            h = m.header
            itemsize = mrcfile.utils.dtype_from_mode(int(h.mode)).itemsize
            expected = 1024 + int(h.nsymbt) + int(h.nx) * int(h.ny) * int(h.nz) * itemsize
        return st.st_size >= expected
    except Exception:  # noqa: BLE001 -- unreadable now means "not ready yet"
        return False


def finished_masks(job_dir: Path) -> list[Path]:
    """Complete masks in the job directory, oldest first (the order the job
    made them in)."""
    found = []
    try:
        entries = [e for e in Path(job_dir).iterdir() if e.name.endswith(MASK_SUFFIX) and e.is_file()]
    except OSError:
        return []
    for e in sorted(entries, key=lambda p: (p.stat().st_mtime, p.name)):
        if _mrc_is_complete(e):
            found.append(e)
    return found


def _tomogram_for(job_dir: Path, project_dir: Optional[Path], base: str) -> Optional[Path]:
    """The tomogram this mask was made from: the row of the job's own STAR copy
    whose tomogram file has the mask's name stem."""
    import starfile
    star = Path(job_dir) / "tomograms.star"
    if not star.is_file():
        return None
    try:
        blocks = starfile.read(star, always_dict=True)
        table = next(d for d in blocks.values() if hasattr(d, "columns") and "rlnPixelSize" in d.columns)
    except Exception:  # noqa: BLE001
        return None
    root = Path(project_dir) if project_dir else Path.cwd()
    for column in TOMOGRAM_COLUMNS:
        if column not in table.columns:
            continue
        for value in table[column].astype(str):
            if value in ("None", "", "nan"):
                continue
            if Path(value).stem == base:
                p = Path(value)
                p = p if p.is_absolute() else root / p
                return p if p.is_file() else None
    return None


def _grey(sl: np.ndarray) -> np.ndarray:
    """Slice -> uint8, stretched between its 0.5 / 99.5 percentiles."""
    sl = np.nan_to_num(np.asarray(sl, dtype=np.float32))
    lo, hi = (float(v) for v in np.percentile(sl, (0.5, 99.5)))
    if hi <= lo:
        hi = lo + 1.0
    return np.rint(np.clip((sl - lo) / (hi - lo), 0.0, 1.0) * 255.0).astype(np.uint8)


def _panel(tomo_slice: Optional[np.ndarray], mask_slice: np.ndarray, label: str, scale: float):
    """One RGB panel: tomogram grey (black when unavailable), mask tinted and
    outlined, with a caption."""
    from PIL import Image, ImageDraw, ImageFont
    from scipy import ndimage

    h, w = mask_slice.shape
    size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    mask = Image.fromarray((mask_slice > 0).astype(np.uint8) * 255, mode="L").resize(size, Image.NEAREST)
    keep = np.asarray(mask) > 0
    if tomo_slice is not None:
        base = Image.fromarray(_grey(tomo_slice), mode="L").resize(size, Image.BOX)
    else:
        base = Image.new("L", size, 0)
    rgb = np.stack([np.asarray(base)] * 3, axis=-1).astype(np.float32)
    tint = np.array(TINT, dtype=np.float32)
    rgb[keep] = rgb[keep] * 0.65 + tint * 0.35
    edge = keep & ~ndimage.binary_erosion(keep, iterations=1)
    rgb[edge] = np.array(EDGE, dtype=np.float32)
    img = Image.fromarray(np.rint(rgb).astype(np.uint8), mode="RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:               # older Pillow: fixed-size bitmap font
        font = ImageFont.load_default()
    draw.rectangle([0, 0, 8 + 7 * len(label), 20], fill=(0, 0, 0))
    draw.text((4, 3), label, fill=(255, 255, 255), font=font)
    return img


def render_first_mask(mask_path: Path, tomogram_path: Optional[Path]) -> tuple[bytes, dict]:
    """PNG bytes for one mask over its tomogram, and facts about the mask."""
    import io
    import mrcfile
    from PIL import Image

    with mrcfile.mmap(mask_path, mode="r", permissive=True) as mm:
        mask = mm.data
        if mask is None or mask.ndim != 3:
            raise ValueError(f"{mask_path.name}: not a 3D volume")
        nz, ny, nx = (int(n) for n in mask.shape)
        voxel = float(mm.voxel_size.x)
        z, y = nz // 2, ny // 2
        mask_xy = np.array(mask[z])
        mask_xz = np.array(mask[:, y, :])
        sample = np.asarray(mask[::8, ::4, ::4])
        percent = 100.0 * float((sample > 0).mean())

    tomo_xy = tomo_xz = None
    note = None
    if tomogram_path is not None:
        with mrcfile.mmap(tomogram_path, mode="r", permissive=True) as tm:
            td = tm.data
            if td is not None and td.shape == mask.shape:
                tomo_xy = np.array(td[z])
                tomo_xz = np.array(td[:, y, :])
            else:
                note = "tomogram and mask differ in size; mask shown alone"
    else:
        note = "tomogram not found; mask shown alone"

    scale = PANEL_WIDTH / nx
    top = _panel(tomo_xy, mask_xy, f"XY  z={z}", scale)
    bottom = _panel(tomo_xz, mask_xz, f"XZ  y={y}", scale)
    gap = 6
    canvas = Image.new("RGB", (top.width, top.height + gap + bottom.height), (20, 20, 20))
    canvas.paste(top, (0, 0))
    canvas.paste(bottom, (0, top.height + gap))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue(), {
        "shape": [nz, ny, nx], "voxel_size": voxel, "mask_percent": round(percent, 1),
        "tomogram": str(tomogram_path) if tomogram_path else None, "note": note,
    }


def _settings(field_values: dict, definition: dict) -> list[str]:
    """The four mask settings, as shown next to the picture."""
    defaults = {o["key"]: o.get("default") for o in definition.get("options", [])}
    out = []
    for key, label, unit in SETTING_KEYS:
        value = (field_values or {}).get(key)
        if value in (None, ""):
            value = defaults.get(key)
        if value not in (None, ""):
            out.append(f"{label} {value}{unit}")
    return out


def _total_masks(job_dir: Path, field_values: dict) -> Optional[int]:
    """How many masks this run will make: the rows its tomo_idx selects."""
    import starfile
    from isonet_jobs import _row_selection
    star = Path(job_dir) / "tomograms.star"
    try:
        blocks = starfile.read(star, always_dict=True)
        table = next(d for d in blocks.values() if hasattr(d, "columns") and "rlnPixelSize" in d.columns)
    except Exception:  # noqa: BLE001
        return None
    return len(_row_selection((field_values or {}).get("tomo_idx"), len(table)))


def mask_preview(job_dir: Path, project_dir: Optional[Path], field_values: dict, definition: dict) -> dict:
    """What the Progress tab shows for a Make Mask run. Renders the picture the
    first time a finished mask exists, then only reads it back."""
    job_dir = Path(job_dir)
    png, meta_path = job_dir / PREVIEW_PNG, job_dir / PREVIEW_JSON
    done = finished_masks(job_dir)
    out = {"supported": True, "available": False, "masks_done": len(done),
           "masks_total": _total_masks(job_dir, field_values), "settings": _settings(field_values, definition)}

    if not png.is_file() or not meta_path.is_file():
        if not done:
            return out
        first = done[0]
        base = first.name[: -len(MASK_SUFFIX)]
        try:
            data, facts = render_first_mask(first, _tomogram_for(job_dir, project_dir, base))
        except Exception as exc:  # noqa: BLE001 -- shown in the tab; the job itself is unaffected
            out["error"] = f"Could not draw {first.name}: {exc}"
            return out
        facts["mask"] = first.name
        tmp = png.with_suffix(".png.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, png)
        meta_path.write_text(json.dumps(facts))
    try:
        facts = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return out
    out.update(facts)
    out.update({"available": True, "file": PREVIEW_PNG, "mtime": png.stat().st_mtime})
    return out
