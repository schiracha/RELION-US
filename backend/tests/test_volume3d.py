"""
The downsampled copy of an MRC volume served to the 3D (Mol*) viewer.
"""
import sys
from pathlib import Path

import mrcfile
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
import project_manager
import viz
import volume3d

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture(autouse=True)
def _private_cache(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(volume3d, "cache_dir", lambda: cache)
    return cache


def _write(path, data, voxel=2.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with mrcfile.new(path, overwrite=True) as m:
        m.set_data(data)
        m.voxel_size = voxel


def test_a_volume_within_the_limit_is_served_at_full_size(tmp_path):
    data = np.random.default_rng(0).normal(size=(10, 12, 14)).astype(np.float32)
    _write(tmp_path / "map.mrc", data, voxel=3.0)
    info = volume3d.prepare(tmp_path, "map.mrc", 64)
    assert info["factor"] == 1 and info["shape"] == [10, 12, 14] and info["viewer_voxel_size"] == 3.0
    with mrcfile.open(info["file"]) as m:
        assert np.allclose(m.data, data) and float(m.voxel_size.x) == 3.0


def test_a_big_volume_is_block_averaged_and_keeps_its_physical_size(tmp_path):
    data = np.zeros((64, 64, 128), dtype=np.float32)
    data[:32] = 4.0                                              # the lower half in z
    _write(tmp_path / "big.mrc", data, voxel=5.0)
    info = volume3d.prepare(tmp_path, "big.mrc", 32)
    assert info["factor"] == 4 and info["shape"] == [16, 16, 32] and info["original_shape"] == [64, 64, 128]
    assert info["viewer_voxel_size"] == 20.0 and info["voxel_size"] == 5.0
    with mrcfile.open(info["file"]) as m:
        assert m.data.shape == (16, 16, 32) and m.data.dtype == np.float32
        assert np.allclose(m.data[:8], 4.0) and np.allclose(m.data[8:], 0.0)
        # the box is as large in angstroms as before: 128 * 5 == 32 * 20
        assert float(m.voxel_size.x) * m.data.shape[2] == 640.0


def test_each_voxel_is_the_mean_of_its_block(tmp_path):
    big = np.arange(64 * 64 * 64, dtype=np.float32).reshape(64, 64, 64)
    _write(tmp_path / "ramp64.mrc", big)
    got = volume3d.prepare(tmp_path, "ramp64.mrc", 32)
    assert got["factor"] == 2
    with mrcfile.open(got["file"]) as m:
        assert np.isclose(m.data[0, 0, 0], big[:2, :2, :2].mean())
        assert np.isclose(m.data[5, 7, 3], big[10:12, 14:16, 6:8].mean())


def test_nan_voxels_are_zeroed_not_propagated(tmp_path):
    data = np.ones((8, 8, 8), dtype=np.float32)
    data[2, 2, 2] = np.nan
    data[3, 3, 3] = np.inf
    _write(tmp_path / "nan.mrc", data)
    info = volume3d.prepare(tmp_path, "nan.mrc", 64)
    with mrcfile.open(info["file"]) as m:
        assert np.isfinite(m.data).all() and m.data[2, 2, 2] == 0.0
    assert np.isfinite([info["min"], info["max"], info["mean"], info["std"]]).all()


def test_a_mask_is_recognised_even_after_averaging_blurs_its_edge(tmp_path):
    mask = np.zeros((64, 64, 64), dtype=np.uint16)
    mask[:, :, :21] = 1                                          # edge falls inside a block
    _write(tmp_path / "mask.mrc", mask)
    info = volume3d.prepare(tmp_path, "mask.mrc", 32)
    assert info["binary"] is True and info["factor"] == 2
    with mrcfile.open(info["file"]) as m:
        assert 0.0 < float(m.data[0, 0, 10]) < 1.0               # the averaged copy has fractions
    _write(tmp_path / "density.mrc", np.random.default_rng(1).normal(size=(16, 16, 16)).astype(np.float32))
    assert volume3d.prepare(tmp_path, "density.mrc", 64)["binary"] is False


def test_the_copy_is_cached_and_rebuilt_when_the_file_changes(tmp_path, _private_cache):
    _write(tmp_path / "map.mrc", np.ones((8, 8, 8), dtype=np.float32))
    first = volume3d.prepare(tmp_path, "map.mrc", 64)
    stamp = first["file"].stat().st_mtime_ns
    assert volume3d.prepare(tmp_path, "map.mrc", 64)["file"].stat().st_mtime_ns == stamp
    _write(tmp_path / "map.mrc", np.full((8, 8, 8), 3.0, dtype=np.float32))
    second = volume3d.prepare(tmp_path, "map.mrc", 64)
    assert second["file"] != first["file"] and second["mean"] == 3.0


def test_the_cache_is_bounded(tmp_path, _private_cache, monkeypatch):
    monkeypatch.setattr(volume3d, "MAX_CACHED", 3)
    for i in range(6):
        _write(tmp_path / f"m{i}.mrc", np.full((8, 8, 8), float(i), dtype=np.float32))
        volume3d.prepare(tmp_path, f"m{i}.mrc", 64)
    assert len(list(_private_cache.glob("*.mrc"))) == 3 and len(list(_private_cache.glob("*.json"))) == 3


def test_refusals_are_plain(tmp_path):
    _write(tmp_path / "image.mrc", np.zeros((1, 32, 32), dtype=np.float32))
    with pytest.raises(viz.VizError, match="not a volume"):
        volume3d.prepare(tmp_path, "image.mrc", 64)
    with pytest.raises(viz.VizError, match="not found"):
        volume3d.prepare(tmp_path, "nope.mrc", 64)
    with pytest.raises(viz.VizError, match="outside the project"):
        volume3d.prepare(tmp_path / "proj", "/etc/passwd", 64)
    _write(tmp_path / "ok.mrc", np.zeros((8, 8, 8), dtype=np.float32))
    for bad in (volume3d.MIN_MAX_DIM - 1, volume3d.MAX_MAX_DIM + 1):
        with pytest.raises(viz.VizError, match="max_dim"):
            volume3d.prepare(tmp_path, "ok.mrc", bad)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    project = tmp_path / "project"
    project_manager.init_new_project(project)
    main.run_manager.set_project_dir(project)
    return fastapi_testclient.TestClient(main.app), project


def test_endpoints_serve_the_info_and_the_file(client):
    http, project = client
    data = np.random.default_rng(2).normal(size=(40, 40, 80)).astype(np.float32)
    _write(project / "Refine3D" / "job001" / "run_class001.mrc", data, voxel=1.5)
    q = {"mrc_path": "Refine3D/job001/run_class001.mrc", "max_dim": 40}
    info = http.get("/api/viz/volume3d/info", params=q).json()
    assert info["factor"] == 2 and info["shape"] == [20, 20, 40] and info["viewer_voxel_size"] == 3.0
    assert "file" not in info                                    # no server-side path leaks out
    resp = http.get("/api/viz/volume3d/file", params=q)
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/octet-stream"
    from mrcfile.mrcinterpreter import MrcInterpreter
    import io
    with MrcInterpreter(iostream=io.BytesIO(resp.content), permissive=True) as m:
        assert m.data.shape == (20, 20, 40)


def test_endpoints_refuse_paths_outside_the_project_and_bad_input(client):
    http, project = client
    assert http.get("/api/viz/volume3d/info", params={"mrc_path": "/etc/passwd"}).status_code == 400
    assert http.get("/api/viz/volume3d/file", params={"mrc_path": "../../etc/passwd"}).status_code == 400
    assert http.get("/api/viz/volume3d/info", params={"mrc_path": "missing.mrc"}).status_code == 400
    _write(project / "a.mrc", np.zeros((8, 8, 8), dtype=np.float32))
    assert http.get("/api/viz/volume3d/info", params={"mrc_path": "a.mrc", "max_dim": 5}).status_code == 400
