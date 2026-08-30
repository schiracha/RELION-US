"""
deepetpicker_bridge.py — bridge between DeepETPicker's picked-coordinate
output and RELION-5 tomography particles.star.

Verified against the DeepETPicker README (github.com/cbmi-group/DeepETPicker,
accessed 2026-08-14; DeepETPicker itself: Liu et al. 2024, Nat Commun
15:2090, PMID 38453943, DOI 10.1038/s41467-024-46041-0), and re-verified
2026-08-30 directly against a real DeepETPicker source checkout
(test.py's Coords_All writer, utils/misc.py's get_centroids, and
utils/coords_to_relion4.py) — column order, delimiter, and voxel units
all match exactly:

- DeepETPicker's native output is `*.coords`: four whitespace-separated
  columns `class_id x y z`, coordinates in voxels of the tomogram it was
  run on.
- DeepETPicker already ships its own conversion utilities to RELION 3
  (*.star/*.coords) and RELION 4 (`coords_to_relion4.py`) formats. Per your
  "don't reinvent the wheel" instruction, if you have DeepETPicker's own
  converter available, prefer running that directly — it will track their
  schema changes. This module exists for the case where you want
  `.coords` -> particles.star wired directly into this project's GUI/SLURM
  flow (e.g. batching many tomograms' `.coords` files in one call, or
  feeding straight into imod_bridge.coordinates_to_model() for visual QC)
  without shelling out to a separately-located script.

Coordinate units: DeepETPicker coordinates are voxels in the tomogram
volume DeepETPicker was run on. RELION-5 particle coordinates are pixels in
the RELION tomogram's own reconstruction — if that's a different binning
than what DeepETPicker used, pass binning_factor (DeepETPicker_voxel_size /
RELION_tomogram_pixel_size) to rescale.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import pandas as pd

from .coord_transform import apply_coordinate_transform

PathLike = Union[str, Path]

COORDS_COLUMNS = ("class_id", "x", "y", "z")


def read_coords(path: PathLike) -> pd.DataFrame:
    """
    Read a DeepETPicker .coords file into a class_id/x/y/z DataFrame.

    DeepETPicker's own converter (utils/coords_to_relion4.py in
    github.com/cbmi-group/DeepETPicker, verified 2026-08-14) reads the X/Y/Z
    from the LAST three columns and only treats column 0 as class_id when
    the row has exactly 4 columns — i.e. it accepts BOTH the 4-column
    `class_id x y z` form and a bare 3-column `x y z` form (assigning
    class_id = 1 in the latter). We match that tolerance here rather than
    hard-requiring 4 columns, so a 3-column .coords that DeepETPicker itself
    would accept doesn't get rejected. Coordinates are voxels of the
    tomogram DeepETPicker was run on.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    rows = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 4:
            class_id, x, y, z = parts
        elif len(parts) == 3:
            # bare `x y z` — DeepETPicker defaults class_id to 1 here
            class_id, (x, y, z) = 1, parts
        elif len(parts) == 5:
            # DeepETPicker's `Coords_withArea` variant (test.py's
            # Coords_withArea writer): `class_id x y z area`. Deliberately
            # NOT auto-parsed: DeepETPicker's own coords_to_relion4.py
            # slices the LAST 3 columns as X/Y/Z, which for this 5-column
            # form would grab (y, z, area) instead of (x, y, z) — silently
            # corrupting coordinates. That converter is only ever pointed
            # at the 4-column Coords_All output in practice; mirror that
            # constraint here with a clear error instead of guessing.
            raise ValueError(
                f"{path}:{lineno}: got 5 fields — this looks like "
                f"DeepETPicker's 'Coords_withArea' output ('class_id x y z "
                f"area'), which this bridge does not support (the trailing "
                f"area column can't be safely stripped without risking a "
                f"misread). Point this at DeepETPicker's 'Coords_All' "
                f"directory instead (4-column 'class_id x y z'): {line!r}"
            )
        else:
            raise ValueError(
                f"{path}:{lineno}: expected 'class_id x y z' (4 cols) or "
                f"'x y z' (3 cols), got {len(parts)} field(s): {line!r}"
            )
        rows.append((int(float(class_id)), float(x), float(y), float(z)))
    return pd.DataFrame(rows, columns=list(COORDS_COLUMNS))


def coords_to_relion_particles(
    coords: Union[PathLike, pd.DataFrame],
    tomo_name: str,
    binning_factor: float = 1.0,
    keep_class_id: bool = True,
    swap_yz: bool = False,
    flip_x: bool = False,
    flip_y: bool = False,
    flip_z: bool = False,
    tomo_size_x: Optional[float] = None,
    tomo_size_y: Optional[float] = None,
    tomo_size_z: Optional[float] = None,
) -> pd.DataFrame:
    """
    Convert DeepETPicker coordinates (a .coords path, or an already-loaded
    DataFrame from read_coords) into a RELION-5-compatible particles
    DataFrame for a single tomogram: rlnTomoName, rlnCoordinateX/Y/Z, and
    optionally rlnClassNumber carried over from DeepETPicker's class_id.

    binning_factor rescales x/y/z: relion_coord = deepet_coord * binning_factor.
    Leave at 1.0 if DeepETPicker was run on the same-binned volume RELION
    will use for extraction.
    """
    df = coords if isinstance(coords, pd.DataFrame) else read_coords(coords)
    missing = [c for c in COORDS_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(f"Input missing expected column(s) {missing}: {list(df.columns)}")

    out = pd.DataFrame(
        {
            "rlnTomoName": [tomo_name] * len(df),
            "rlnCoordinateX": df["x"].to_numpy() * binning_factor,
            "rlnCoordinateY": df["y"].to_numpy() * binning_factor,
            "rlnCoordinateZ": df["z"].to_numpy() * binning_factor,
        }
    )
    # Optional axis flips/swap, applied AFTER binning (so tomo_size_* are in
    # the same rescaled units as the coordinates).
    if swap_yz or flip_x or flip_y or flip_z:
        out = apply_coordinate_transform(
            out,
            swap_yz=swap_yz,
            flip_x=flip_x, flip_y=flip_y, flip_z=flip_z,
            tomo_size_x=tomo_size_x, tomo_size_y=tomo_size_y, tomo_size_z=tomo_size_z,
        )
    if keep_class_id:
        out["rlnClassNumber"] = df["class_id"].astype(int).to_numpy()
    return out


def batch_coords_directory_to_particles(
    coords_dir: PathLike,
    binning_factor: float = 1.0,
    tomo_name_from_filename: Optional[callable] = None,
) -> pd.DataFrame:
    """
    Convert every `*.coords` file in a directory into one combined RELION
    particles DataFrame, tagging each row with the tomogram name derived
    from its filename (default: filename stem, e.g. TS_01.coords -> TS_01).
    Pass tomo_name_from_filename to customize (e.g. strip a suffix your
    DeepETPicker run added).
    """
    coords_dir = Path(coords_dir)
    if not coords_dir.is_dir():
        raise NotADirectoryError(coords_dir)

    name_fn = tomo_name_from_filename or (lambda p: p.stem)
    frames = []
    for coords_path in sorted(coords_dir.glob("*.coords")):
        tomo_name = name_fn(coords_path)
        frames.append(
            coords_to_relion_particles(coords_path, tomo_name, binning_factor=binning_factor)
        )

    if not frames:
        raise FileNotFoundError(f"No *.coords files found in {coords_dir}")
    return pd.concat(frames, ignore_index=True)
