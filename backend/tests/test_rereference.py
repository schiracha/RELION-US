"""
Tests for converters/rereference.py -- moving voxel picks between two
reconstructions of the same tomogram (different thickness / size / binning).
"""
import sys
from pathlib import Path

import pandas as pd
import pytest
import starfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converters import rereference
from converters.rereference import VolumeGeometry

BIN = 1000 / 344.0   # RELION's rlnTomoTomogramBinning for the 1000 px-thick EMPIAR job (2.906977)


def _df(xs, ys, zs):
    return pd.DataFrame({
        "rlnTomoName": ["TS_01"] * len(xs),
        "rlnCoordinateX": xs, "rlnCoordinateY": ys, "rlnCoordinateZ": zs,
    })


def test_thickness_only_change_is_a_pure_z_shift():
    """2000 -> 1000 unbinned thickness at the same binning: exactly the
    (2000-1000)/2 = 500 unbinned px = 172 voxel shift, no scale, X/Y fixed."""
    src = VolumeGeometry((4000, 4000, 2000), BIN)
    dst = VolumeGeometry((4000, 4000, 1000), BIN)
    scales, shifts = rereference.axis_scale_and_shift(src, dst)
    assert scales == (1.0, 1.0, 1.0)
    assert shifts[0] == 0 and shifts[1] == 0
    assert shifts[2] == pytest.approx(-172.0)

    kept, outside = rereference.rereference_coordinates(_df([100.0], [200.0], [344.0]), src, dst)
    assert kept["rlnCoordinateX"].iloc[0] == pytest.approx(100.0)
    assert kept["rlnCoordinateY"].iloc[0] == pytest.approx(200.0)
    assert kept["rlnCoordinateZ"].iloc[0] == pytest.approx(172.0)   # old centre -> new centre
    assert outside.empty


def test_old_centre_maps_to_new_centre():
    src = VolumeGeometry((4000, 4000, 2000), BIN)
    dst = VolumeGeometry((4000, 4000, 1000), BIN)
    centre = _df([src.size_voxels[0] / 2], [src.size_voxels[1] / 2], [src.size_voxels[2] / 2])
    kept, _ = rereference.rereference_coordinates(centre, src, dst)
    for col, n in zip(rereference.COORD_COLS, dst.size_voxels):
        assert kept[col].iloc[0] == pytest.approx(n / 2)


def test_picks_in_the_cropped_slab_are_dropped_not_squeezed():
    src = VolumeGeometry((4000, 4000, 2000), BIN)
    dst = VolumeGeometry((4000, 4000, 1000), BIN)
    # old Z voxels: 0 and 687 are in the removed top/bottom slabs; 344 is the centre
    df = _df([10.0, 10.0, 10.0], [10.0, 10.0, 10.0], [0.0, 344.0, 687.0])
    kept, outside = rereference.rereference_coordinates(df, src, dst)
    assert len(kept) == 1 and len(outside) == 2
    assert kept["rlnCoordinateZ"].iloc[0] == pytest.approx(172.0)
    # outside picks keep their (shifted, NOT rescaled) positions for inspection
    assert sorted(outside["rlnCoordinateZ"]) == pytest.approx([-172.0, 515.0])


def test_drop_outside_false_keeps_everything():
    src = VolumeGeometry((4000, 4000, 2000), BIN)
    dst = VolumeGeometry((4000, 4000, 1000), BIN)
    df = _df([10.0, 10.0], [10.0, 10.0], [0.0, 344.0])
    kept, outside = rereference.rereference_coordinates(df, src, dst, drop_outside=False)
    assert len(kept) == 2 and len(outside) == 1


def test_binning_change_scales_uniformly_and_shifts():
    """Picks on a bin-3 volume (4000 x 4000 x 1200 unbinned) moved onto the
    bin-2.907, 1000 thick reconstruction: one uniform 1.032 scale, then the
    centre-preserving Z shift -- never an independent Z squeeze."""
    src = VolumeGeometry((4000, 4000, 1200), 3.0)
    dst = VolumeGeometry((4000, 4000, 1000), BIN)
    scales, shifts = rereference.axis_scale_and_shift(src, dst)
    assert scales == pytest.approx((3.0 / BIN,) * 3)
    assert scales[0] == pytest.approx(1.032)
    assert shifts[0] == pytest.approx(0.0) and shifts[1] == pytest.approx(0.0)
    assert shifts[2] == pytest.approx(-34.4)


def test_mapping_preserves_physical_position():
    """The defining property: centred Angstrom position is unchanged by the
    move, for any combination of size and binning change."""
    px = 3.44
    src = VolumeGeometry((3996, 3996, 1200), 3.0)
    dst = VolumeGeometry((4000, 4000, 1000), BIN)
    df = _df([111.0, 700.0], [222.0, 50.0], [60.0, 300.0])
    kept, outside = rereference.rereference_coordinates(df, src, dst, drop_outside=False)
    for axis, col in enumerate(rereference.COORD_COLS):
        before = (df[col] - src.size_voxels[axis] / 2) * px * src.binning
        after = (kept[col] - dst.size_voxels[axis] / 2) * px * dst.binning
        assert list(after) == pytest.approx(list(before))


def test_same_geometry_is_identity():
    g = VolumeGeometry((4000, 4000, 1000), BIN)
    df = _df([5.0], [6.0], [7.0])
    kept, outside = rereference.rereference_coordinates(df, g, g)
    assert kept[list(rereference.COORD_COLS)].iloc[0].tolist() == pytest.approx([5.0, 6.0, 7.0])


def test_other_columns_survive():
    g = VolumeGeometry((4000, 4000, 1000), BIN)
    df = _df([5.0], [6.0], [7.0]).assign(rlnClassNumber=3)
    kept, _ = rereference.rereference_coordinates(df, g, g)
    assert kept["rlnClassNumber"].iloc[0] == 3 and kept["rlnTomoName"].iloc[0] == "TS_01"


def test_resolve_source_inherits_unspecified_from_target():
    dst = VolumeGeometry((4000, 4000, 1000), BIN)
    src = rereference.resolve_source_geometry(dst, size_z=2000)
    assert src == VolumeGeometry((4000, 4000, 2000), BIN)
    assert rereference.resolve_source_geometry(dst, binning=3).binning == 3


def test_invalid_geometry_rejected():
    with pytest.raises(ValueError):
        VolumeGeometry((4000, 4000, 0), 3.0)
    with pytest.raises(ValueError):
        VolumeGeometry((4000, 4000, 1000), 0)


def _write_tomograms_star(tmp_path, names=("TS_01", "TS_02"), drop_col=None):
    df = pd.DataFrame({
        "rlnTomoName": list(names),
        "rlnTomoSizeX": [4000] * len(names), "rlnTomoSizeY": [4000] * len(names),
        "rlnTomoSizeZ": [1000] * len(names), "rlnTomoTomogramBinning": [BIN] * len(names),
    })
    if drop_col:
        df = df.drop(columns=[drop_col])
    path = tmp_path / "tomograms.star"
    starfile.write({"global": df}, path, overwrite=True)
    return path


def test_read_target_geometry(tmp_path):
    star = _write_tomograms_star(tmp_path)
    g = rereference.read_target_geometry(star, "TS_02")
    assert g.size_unbinned == (4000, 4000, 1000)
    assert g.binning == pytest.approx(BIN)
    assert g.size_voxels[2] == pytest.approx(344.0)


def test_read_target_geometry_tolerates_an_extension(tmp_path):
    star = _write_tomograms_star(tmp_path)
    assert rereference.read_target_geometry(star, "TS_01.mrc").size_unbinned[2] == 1000


def test_read_target_geometry_unknown_tomogram(tmp_path):
    star = _write_tomograms_star(tmp_path)
    with pytest.raises(ValueError, match="not in tomograms.star"):
        rereference.read_target_geometry(star, "TS_99")


def test_read_target_geometry_missing_column(tmp_path):
    star = _write_tomograms_star(tmp_path, drop_col="rlnTomoTomogramBinning")
    with pytest.raises(ValueError, match="rlnTomoTomogramBinning"):
        rereference.read_target_geometry(star, "TS_01")


def test_read_target_geometry_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        rereference.read_target_geometry(tmp_path / "nope.star", "TS_01")
