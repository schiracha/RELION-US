"""
Tests for analyze.py — the Analyze popup's backend (Menu > Tools > Analyze).

Fixtures mimic what RELION itself writes, same discipline as test_progress.py:
run_it###_optimiser.star as a STAR "list" block (verified against
MlOptimiser::write(), src/ml_optimiser.cpp ~1359-1402: MD.setIsList(true)
before writing data_optimiser_general), and run_it###_model.star reusing
test_progress.py's own real-shape convention for model_general/model_classes.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyze

starfile = pytest.importorskip("starfile")

NC = 3


def _write_optimiser(job, it, **cols):
    """A STAR list block (plain dict -> starfile.write emits `_key value`
    pairs, no loop_) -- the real on-disk shape confirmed against
    MlOptimiser::write(). Only the columns passed in are written, so a test
    can exercise "this run's optimiser.star doesn't have column X yet"."""
    starfile.write({"optimiser_general": cols}, job / f"run_it{it:03d}_optimiser.star", overwrite=True)


def _write_model_iteration(job, it, *, nc=NC, distributions=None, half=False):
    """Same real-shape convention as test_progress.py's _write_iteration
    (model_general as a plain dict -> STAR list block), trimmed to only what
    read_class_distribution_series actually reads (rlnClassDistribution)."""
    dist = distributions if distributions is not None else [1.0 / nc] * nc
    blocks = {
        "model_general": {
            "rlnCurrentResolution": 0.05,
            "rlnNrClasses": nc,
            "rlnReferenceDimensionality": 2,
            "rlnPixelSize": 1.4,
        },
        "model_classes": pd.DataFrame({
            "rlnReferenceImage": [f"run_it{it:03d}_class{k + 1:03d}.mrc" for k in range(nc)],
            "rlnClassDistribution": dist,
            "rlnEstimatedResolution": [10.0] * nc,
            "rlnAccuracyRotations": [3.0] * nc,
            "rlnAccuracyTranslationsAngst": [1.1] * nc,
        }),
    }
    names = [f"run_it{it:03d}_half1_model.star", f"run_it{it:03d}_half2_model.star"] if half \
        else [f"run_it{it:03d}_model.star"]
    for name in names:
        starfile.write(blocks, job / name, overwrite=True)


# --------------------------------------------------------------------------
# read_optimiser_series
# --------------------------------------------------------------------------


def test_optimiser_series_no_files_not_available(tmp_path):
    result = analyze.read_optimiser_series(tmp_path)
    assert result == {"available": False, "columns": [], "series": []}


def test_optimiser_series_reads_across_iterations(tmp_path):
    _write_optimiser(tmp_path, 1, rlnChangesOptimalClasses=40.0, rlnChangesOptimalOffsets=2.5,
                      rlnChangesOptimalOrientations=8.0)
    _write_optimiser(tmp_path, 2, rlnChangesOptimalClasses=10.0, rlnChangesOptimalOffsets=1.1,
                      rlnChangesOptimalOrientations=3.0)
    result = analyze.read_optimiser_series(tmp_path)
    assert result["available"] is True
    assert [p["iteration"] for p in result["series"]] == [1, 2]
    assert result["series"][0]["rlnChangesOptimalClasses"] == 40.0
    assert result["series"][1]["rlnChangesOptimalClasses"] == 10.0
    assert set(result["columns"]) == {
        "rlnChangesOptimalClasses", "rlnChangesOptimalOffsets", "rlnChangesOptimalOrientations",
    }


def test_optimiser_series_columns_only_lists_what_is_actually_present(tmp_path):
    # A job type that only ever writes one of the three (or an older/newer
    # RELION build that dropped one) -- the picker must not offer a column
    # that would just come back empty for every iteration.
    _write_optimiser(tmp_path, 1, rlnChangesOptimalClasses=5.0)
    result = analyze.read_optimiser_series(tmp_path)
    assert result["columns"] == ["rlnChangesOptimalClasses"]
    assert result["series"][0]["rlnChangesOptimalOffsets"] is None
    assert result["series"][0]["rlnChangesOptimalOrientations"] is None


def test_optimiser_series_iteration_number_comes_from_the_filename(tmp_path):
    # Not from any in-file column -- same convention progress.py's own
    # _iteration_files/_parse_model_star_cached already use for model.star.
    _write_optimiser(tmp_path, 7, rlnChangesOptimalClasses=1.0)
    result = analyze.read_optimiser_series(tmp_path)
    assert result["series"][0]["iteration"] == 7


# --------------------------------------------------------------------------
# read_class_distribution_series
# --------------------------------------------------------------------------


def test_class_distribution_no_files_not_available(tmp_path):
    result = analyze.read_class_distribution_series(tmp_path)
    assert result == {"available": False, "iterations": [], "classes": {}}


def test_class_distribution_reads_per_class_across_iterations(tmp_path):
    _write_model_iteration(tmp_path, 1, nc=2, distributions=[0.7, 0.3])
    _write_model_iteration(tmp_path, 2, nc=2, distributions=[0.6, 0.4])
    result = analyze.read_class_distribution_series(tmp_path)
    assert result["available"] is True
    assert result["iterations"] == [1, 2]
    assert result["classes"][1] == [0.7, 0.6]   # class index 1 (1-based, model_classes row 0)
    assert result["classes"][2] == [0.3, 0.4]


def test_class_distribution_single_class_refine3d_like(tmp_path):
    _write_model_iteration(tmp_path, 1, nc=1, distributions=[1.0])
    result = analyze.read_class_distribution_series(tmp_path)
    assert result["classes"] == {1: [1.0]}


def test_class_distribution_half_set_only_still_works(tmp_path):
    # Refine3D's convention: only run_it###_half1_model.star (and half2, but
    # progress.py's own _iteration_files deliberately never reads half2) --
    # no plain run_it###_model.star at all. No half1= flag needed here (see
    # this function's own docstring): _iteration_files already handles both
    # shapes uniformly.
    _write_model_iteration(tmp_path, 1, nc=1, distributions=[1.0], half=True)
    assert not (tmp_path / "run_it001_model.star").exists()
    assert (tmp_path / "run_it001_half1_model.star").exists()
    result = analyze.read_class_distribution_series(tmp_path)
    assert result["available"] is True
    assert result["classes"] == {1: [1.0]}


def test_class_distribution_skips_iterations_with_no_classes_block(tmp_path):
    starfile.write({"model_general": {"rlnCurrentResolution": 0.05, "rlnNrClasses": 0,
                                       "rlnReferenceDimensionality": 2, "rlnPixelSize": 1.4}},
                    tmp_path / "run_it001_model.star", overwrite=True)
    _write_model_iteration(tmp_path, 2, nc=2, distributions=[0.5, 0.5])
    result = analyze.read_class_distribution_series(tmp_path)
    assert result["iterations"] == [2]   # iteration 1's empty classes block contributes nothing
