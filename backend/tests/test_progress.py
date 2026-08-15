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
        "model_general": pd.DataFrame({
            "rlnCurrentResolution": [1.0 / (20.0 - it)],   # RELION stores 1/Angstrom
            "rlnNrClasses": [nc],
            "rlnReferenceDimensionality": [dim],
            "rlnPixelSize": [1.4],
        }),
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
