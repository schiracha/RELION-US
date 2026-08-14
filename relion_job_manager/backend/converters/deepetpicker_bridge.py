"""
deepetpicker_bridge.py — bridge between DeepETPicker's picked-coordinate
output and RELION-5 tomography particles.star.

Verified against the DeepETPicker README (github.com/cbmi-group/DeepETPicker,
accessed 2026-08-14; DeepETPicker itself: Liu et al. 2024, Nat Commun
15:2090, PMID 38453943, DOI 10.1038/s41467-024-46041-0):

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

PathLike = Union[str, Path]

COORDS_COLUMNS = ("class_id", "x", "y", "z")


def read_coords(path: PathLike) -> pd.DataFrame:
    """Read a DeepETPicker .coords file (class_id, x, y, z; whitespace-separated)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    rows = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 4:
            raise ValueError(
                f"{path}:{lineno}: expected 'class_id x y z', got {len(parts)} "
                f"field(s): {line!r}"
            )
        class_id, x, y, z = parts
        rows.append((int(float(class_id)), float(x), float(y), float(z)))
    return pd.DataFrame(rows, columns=list(COORDS_COLUMNS))


def coords_to_relion_particles(
    coords: Union[PathLike, pd.DataFrame],
    tomo_name: str,
    binning_factor: float = 1.0,
    keep_class_id: bool = True,
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
            "rlnCoordinateX": df["x"] * binning_factor,
            "rlnCoordinateY": df["y"] * binning_factor,
            "rlnCoordinateZ": df["z"] * binning_factor,
        }
    )
    if keep_class_id:
        out["rlnClassNumber"] = df["class_id"].astype(int)
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
