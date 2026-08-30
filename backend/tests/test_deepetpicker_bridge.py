import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converters.deepetpicker_bridge import (
    batch_coords_directory_to_particles,
    coords_to_relion_particles,
    read_coords,
)

SAMPLE_COORDS = "0 100.0 110.0 50.0\n1 150.5 160.5 55.5\n0 200.0 210.0 60.0\n"


def test_read_coords(tmp_path):
    path = tmp_path / "TS_01.coords"
    path.write_text(SAMPLE_COORDS)
    df = read_coords(path)
    assert list(df.columns) == ["class_id", "x", "y", "z"]
    assert len(df) == 3
    assert df.loc[1, "x"] == pytest.approx(150.5)


def test_read_coords_accepts_3col_xyz(tmp_path):
    """DeepETPicker's own utils/coords_to_relion4.py accepts a bare 3-column
    `x y z` file (assigning class_id = 1), so we must too — rejecting it
    would be stricter than DeepETPicker itself. Verified against
    github.com/cbmi-group/DeepETPicker, 2026-08-14."""
    path = tmp_path / "three_col.coords"
    path.write_text("100.0 200.0 300.0\n101.0 201.0 301.0\n")
    df = read_coords(path)
    assert list(df.columns) == ["class_id", "x", "y", "z"]
    assert len(df) == 2
    assert list(df["class_id"]) == [1, 1]  # defaulted
    assert df.loc[0, "x"] == pytest.approx(100.0)
    assert df.loc[0, "z"] == pytest.approx(300.0)


def test_read_coords_rejects_wrong_field_count(tmp_path):
    # 2 columns and 6+ columns are genuinely malformed (neither `x y z` nor
    # `class_id x y z`, and not the recognized 5-column Coords_withArea
    # shape either) and must still be rejected with the generic message.
    for bad in ("1.0 2.0\n", "0 1.0 2.0 3.0 4.0 5.0\n"):
        path = tmp_path / "bad.coords"
        path.write_text(bad)
        with pytest.raises(ValueError, match="expected 'class_id x y z'"):
            read_coords(path)


def test_read_coords_rejects_5col_with_actionable_message(tmp_path):
    """DeepETPicker also emits a 5-column `Coords_withArea` variant
    (class_id x y z area, test.py's Coords_withArea writer). Auto-parsing
    it by taking the last 3 columns (mirroring coords_to_relion4.py's own
    slicing) would silently grab (y, z, area) instead of (x, y, z) here —
    so this must stay a hard rejection, but with a specific, actionable
    message pointing the user at Coords_All instead of the generic
    field-count error. Verified against the real DeepETPicker source
    checkout, 2026-08-30."""
    path = tmp_path / "with_area.coords"
    path.write_text("0 100.0 110.0 50.0 12.5\n")
    with pytest.raises(ValueError, match="Coords_withArea"):
        read_coords(path)


def test_coords_to_relion_particles_no_scaling(tmp_path):
    path = tmp_path / "TS_01.coords"
    path.write_text(SAMPLE_COORDS)
    out = coords_to_relion_particles(path, tomo_name="TS_01")
    assert set(out["rlnTomoName"]) == {"TS_01"}
    assert out.loc[0, "rlnCoordinateX"] == pytest.approx(100.0)
    assert list(out["rlnClassNumber"]) == [0, 1, 0]


def test_coords_to_relion_particles_with_binning(tmp_path):
    path = tmp_path / "TS_01.coords"
    path.write_text(SAMPLE_COORDS)
    out = coords_to_relion_particles(path, tomo_name="TS_01", binning_factor=2.0)
    assert out.loc[0, "rlnCoordinateX"] == pytest.approx(200.0)


def test_coords_to_relion_particles_swap_yz_and_class_preserved(tmp_path):
    path = tmp_path / "TS_01.coords"
    path.write_text(SAMPLE_COORDS)
    out = coords_to_relion_particles(path, tomo_name="TS_01", swap_yz=True)
    # first row: y=110, z=50 -> swapped
    assert out.loc[0, "rlnCoordinateY"] == pytest.approx(50.0)
    assert out.loc[0, "rlnCoordinateZ"] == pytest.approx(110.0)
    # class_id column must still line up correctly after the transform
    assert list(out["rlnClassNumber"]) == [0, 1, 0]


def test_coords_to_relion_particles_mirror_after_binning(tmp_path):
    path = tmp_path / "TS_01.coords"
    path.write_text(SAMPLE_COORDS)
    # binning 2 -> x0 = 200; mirror about a 1000-voxel axis -> (1000-1)-200 = 799
    out = coords_to_relion_particles(
        path, tomo_name="TS_01", binning_factor=2.0, flip_x=True, tomo_size_x=1000
    )
    assert out.loc[0, "rlnCoordinateX"] == pytest.approx(799.0)


def test_batch_directory(tmp_path):
    (tmp_path / "TS_01.coords").write_text(SAMPLE_COORDS)
    (tmp_path / "TS_02.coords").write_text("0 1.0 2.0 3.0\n")
    combined = batch_coords_directory_to_particles(tmp_path)
    assert set(combined["rlnTomoName"]) == {"TS_01", "TS_02"}
    assert len(combined) == 4


def test_batch_directory_raises_when_empty(tmp_path):
    with pytest.raises(FileNotFoundError):
        batch_coords_directory_to_particles(tmp_path)
