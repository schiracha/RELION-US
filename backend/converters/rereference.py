"""
rereference.py — move voxel coordinates from one reconstruction of a tomogram
to another (issue: picks made on one reconstruction, then the tomogram was
re-reconstructed with a different thickness/size/binning).

RELION anchors a tomogram at its CENTRE (the tilt-series origin): a
re-reconstruction with a different size or binning changes where voxel 0 is
and how big a voxel is, but not where the centre is. So the physically
correct mapping scales about the centre and, because voxel coordinates count
from the corner, reduces to one scale and one shift per axis:

    v' = v * (b_src / b_dst) + (N_dst - N_src) / (2 * b_dst)

with N the volume size in UNBINNED tilt-series pixels (RELION's own
rlnTomoSizeX/Y/Z) and b the binning (rlnTomoTomogramBinning). Same binning,
thickness 2000 -> 1000 at binning 2.906977 gives a pure Z shift of exactly
-172 voxels; there is never a separate squeeze, because voxels are isotropic.

Picks that land outside the destination volume (the slab that was cropped
away) are dropped and reported by default -- never squeezed back in, which
would move them off their real particles.

Coordinates stored as centred Angstrom (rlnCenteredCoordinate*Angst) don't
need any of this; they are already independent of the reconstruction size.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import pandas as pd
import starfile

PathLike = Union[str, Path]

AXES = ("X", "Y", "Z")
COORD_COLS = ("rlnCoordinateX", "rlnCoordinateY", "rlnCoordinateZ")
SIZE_COLS = ("rlnTomoSizeX", "rlnTomoSizeY", "rlnTomoSizeZ")
BINNING_COL = "rlnTomoTomogramBinning"
TOMO_NAME_COL = "rlnTomoName"


@dataclass(frozen=True)
class VolumeGeometry:
    """A reconstruction's geometry in RELION's own terms: size in UNBINNED
    tilt-series pixels per axis, and the binning the volume is stored at."""
    size_unbinned: tuple[float, float, float]
    binning: float

    def __post_init__(self):
        if self.binning <= 0:
            raise ValueError(f"binning must be positive, got {self.binning!r}")
        if any(s <= 0 for s in self.size_unbinned):
            raise ValueError(f"volume sizes must be positive, got {self.size_unbinned!r}")

    @property
    def size_voxels(self) -> tuple[float, float, float]:
        return tuple(s / self.binning for s in self.size_unbinned)


def read_target_geometry(tomograms_star: PathLike, tomo_name: str) -> VolumeGeometry:
    """The geometry RELION recorded for `tomo_name` in a tomograms.star
    (rlnTomoSizeX/Y/Z + rlnTomoTomogramBinning). Raises ValueError with a
    specific message if the tomogram or a needed column is missing -- never
    guesses."""
    path = Path(tomograms_star)
    if not path.is_file():
        raise FileNotFoundError(f"tomograms STAR not found: {tomograms_star}")
    blocks = starfile.read(path, always_dict=True)
    df: Optional[pd.DataFrame] = None
    for block in blocks.values():
        if isinstance(block, pd.DataFrame) and TOMO_NAME_COL in block.columns:
            df = block
            break
    if df is None:
        raise ValueError(f"{path.name}: no block with {TOMO_NAME_COL}")

    names = df[TOMO_NAME_COL].astype(str)
    rows = df[names == str(tomo_name)]
    if rows.empty:
        # Tolerate a name given with/without an extension, as RELION's own
        # tomogram names are bare stems but a user may type the filename.
        stem = Path(str(tomo_name)).stem
        rows = df[names.map(lambda n: Path(n).stem) == stem]
    if rows.empty:
        raise ValueError(
            f"{tomo_name!r} is not in {path.name} (it lists: "
            f"{', '.join(names.head(5))}{'…' if len(names) > 5 else ''})"
        )
    missing = [c for c in (*SIZE_COLS, BINNING_COL) if c not in df.columns]
    if missing:
        raise ValueError(
            f"{path.name} lacks {', '.join(missing)}, so this tomogram's "
            f"reconstruction geometry can't be read from it"
        )
    row = rows.iloc[0]
    return VolumeGeometry(
        size_unbinned=tuple(float(row[c]) for c in SIZE_COLS),
        binning=float(row[BINNING_COL]),
    )


def resolve_source_geometry(
    dst: VolumeGeometry,
    size_x: Optional[float] = None,
    size_y: Optional[float] = None,
    size_z: Optional[float] = None,
    binning: Optional[float] = None,
) -> VolumeGeometry:
    """The geometry the coordinates were picked on. Anything not given
    inherits the destination's value, so the common case -- same tomogram
    re-reconstructed with only the thickness changed -- is one field."""
    sizes = tuple(
        float(given) if given is not None else dst_size
        for given, dst_size in zip((size_x, size_y, size_z), dst.size_unbinned)
    )
    return VolumeGeometry(sizes, float(binning) if binning is not None else dst.binning)


def axis_scale_and_shift(src: VolumeGeometry, dst: VolumeGeometry) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Per-axis (scale, shift) such that v' = v * scale + shift, in
    destination voxels."""
    scale = src.binning / dst.binning
    scales = (scale, scale, scale)
    shifts = tuple(
        (nd - ns) / (2.0 * dst.binning)
        for ns, nd in zip(src.size_unbinned, dst.size_unbinned)
    )
    return scales, shifts


def rereference_coordinates(
    df: pd.DataFrame,
    src: VolumeGeometry,
    dst: VolumeGeometry,
    drop_outside: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Map rlnCoordinateX/Y/Z in `df` from `src` voxels to `dst` voxels.

    Returns (kept, outside). `outside` always lists the picks that land
    outside the destination volume (already transformed, so you can see where
    they landed). They are removed from `kept` only when drop_outside is
    True; otherwise `kept` is every pick."""
    missing = [c for c in COORD_COLS if c not in df.columns]
    if missing:
        raise KeyError(f"rereference_coordinates needs columns {missing}")

    scales, shifts = axis_scale_and_shift(src, dst)
    out = df.copy()
    for col, sc, sh in zip(COORD_COLS, scales, shifts):
        out[col] = df[col].to_numpy(dtype=float) * sc + sh

    inside = pd.Series(True, index=out.index)
    for col, n in zip(COORD_COLS, dst.size_voxels):
        inside &= (out[col] >= 0.0) & (out[col] < n)

    outside = out[~inside]
    kept = out[inside] if drop_outside else out
    return kept.reset_index(drop=True), outside.reset_index(drop=True)
