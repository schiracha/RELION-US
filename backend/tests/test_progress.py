"""
Tests for progress.py — the live per-iteration view of RELION's iterative jobs.

Fixtures mimic what RELION itself writes (verified against src/ml_optimiser.cpp
and src/ml_model.cpp): run_it###_model.star plus either a run_it###_classes.mrcs
stack (2D) or run_it###_class###.mrc volumes (3D), with half-set naming for
Refine3D.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import progress

mrcfile = pytest.importorskip("mrcfile")
starfile = pytest.importorskip("starfile")

NC = 3


def _write_iteration(job, it, *, dim=2, half=False, nc=NC):
    if dim == 2:
        stack = np.random.rand(nc, 32, 32).astype(np.float32)
        with mrcfile.new(job / f"run_it{it:03d}_classes.mrcs", overwrite=True) as m:
            m.set_data(stack)
        refs = [f"{k + 1:06d}@run_it{it:03d}_classes.mrcs" for k in range(nc)]
    else:
        for k in range(nc):
            with mrcfile.new(job / f"run_it{it:03d}_class{k + 1:03d}.mrc", overwrite=True) as m:
                m.set_data(np.random.rand(16, 16, 16).astype(np.float32))
        refs = [f"run_it{it:03d}_class{k + 1:03d}.mrc" for k in range(nc)]

    blocks = {
        # RELION writes model_general as a STAR "list" block (single `_key
        # value` pairs, no loop_) -- starfile only emits that shape for a
        # plain dict, not a DataFrame (a DataFrame always becomes a loop_
        # block, and a Series is silently dropped since coerce_dict only
        # recognizes dict/DataFrame values). Matching the real shape here is
        # what catches bugs like _parse_model_star_cached assuming `general`
        # is always a DataFrame -- it isn't, for genuine RELION output.
        "model_general": {
            "rlnCurrentResolution": 1.0 / (20.0 - it),   # RELION stores 1/Angstrom
            "rlnNrClasses": nc,
            "rlnReferenceDimensionality": dim,
            "rlnPixelSize": 1.4,
        },
        "model_classes": pd.DataFrame({
            "rlnReferenceImage": refs,
            "rlnClassDistribution": [1.0 / nc] * nc,
            "rlnEstimatedResolution": [22.0 - it + k for k in range(nc)],
            "rlnAccuracyRotations": [3.0] * nc,
            "rlnAccuracyTranslationsAngst": [1.1] * nc,
        }),
    }
    names = [f"run_it{it:03d}_half1_model.star", f"run_it{it:03d}_half2_model.star"] if half \
        else [f"run_it{it:03d}_model.star"]
    for name in names:
        starfile.write(blocks, job / name, overwrite=True)
    return refs


def test_supported_job_set_is_deliberately_narrow():
    assert progress.supports_progress("Class2D")
    assert progress.supports_progress("Autorefine")
    # nothing to plot for these -- the tab must not appear
    assert not progress.supports_progress("Import")
    assert not progress.supports_progress("Maskcreate")
    assert not progress.supports_progress("ImodImport")


def test_no_iterations_yet_is_not_an_error(tmp_path):
    """The first minute of a run has no model.star yet; that's normal."""
    out = progress.read_progress(tmp_path)
    assert out["available"] is False
    assert out["iterations"] == []


def test_reads_2d_iterations_and_converts_resolution_units(tmp_path):
    for it in (1, 2, 3):
        _write_iteration(tmp_path, it, dim=2)
    out = progress.read_progress(tmp_path)
    assert out["available"] is True
    assert [p["iteration"] for p in out["iterations"]] == [1, 2, 3]
    assert out["dimensionality"] == 2
    assert out["nr_classes"] == NC
    # rlnCurrentResolution is 1/A in the file; we report Angstrom. rel= because
    # starfile round-trips the reciprocal at ~6 significant figures.
    assert out["iterations"][0]["resolution_A"] == pytest.approx(19.0, rel=1e-3)
    assert out["iterations"][2]["resolution_A"] == pytest.approx(17.0, rel=1e-3)
    # best (smallest) per-class resolution for that iteration
    assert out["iterations"][0]["best_class_resolution_A"] == pytest.approx(21.0)
    assert out["latest"]["iteration"] == 3
    assert len(out["latest"]["classes"]) == NC


def test_accuracy_rotation_and_translation_are_summarized_per_iteration(tmp_path):
    """rlnAccuracyRotations/rlnAccuracyTranslationsAngst were already parsed
    per class (zero extra file I/O) but never surfaced at the iteration
    level before -- the Progress tab's zero-extra-cost accuracy chart reads
    these two fields."""
    for it in (1, 2, 3):
        _write_iteration(tmp_path, it, dim=2)
    out = progress.read_progress(tmp_path)
    # _write_iteration's fixture: rlnAccuracyRotations=3.0, rlnAccuracyTranslationsAngst=1.1
    # for every class, every iteration -- mean of an all-equal set is itself.
    for point in out["iterations"]:
        assert point["accuracy_rotation_deg"] == pytest.approx(3.0)
        assert point["accuracy_translation_A"] == pytest.approx(1.1)


def test_half_set_naming_is_not_double_counted(tmp_path):
    """Refine3D writes run_it###_half1_model.star AND _half2_; the two track
    each other, so only half1 is used -- otherwise every iteration appears twice."""
    for it in (1, 2):
        _write_iteration(tmp_path, it, dim=3, half=True)
    out = progress.read_progress(tmp_path)
    assert [p["iteration"] for p in out["iterations"]] == [1, 2]
    assert out["dimensionality"] == 3


def test_thumbnail_from_2d_class_stack(tmp_path):
    refs = _write_iteration(tmp_path, 1, dim=2)
    png = progress.render_class_thumbnail(tmp_path, refs[1])
    assert png[:4] == b"\x89PNG"


def test_thumbnail_from_3d_class_volume(tmp_path):
    refs = _write_iteration(tmp_path, 1, dim=3)
    png = progress.render_class_thumbnail(tmp_path, refs[0])
    assert png[:4] == b"\x89PNG"


def test_thumbnail_is_downsampled(tmp_path):
    """Thumbnails must stay small -- this feature is explicitly memory-bounded."""
    from PIL import Image
    import io as _io

    big = np.random.rand(1, 512, 512).astype(np.float32)
    with mrcfile.new(tmp_path / "run_it001_classes.mrcs", overwrite=True) as m:
        m.set_data(big)
    png = progress.render_class_thumbnail(tmp_path, "000001@run_it001_classes.mrcs")
    img = Image.open(_io.BytesIO(png))
    assert max(img.size) <= progress.THUMBNAIL_MAX_PX


def test_bad_references_raise_progresserror(tmp_path):
    _write_iteration(tmp_path, 1, dim=2)
    with pytest.raises(progress.ProgressError):
        progress.render_class_thumbnail(tmp_path, "")
    with pytest.raises(progress.ProgressError):
        progress.render_class_thumbnail(tmp_path, "000001@does_not_exist.mrcs")
    with pytest.raises(progress.ProgressError, match="outside"):
        # index beyond the end of the stack
        progress.render_class_thumbnail(tmp_path, "000099@run_it001_classes.mrcs")


def test_iteration_cap_keeps_the_most_recent(tmp_path):
    for it in range(1, 8):
        _write_iteration(tmp_path, it, dim=2, nc=1)
    out = progress.read_progress(tmp_path, max_iterations=3)
    assert [p["iteration"] for p in out["iterations"]] == [5, 6, 7]
    assert out["latest"]["iteration"] == 7


# --------------------------------------------------------------------------
# read_iteration -- fetching one PAST iteration's full class breakdown on
# demand, not just whatever was newest when the popup opened or the last
# poll landed (what lets the Progress tab's iteration picker actually work
# for a completed job, which never polls again after it opens).
# --------------------------------------------------------------------------


def test_read_iteration_returns_that_iterations_own_classes(tmp_path):
    for it in (1, 2, 3):
        _write_iteration(tmp_path, it, dim=2)
    first = progress.read_iteration(tmp_path, 1)
    assert first["iteration"] == 1
    assert len(first["classes"]) == NC
    # Distinct from the LATEST iteration's own numbers -- proves this reads
    # iteration 1's file, not just re-serving `latest`.
    latest = progress.read_progress(tmp_path)["latest"]
    assert first["resolution_A"] != latest["resolution_A"]
    assert first["iteration"] != latest["iteration"]


def test_read_iteration_matches_read_progress_latest_for_the_newest_one(tmp_path):
    for it in (1, 2, 3):
        _write_iteration(tmp_path, it, dim=2)
    newest = progress.read_iteration(tmp_path, 3)
    latest = progress.read_progress(tmp_path)["latest"]
    assert newest["iteration"] == latest["iteration"]
    assert newest["resolution_A"] == pytest.approx(latest["resolution_A"])
    assert len(newest["classes"]) == len(latest["classes"])


def test_read_iteration_missing_raises_progresserror(tmp_path):
    _write_iteration(tmp_path, 1, dim=2)
    with pytest.raises(progress.ProgressError, match="99"):
        progress.read_iteration(tmp_path, 99)


def test_read_iteration_half_set_naming_is_resolved(tmp_path):
    """Same half1-only convention as read_progress: a Refine3D iteration is
    requested by its plain number, not by knowing it's stored as
    run_it###_half1_model.star under the hood."""
    _write_iteration(tmp_path, 1, dim=3, half=True)
    out = progress.read_iteration(tmp_path, 1)
    assert out["iteration"] == 1
    assert len(out["classes"]) == NC


# --------------------------------------------------------------------------
# read_orientation_distribution -- on-demand only, reads run_it###_data.star
# (one row per PARTICLE, not per class) instead of model.star.
# --------------------------------------------------------------------------


def _write_data_star(job_dir: Path, it: int, angles: list[tuple[float, float]]):
    """angles: [(rot, tilt), ...] in degrees, RELION's own ranges
    (rot -180..180, tilt 0..180)."""
    particles = pd.DataFrame({
        "rlnAngleRot": [a[0] for a in angles],
        "rlnAngleTilt": [a[1] for a in angles],
        "rlnAnglePsi": [0.0] * len(angles),
    })
    starfile.write({"particles": particles}, job_dir / f"run_it{it:03d}_data.star", overwrite=True)


def test_orientation_distribution_supported_jobs_exclude_class2d():
    assert progress.supports_orientation_distribution("Class3D")
    assert progress.supports_orientation_distribution("Autorefine")
    assert not progress.supports_orientation_distribution("Class2D")
    assert not progress.supports_orientation_distribution("Import")


def test_orientation_distribution_not_yet_available(tmp_path):
    out = progress.read_orientation_distribution(tmp_path)
    assert out["available"] is False


def test_orientation_distribution_bins_angles_into_the_right_cells(tmp_path):
    _write_data_star(tmp_path, 1, [(0.0, 0.0), (0.0, 0.0), (170.0, 175.0)])
    out = progress.read_orientation_distribution(tmp_path, n_rot_bins=36, n_tilt_bins=18)
    assert out["available"] is True
    assert out["iteration"] == 1
    assert out["n_particles"] == 3
    grid = out["counts"]
    assert len(grid) == 18 and len(grid[0]) == 36
    # rot=0 -> bin 18 (middle: (0+180)/360*36=18); tilt=0 -> bin 0.
    assert grid[0][18] == 2
    # rot=170 -> bin int((170+180)/360*36)=35; tilt=175 -> bin int(175/180*18)=17.
    assert grid[17][35] == 1
    assert sum(sum(row) for row in grid) == 3


def test_orientation_distribution_uses_the_highest_numbered_iteration(tmp_path):
    _write_data_star(tmp_path, 1, [(0.0, 0.0)])
    _write_data_star(tmp_path, 5, [(0.0, 0.0), (0.0, 0.0)])
    out = progress.read_orientation_distribution(tmp_path)
    assert out["iteration"] == 5
    assert out["n_particles"] == 2


def test_orientation_distribution_missing_angle_columns_is_not_available(tmp_path):
    """Class2D's data.star has rlnAnglePsi but no rlnAngleRot/rlnAngleTilt --
    must report unavailable rather than crash or fabricate zeros."""
    particles = pd.DataFrame({"rlnAnglePsi": [0.0, 10.0]})
    starfile.write({"particles": particles}, tmp_path / "run_it001_data.star", overwrite=True)
    out = progress.read_orientation_distribution(tmp_path)
    assert out["available"] is False
    assert out["iteration"] == 1
