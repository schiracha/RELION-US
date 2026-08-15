"""
coord_transform.py — one shared, tested implementation of the coordinate
axis flips/swaps that come up when importing particle picks from another
package into RELION-5 tomography.

Why this exists: different tomography packages disagree on axis conventions.
The two that actually bite in practice:

  * Y/Z swap — IMOD tomograms are stored either "rotated" (depth = Z, what
    RELION expects) or "flipped"/`trimvol -yz` (Y and Z swapped), and a model
    built on a raw `tilt` reconstruction has depth in Y. See
    imod_bridge.model_to_coordinates for the full, sourced explanation.
  * Axis mirror — packages differ on whether an axis origin is at the top or
    bottom of the volume, so a coordinate sometimes needs `coord -> (dim -
    coord)` about the tomogram's size along that axis.

Rather than re-implement these per importer (and risk them drifting apart),
every coordinate importer routes through `apply_coordinate_transform` here.
It operates on RELION's own `rlnCoordinateX/Y/Z` columns, so it composes
with whatever else an importer produces.

Mirroring needs the tomogram dimension along that axis (in the SAME pixel/
voxel units as the coordinates). A mirror requested without the needed
dimension raises a clear error rather than silently producing wrong
coordinates — consistent with this project's "fail loudly, don't guess"
stance for anything that would corrupt scientific data.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

X, Y, Z = "rlnCoordinateX", "rlnCoordinateY", "rlnCoordinateZ"


def apply_coordinate_transform(
    df: pd.DataFrame,
    swap_yz: bool = False,
    flip_x: bool = False,
    flip_y: bool = False,
    flip_z: bool = False,
    tomo_size_x: Optional[float] = None,
    tomo_size_y: Optional[float] = None,
    tomo_size_z: Optional[float] = None,
) -> pd.DataFrame:
    """
    Return a copy of `df` with the requested axis transforms applied to its
    rlnCoordinateX/Y/Z columns.

    Order of operations is deterministic and documented: mirrors are applied
    FIRST (each about its own axis dimension), THEN the Y/Z swap. That order
    means the tomo_size_* values always refer to the ORIGINAL axes the caller
    measured, which is the least surprising: you give the dimensions of the
    volume your coordinates currently live in, tick which axes to mirror, and
    optionally swap Y/Z last to move into RELION's depth-in-Z frame.

    swap_yz:      swap the Y and Z coordinate columns (handedness-changing;
                  the usual fix for an IMOD flipped/raw-tilt tomogram).
    flip_{x,y,z}: mirror that axis: coord -> (tomo_size_axis - coord).
                  Requires the matching tomo_size_* (raises ValueError if a
                  flip is requested without it).
    """
    required = {c for c in (X, Y, Z) if c not in df.columns}
    if required:
        raise KeyError(f"apply_coordinate_transform needs columns {sorted(required)} present")

    out = df.copy()

    for do_flip, col, size, axisname in (
        (flip_x, X, tomo_size_x, "X"),
        (flip_y, Y, tomo_size_y, "Y"),
        (flip_z, Z, tomo_size_z, "Z"),
    ):
        if not do_flip:
            continue
        if size is None or float(size) <= 0:
            raise ValueError(
                f"flip_{axisname.lower()} requested but tomo_size_{axisname.lower()} "
                f"was not given (need the tomogram's {axisname} dimension, in the "
                f"same units as the coordinates, to mirror about it)"
            )
        out[col] = float(size) - out[col]

    if swap_yz:
        out[Y], out[Z] = out[Z].copy(), out[Y].copy()

    return out
