import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converters.coord_transform import apply_coordinate_transform


def _df():
    return pd.DataFrame(
        {"rlnCoordinateX": [10.0], "rlnCoordinateY": [20.0], "rlnCoordinateZ": [5.0]}
    )


def test_swap_yz():
    out = apply_coordinate_transform(_df(), swap_yz=True)
    row = out.iloc[0]
    assert row["rlnCoordinateX"] == 10.0
    assert row["rlnCoordinateY"] == 5.0   # was Z
    assert row["rlnCoordinateZ"] == 20.0  # was Y


def test_mirror_needs_dimension():
    with pytest.raises(ValueError, match="tomo_size_z"):
        apply_coordinate_transform(_df(), flip_z=True)


def test_mirror_about_dimension():
    # 0-based mirror about the volume centre: (size-1) - coord
    out = apply_coordinate_transform(_df(), flip_x=True, tomo_size_x=100)
    assert out.iloc[0]["rlnCoordinateX"] == 89.0


def test_mirror_keeps_coordinates_inside_the_volume():
    """The endpoints must map onto each other, not off the end: 0 -> size-1."""
    import pandas as pd
    edge = pd.DataFrame({
        "rlnCoordinateX": [0.0, 99.0], "rlnCoordinateY": [0.0, 0.0], "rlnCoordinateZ": [0.0, 0.0],
    })
    out = apply_coordinate_transform(edge, flip_x=True, tomo_size_x=100)
    assert list(out["rlnCoordinateX"]) == [99.0, 0.0]


def test_mirror_then_swap_order():
    # flips apply first (about original axes), THEN swap_yz
    out = apply_coordinate_transform(
        _df(), swap_yz=True, flip_y=True, tomo_size_y=100
    )
    row = out.iloc[0]
    # original Y=20 mirrored about a 100-voxel axis -> (100-1)-20 = 79, then swapped into Z
    assert row["rlnCoordinateZ"] == 79.0
    assert row["rlnCoordinateY"] == 5.0  # original Z


def test_noop_returns_equivalent_copy():
    df = _df()
    out = apply_coordinate_transform(df)
    assert out.equals(df)
    assert out is not df  # a copy, not the same object


def test_missing_columns_raise():
    with pytest.raises(KeyError):
        apply_coordinate_transform(pd.DataFrame({"rlnCoordinateX": [1.0]}), swap_yz=True)
