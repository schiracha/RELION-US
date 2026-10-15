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


def _make_micrograph(tmp_path, name="mic001.mrc"):
    """A plain 2D SPA micrograph -- no Z axis, unlike _make_project's
    tomogram. Manual picking (SPA) needs the viewer to handle these too."""
    (tmp_path / ".relion_us").mkdir(exist_ok=True)
    img = (np.random.rand(50, 40) * 100).astype(np.float32)  # y, x -- 2D
    with mrcfile.new(tmp_path / name, overwrite=True) as m:
        m.set_data(img)
        m.voxel_size = 1.5
    return tmp_path


def test_volume_info_handles_a_2d_micrograph(tmp_path):
    """A 2D image is exposed as a 1-slice volume (nz=1) rather than
    rejected -- SPA manual picking has no Z axis, unlike tomography."""
    _make_micrograph(tmp_path)
    vi = viz.volume_info(tmp_path, "mic001.mrc")
    assert (vi["nx"], vi["ny"], vi["nz"]) == (40, 50, 1)
    assert vi["voxel_size"] == pytest.approx(1.5)


def test_render_slice_handles_a_2d_micrograph(tmp_path):
    _make_micrograph(tmp_path)
    png = viz.render_slice_png(tmp_path, "mic001.mrc", "z", 0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_4d_stack_is_rejected(tmp_path):
    (tmp_path / ".relion_us").mkdir(exist_ok=True)
    stack = (np.random.rand(2, 5, 6, 7) * 100).astype(np.float32)
    with mrcfile.new(tmp_path / "stack.mrcs", overwrite=True) as m:
        m.set_data(stack)
    with pytest.raises(viz.VizError, match="not a 2D image or 3D"):
        viz.volume_info(tmp_path, "stack.mrcs")


def test_path_traversal_blocked(tmp_path):
    _make_project(tmp_path)
    with pytest.raises(viz.VizError, match="outside the project"):
        viz.volume_info(tmp_path, "../../../etc/passwd")


def test_render_slice_respects_contrast_override(tmp_path):
    _make_project(tmp_path)
    # a degenerate lo>hi is corrected, not crashed
    png = viz.render_slice_png(tmp_path, "TS_01.mrc", "z", 0, lo=50.0, hi=50.0)
    assert png[:4] == b"\x89PNG"


# --- regression: tomogram-name matching must not be a bare substring test ---

def test_names_match_rejects_numeric_prefix_collision():
    """TS_1 must NOT match TS_10 -- a bare `in` test silently overlaid one
    tomogram's particles onto another, and TS_1/TS_10/TS_11 naming is normal."""
    assert viz._names_match("TS_1", "TS_1") is True
    assert viz._names_match("TS_1.mrc", "TS_1") is True
    assert viz._names_match("Tomograms/job005/TS_01.mrc", "TS_01") is True
    assert viz._names_match("rec_TS_01", "TS_01") is True      # separator boundary
    assert viz._names_match("TS_1", "TS_10") is False
    assert viz._names_match("TS_10", "TS_1") is False
    assert viz._names_match("TS_01", "TS_010") is False


def test_load_picks_returns_nothing_when_no_tomogram_matches(tmp_path):
    """A no-match used to fall back to the WHOLE DataFrame, drawing every
    tomogram's picks on one tomogram -- indistinguishable from a correct
    overlay."""
    _make_project(tmp_path)
    df = pd.DataFrame({
        "rlnTomoName": ["AAA", "BBB", "CCC"],
        "rlnCoordinateX": [1.0, 2.0, 3.0],
        "rlnCoordinateY": [1.0, 2.0, 3.0],
        "rlnCoordinateZ": [1.0, 2.0, 3.0],
    })
    starfile.write({"particles": df}, tmp_path / "unrelated.star", overwrite=True)
    out = viz.load_picks(tmp_path, "unrelated.star", tomo_name="TS_01.mrc")
    assert out["matched"] is False
    assert out["picks"] == []


def test_slice_honours_lo_without_hi(tmp_path):
    """lo and hi are independent query params; supplying only one used to
    discard it and re-derive both."""
    _make_project(tmp_path)
    dark = viz.render_slice_png(tmp_path, "TS_01.mrc", "z", 5, lo=0.0, hi=1e6)
    bright = viz.render_slice_png(tmp_path, "TS_01.mrc", "z", 5, lo=0.0)
    assert dark != bright, "lo-only request produced the same image as a full-range one"


def test_slice_survives_nan_voxels(tmp_path):
    """NaN voxels are real in cryo-ET; a plain percentile made lo/hi NaN, the
    `hi <= lo` guard silently didn't fire, and the slice rendered all black."""
    (tmp_path / ".relion_us").mkdir()
    vol = (np.random.rand(8, 16, 16) * 100).astype(np.float32)
    vol[0, 0, 0] = np.nan
    with mrcfile.new(tmp_path / "nan.mrc", overwrite=True) as m:
        m.set_data(vol)
        m.voxel_size = 1.0
    png = viz.render_slice_png(tmp_path, "nan.mrc", "z", 0)
    assert png[:4] == b"\x89PNG"
    info = viz.volume_info(tmp_path, "nan.mrc")
    assert info["contrast_hi"] > info["contrast_lo"]
    assert np.isfinite(info["contrast_lo"]) and np.isfinite(info["contrast_hi"])


# --------------------------------------------------------------------------
# Slice transposition — the orthogonal viewer's left-hand (ZY) panel needs Y
# running vertically so it shares the main XY panel's vertical axis. Getting
# this backwards silently mirrors the side view: it still looks like a
# tomogram, just with the picks in the wrong place.
# --------------------------------------------------------------------------


def _png_size(png: bytes) -> tuple[int, int]:
    """Width, height straight out of the PNG IHDR (bytes 16..24)."""
    import struct
    assert png[:4] == b"\x89PNG"
    return struct.unpack(">II", png[16:24])


def test_slice_dimensions_untransposed(tmp_path):
    # volume is 40x50x30 (nx, ny, nz)
    _make_project(tmp_path)
    assert _png_size(viz.render_slice_png(tmp_path, "TS_01.mrc", "z", 15)) == (40, 50)  # x, y
    assert _png_size(viz.render_slice_png(tmp_path, "TS_01.mrc", "y", 25)) == (40, 30)  # x, z
    assert _png_size(viz.render_slice_png(tmp_path, "TS_01.mrc", "x", 20)) == (50, 30)  # y, z


def test_transpose_swaps_width_and_height(tmp_path):
    _make_project(tmp_path)
    # The ZY panel asks for axis 'x' transposed: z across, y down.
    assert _png_size(
        viz.render_slice_png(tmp_path, "TS_01.mrc", "x", 20, transpose=True)
    ) == (30, 50)


def test_transpose_actually_transposes_the_pixels(tmp_path):
    """Not just the shape — a transpose that only relabelled the dimensions
    would pass the size check above while scrambling the image."""
    import numpy as np
    import mrcfile
    from PIL import Image
    import io

    # A volume whose x-slice is an obvious gradient, so transposition is
    # visible rather than inferred.
    data = np.zeros((6, 8, 4), dtype=np.float32)        # z, y, x
    for z in range(6):
        for y in range(8):
            data[z, y, :] = z * 10 + y
    with mrcfile.new(tmp_path / "grad.mrc", overwrite=True) as m:
        m.set_data(data)

    plain = np.array(Image.open(io.BytesIO(
        viz.render_slice_png(tmp_path, "grad.mrc", "x", 2))))
    flipped = np.array(Image.open(io.BytesIO(
        viz.render_slice_png(tmp_path, "grad.mrc", "x", 2, transpose=True))))
    assert plain.shape == (6, 8)      # rows z, cols y
    assert flipped.shape == (8, 6)    # rows y, cols z
    assert np.array_equal(flipped, plain.T)


# --------------------------------------------------------------------------
# Gaussian smoothing -- the old DeepETPicker viewer's "Gaussian filter" blurred
# the whole volume in 3-D (skimage.filters.gaussian); the slice must match that,
# not a 2-D blur of the one slice.
# --------------------------------------------------------------------------


def _decode(png: bytes):
    import io
    from PIL import Image
    return np.array(Image.open(io.BytesIO(png)))


@pytest.mark.parametrize("axis,index", [("z", 0), ("z", 9), ("z", 19), ("y", 29), ("y", 15), ("x", 24), ("x", 0)])
@pytest.mark.parametrize("sigma", [0.5, 1.5, 3.0])
def test_gaussian_slice_equals_filtering_the_whole_volume(axis, index, sigma):
    from scipy import ndimage
    vol = np.random.default_rng(0).normal(size=(20, 30, 25)).astype(np.float32)   # z, y, x
    got = viz._gaussian_slice(vol, axis, index, sigma)
    ref = ndimage.gaussian_filter(vol.astype(np.float64), sigma, mode="nearest", truncate=4.0)
    ref = ref[index] if axis == "z" else ref[:, index] if axis == "y" else ref[:, :, index]
    assert np.abs(got - ref).max() < 1e-4


def test_gaussian_slice_matches_skimage_which_the_old_viewer_used():
    skimage_filters = pytest.importorskip("skimage.filters")
    vol = np.random.default_rng(1).normal(size=(16, 20, 18)).astype(np.float32)
    ref = skimage_filters.gaussian(vol, 1.5)[8]
    assert np.abs(viz._gaussian_slice(vol, "z", 8, 1.5) - ref).max() < 1e-4


def test_sigma_zero_is_the_plain_slice(tmp_path):
    _make_project(tmp_path)
    assert viz.render_slice_png(tmp_path, "TS_01.mrc", "z", 5, sigma=0.0) == \
        viz.render_slice_png(tmp_path, "TS_01.mrc", "z", 5)


def test_smoothing_reduces_noise_and_keeps_the_slice_geometry(tmp_path):
    _make_project(tmp_path)
    plain = _decode(viz.render_slice_png(tmp_path, "TS_01.mrc", "z", 15, lo=0, hi=100))
    smooth = _decode(viz.render_slice_png(tmp_path, "TS_01.mrc", "z", 15, lo=0, hi=100, sigma=2.0))
    assert plain.shape == smooth.shape == (50, 40)
    assert smooth.std() < plain.std() / 2


def test_smoothing_reaches_across_slices(tmp_path):
    """A bright slab one slice away must show up in this slice once blurred in 3-D;
    a 2-D blur of the single slice would leave it untouched."""
    (tmp_path / ".relion_us").mkdir()
    vol = np.zeros((9, 16, 16), dtype=np.float32)
    vol[5] = 100.0
    with mrcfile.new(tmp_path / "slab.mrc", overwrite=True) as m:
        m.set_data(vol)
    flat = _decode(viz.render_slice_png(tmp_path, "slab.mrc", "z", 4, lo=0, hi=100))
    blurred = _decode(viz.render_slice_png(tmp_path, "slab.mrc", "z", 4, lo=0, hi=100, sigma=1.0))
    assert flat.max() == 0 and blurred.min() > 0


def test_smoothing_keeps_nan_voxels_black_and_does_not_spread_them(tmp_path):
    (tmp_path / ".relion_us").mkdir()
    vol = (np.random.default_rng(2).random((8, 16, 16)) * 100).astype(np.float32)
    vol[3, 4, 4] = np.nan
    with mrcfile.new(tmp_path / "nan.mrc", overwrite=True) as m:
        m.set_data(vol)
    out = viz._gaussian_slice(vol, "z", 3, 1.5)
    assert np.isnan(out[4, 4]) and np.isfinite(np.delete(out.ravel(), 4 * 16 + 4)).all()
    assert viz.render_slice_png(tmp_path, "nan.mrc", "z", 3, sigma=1.5)[:4] == b"\x89PNG"


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), viz.MAX_GAUSSIAN_SIGMA + 0.1])
def test_bad_sigma_is_rejected(tmp_path, bad):
    _make_project(tmp_path)
    with pytest.raises(viz.VizError, match="sigma"):
        viz.render_slice_png(tmp_path, "TS_01.mrc", "z", 5, sigma=bad)


def test_slice_endpoint_passes_sigma_and_rejects_bad_values(tmp_path, monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    import main
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    proj = tmp_path / "p"
    proj.mkdir()
    _make_project(proj)
    main.run_manager.set_project_dir(proj)
    c = testclient.TestClient(main.app)
    q = "/api/viz/slice?mrc_path=TS_01.mrc&axis=z&index=5&lo=0&hi=100"
    plain, smooth = c.get(q), c.get(q + "&sigma=2")
    assert plain.status_code == smooth.status_code == 200 and plain.content != smooth.content
    assert c.get(q + "&sigma=99").status_code == 400
