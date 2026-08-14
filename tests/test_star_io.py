"""
Unit tests for converters/star_io.py using synthetic STAR files (no RELION
install required — these test the Python I/O layer only).

Run with: python3 -m pytest tests/ -v
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converters.star_io import (
    MissingColumnError,
    StarDocument,
    load_particles,
    load_tomograms,
    write_particles,
)


@pytest.fixture
def synthetic_particles_df():
    return pd.DataFrame(
        {
            "rlnTomoName": ["TS_01", "TS_01", "TS_02"],
            "rlnTomoParticleId": [1, 2, 1],
            "rlnCoordinateX": [100.0, 150.5, 200.0],
            "rlnCoordinateY": [110.0, 160.5, 210.0],
            "rlnCoordinateZ": [50.0, 55.5, 60.0],
        }
    )


def test_write_then_load_particles(tmp_path, synthetic_particles_df):
    out = tmp_path / "particles.star"
    write_particles(synthetic_particles_df, out, block_name="particles")
    assert out.exists()

    loaded = load_particles(out, block="particles")
    assert list(loaded["rlnTomoName"]) == ["TS_01", "TS_01", "TS_02"]
    assert loaded.shape[0] == 3


def test_write_particles_rejects_missing_coordinate_column(tmp_path, synthetic_particles_df):
    bad_df = synthetic_particles_df.drop(columns=["rlnCoordinateZ"])
    out = tmp_path / "bad_particles.star"
    with pytest.raises(MissingColumnError):
        write_particles(bad_df, out)
    assert not out.exists()


def test_load_particles_rejects_missing_tomo_name(tmp_path, synthetic_particles_df):
    bad_df = synthetic_particles_df.drop(columns=["rlnTomoName"])
    out = tmp_path / "no_name.star"
    import starfile

    starfile.write({"particles": bad_df}, out)
    with pytest.raises(MissingColumnError):
        load_particles(out, block="particles")


def test_load_tomograms_multi_block(tmp_path):
    import starfile

    tomo_df = pd.DataFrame(
        {
            "rlnTomoName": ["TS_01", "TS_02"],
            "rlnVoltage": [300.0, 300.0],
            "rlnTomoTiltSeriesPixelSize": [1.35, 1.35],
        }
    )
    global_df = pd.DataFrame({"rlnTomoSubTomosAre2DStacks": [0]})
    out = tmp_path / "tomograms.star"
    starfile.write({"global": global_df, "tomograms": tomo_df}, out)

    doc = StarDocument.read(out)
    assert set(doc.blocks.keys()) == {"global", "tomograms"}
    loaded = load_tomograms(out, block="tomograms")
    assert list(loaded["rlnTomoName"]) == ["TS_01", "TS_02"]


def test_backup_before_overwrite(tmp_path, synthetic_particles_df):
    from converters.star_io import backup_before_overwrite

    out = tmp_path / "particles.star"
    write_particles(synthetic_particles_df, out)
    backup = backup_before_overwrite(out)
    assert backup is not None
    assert backup.exists()
    assert backup.read_bytes() == out.read_bytes()
