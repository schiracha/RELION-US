import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import viz

mrcfile = pytest.importorskip("mrcfile")
starfile = pytest.importorskip("starfile")


def _make_project(tmp_path):
    (tmp_path / ".relion_us").mkdir()
    vol = (np.random.rand(30, 50, 40) * 100).astype(np.float32)  # z,y,x
    with mrcfile.new(tmp_path / "TS_01.mrc", overwrite=True) as m:
        m.set_data(vol)
        m.voxel_size = 10.0
    df = pd.DataFrame({
        "rlnTomoName": ["TS_01"] * 3,
        "rlnCoordinateX": [10.0, 20.0, 30.0],
        "rlnCoordinateY": [15.0, 25.0, 35.0],
        "rlnCoordinateZ": [5.0, 15.0, 25.0],
        "rlnClassNumber": [1, 1, 2],
    })
    starfile.write({"particles": df}, tmp_path / "particles.star", overwrite=True)
    return tmp_path


def test_inspect_mrc_plus_particles(tmp_path):
    _make_project(tmp_path)
    info = viz.inspect(tmp_path, "TS_01.mrc", "particles.star")
    assert info["kind"] == "volume"
    assert info["tomograms"][0]["name"] == "TS_01"
    assert info["particles_path"].endswith("particles.star")


def test_volume_info_dims_and_contrast(tmp_path):
    _make_project(tmp_path)
    vi = viz.volume_info(tmp_path, "TS_01.mrc")
    assert (vi["nx"], vi["ny"], vi["nz"]) == (40, 50, 30)
    assert vi["voxel_size"] == pytest.approx(10.0)
    assert vi["contrast_hi"] > vi["contrast_lo"]


def test_render_slice_returns_png_each_axis(tmp_path):
    _make_project(tmp_path)
    for axis, idx in (("z", 15), ("y", 25), ("x", 20)):
        png = viz.render_slice_png(tmp_path, "TS_01.mrc", axis, idx)
        assert png[:4] == b"\x89PNG", axis


def test_load_picks_matches_tomo(tmp_path):
    _make_project(tmp_path)
    vi = viz.volume_info(tmp_path, "TS_01.mrc")
    out = viz.load_picks(tmp_path, "particles.star", tomo_name="TS_01.mrc", volume=vi)
    assert out["matched"] is True
    assert len(out["picks"]) == 3
    assert out["picks"][0] == {"x": 10.0, "y": 15.0, "z": 5.0, "class": 1}


def test_load_picks_flags_mismatch(tmp_path):
    _make_project(tmp_path)
    df = pd.DataFrame({
        "rlnTomoName": ["TS_99"] * 2,
        "rlnCoordinateX": [1.0, 2.0], "rlnCoordinateY": [1.0, 2.0], "rlnCoordinateZ": [1.0, 2.0],
    })
    starfile.write({"particles": df}, tmp_path / "other.star", overwrite=True)
    out = viz.load_picks(tmp_path, "other.star", tomo_name="TS_01.mrc")
    assert out["matched"] is False
    assert "doesn't match" in out["message"]


def test_particles_only_star_needs_mrc(tmp_path):
    _make_project(tmp_path)
    info = viz.inspect(tmp_path, "particles.star")
    assert info.get("needs_mrc") is True


def test_path_traversal_blocked(tmp_path):
    _make_project(tmp_path)
    with pytest.raises(viz.VizError, match="outside the project"):
        viz.volume_info(tmp_path, "../../../etc/passwd")


def test_render_slice_respects_contrast_override(tmp_path):
    _make_project(tmp_path)
    # a degenerate lo>hi is corrected, not crashed
    png = viz.render_slice_png(tmp_path, "TS_01.mrc", "z", 0, lo=50.0, hi=50.0)
    assert png[:4] == b"\x89PNG"
