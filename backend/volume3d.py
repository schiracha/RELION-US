"""
A downsampled copy of an MRC volume for the 3D (Mol*) viewer.

The browser has to download whatever it renders, and a tomogram is gigabytes,
so the 3D viewer is given a block-averaged copy no larger than `max_dim`
voxels along its longest side (voxel size scaled to match, so distances and
scale bars stay true). A refined map of 100-300 voxels is served as it is.
The copy is cached on disk (keyed by file, modification time and factor), so
reopening a volume, or changing nothing but the colour, costs nothing.

Reads through a memory map one slab of planes at a time: memory stays at a
few planes whatever the volume's size.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np

import viz

DEFAULT_MAX_DIM = 192
MIN_MAX_DIM = 32
MAX_MAX_DIM = 512
MAX_CACHED = 12                     # copies kept; the oldest go first


def cache_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "relion_us_volume3d"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _factor(shape, max_dim: int) -> int:
    return max(1, math.ceil(max(shape) / max_dim))


def _block_mean(data, factor: int):
    """data (nz, ny, nx) -> float32 array of (nz//f, ny//f, nx//f), each voxel
    the mean of an f x f x f block. Any remainder at the far edges is dropped.
    Non-finite voxels (real in cryo-ET) count as 0."""
    nz, ny, nx = data.shape
    oz, oy, ox = max(1, nz // factor), max(1, ny // factor), max(1, nx // factor)
    if factor == 1:
        out = np.empty((nz, ny, nx), dtype=np.float32)
        for z in range(nz):
            out[z] = np.nan_to_num(np.asarray(data[z], dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
        return out
    out = np.empty((oz, oy, ox), dtype=np.float32)
    f = factor
    for k in range(oz):
        slab = np.asarray(data[k * f:(k + 1) * f, : oy * f, : ox * f], dtype=np.float32)
        slab = np.nan_to_num(slab, nan=0.0, posinf=0.0, neginf=0.0)
        zs = slab.shape[0]
        out[k] = slab.reshape(zs, oy, f, ox, f).mean(axis=(0, 2, 4))
    return out


def _is_binary(data) -> bool:
    """A 0/1 mask, judged on the ORIGINAL data from a strided sample -- block
    averaging blurs a mask's edge into fractions. A mask wants an absolute
    contour at 0.5, not one in sigma."""
    nz = data.shape[0]
    planes = np.linspace(0, nz - 1, min(nz, 16)).astype(int)
    values = np.unique(np.asarray(data[planes, ::4, ::4]))
    return bool(values.size <= 2 and set(np.round(values.astype(float), 3).tolist()) <= {0.0, 1.0})


def _stats(volume: np.ndarray, binary: bool) -> dict:
    return {"min": float(volume.min()), "max": float(volume.max()),
            "mean": float(volume.mean()), "std": float(volume.std()), "binary": binary}


def prepare(project_dir: Path, mrc_path: str, max_dim: int = DEFAULT_MAX_DIM) -> dict:
    """Make (or find) the viewer's copy of `mrc_path`. Returns {file, ...info};
    `file` is the path of the cached MRC, the rest is JSON-safe."""
    import mrcfile

    if not (MIN_MAX_DIM <= int(max_dim) <= MAX_MAX_DIM):
        raise viz.VizError(f"max_dim must be between {MIN_MAX_DIM} and {MAX_MAX_DIM}")
    p = viz._safe(project_dir, mrc_path)
    if not p.is_file():
        raise viz.VizError(f"volume not found: {mrc_path}")
    with mrcfile.open(p, mode="r", permissive=True, header_only=True) as m:
        nx, ny, nz = int(m.header.nx), int(m.header.ny), int(m.header.nz)
        voxel = float(m.voxel_size.x)
    if min(nx, ny, nz) < 2:
        raise viz.VizError(
            f"{p.name} is a single image ({nx} x {ny} x {nz}), not a volume; open it in the slice viewer instead")
    factor = _factor((nz, ny, nx), int(max_dim))
    st = p.stat()
    key = hashlib.sha1(f"{p}|{st.st_mtime_ns}|{st.st_size}|{factor}".encode()).hexdigest()[:24]
    out, sidecar = cache_dir() / f"{key}.mrc", cache_dir() / f"{key}.json"

    if not (out.is_file() and sidecar.is_file()):
        with mrcfile.mmap(p, mode="r", permissive=True) as m:
            data = viz._as_3d(m.data, p.name)
            reduced = _block_mean(data, factor)
            binary = _is_binary(data)
            origin = (float(m.header.origin.x), float(m.header.origin.y), float(m.header.origin.z))
        info = _stats(reduced, binary)
        tmp = out.with_suffix(".mrc.tmp")
        with mrcfile.new(tmp, overwrite=True) as m:
            m.set_data(reduced)
            m.voxel_size = (voxel * factor) if voxel > 0 else 1.0
            m.header.origin = origin
        os.replace(tmp, out)
        sidecar.write_text(json.dumps(info))
        _prune()
    info = json.loads(sidecar.read_text())
    shape = _reduced_shape((nz, ny, nx), factor)
    info.update({
        "file": out, "name": p.name,
        "original_shape": [nz, ny, nx], "shape": list(shape), "factor": factor,
        "voxel_size": voxel, "viewer_voxel_size": voxel * factor if voxel > 0 else 1.0,
        "size_mb": round(out.stat().st_size / 1e6, 1), "max_dim": int(max_dim),
    })
    return info


def _reduced_shape(shape, factor: int):
    return tuple(max(1, n // factor) for n in shape) if factor > 1 else tuple(shape)


def _prune() -> None:
    files = sorted(cache_dir().glob("*.mrc"), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in files[MAX_CACHED:]:
        try:
            old.unlink()
            old.with_suffix(".json").unlink(missing_ok=True)
        except OSError:
            pass


def public_info(info: dict) -> dict:
    """`prepare`'s result without the server-side file path."""
    return {k: v for k, v in info.items() if k != "file"}
