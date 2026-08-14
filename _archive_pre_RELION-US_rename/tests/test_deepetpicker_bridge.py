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


def test_read_coords_rejects_wrong_field_count(tmp_path):
    path = tmp_path / "bad.coords"
    path.write_text("0 1.0 2.0\n")
    with pytest.raises(ValueError, match="expected 'class_id x y z'"):
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


def test_batch_directory(tmp_path):
    (tmp_path / "TS_01.coords").write_text(SAMPLE_COORDS)
    (tmp_path / "TS_02.coords").write_text("0 1.0 2.0 3.0\n")
    combined = batch_coords_directory_to_particles(tmp_path)
    assert set(combined["rlnTomoName"]) == {"TS_01", "TS_02"}
    assert len(combined) == 4


def test_batch_directory_raises_when_empty(tmp_path):
    with pytest.raises(FileNotFoundError):
        batch_coords_directory_to_particles(tmp_path)
