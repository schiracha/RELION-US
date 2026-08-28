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


def _write_model_iteration(job, it, *, nc=NC, distributions=None, half=False, fsc_shells=None):
    """Same real-shape convention as test_progress.py's _write_iteration
    (model_general as a plain dict -> STAR list block), trimmed to only what
    read_class_distribution_series actually reads (rlnClassDistribution),
    plus optional model_class_N loop_ blocks (one per class, one row per
    Fourier shell -- verified against MlModel::write(), ml_model.cpp
    ~748-771) for read_class_fsc. fsc_shells, when given, is a list of
    {resolution, fsc, ssnr} triples shared by every class in this fixture."""
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
    if fsc_shells:
        for k in range(nc):
            blocks[f"model_class_{k + 1}"] = pd.DataFrame({
                "rlnSpectralIndex": list(range(len(fsc_shells))),
                "rlnAngstromResolution": [s["resolution"] for s in fsc_shells],
                "rlnGoldStandardFsc": [s["fsc"] + k * 0.01 for s in fsc_shells],
                "rlnSsnrMap": [s["ssnr"] + k * 0.1 for s in fsc_shells],
            })
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


# --------------------------------------------------------------------------
# read_class_fsc
# --------------------------------------------------------------------------

FSC_SHELLS = [
    {"resolution": 20.0, "fsc": 0.99, "ssnr": 50.0},
    {"resolution": 10.0, "fsc": 0.80, "ssnr": 10.0},
    {"resolution": 5.0, "fsc": 0.10, "ssnr": 0.5},
]


def test_class_fsc_no_files_not_available(tmp_path):
    result = analyze.read_class_fsc(tmp_path)
    assert result == {"available": False, "iteration": None, "classes": {}}


def test_class_fsc_reads_last_iteration_by_default(tmp_path):
    _write_model_iteration(tmp_path, 1, nc=2, fsc_shells=FSC_SHELLS)
    _write_model_iteration(tmp_path, 2, nc=2, fsc_shells=FSC_SHELLS)
    result = analyze.read_class_fsc(tmp_path)
    assert result["available"] is True
    assert result["iteration"] == 2
    assert set(result["classes"].keys()) == {1, 2}
    assert result["classes"][1]["resolution"] == [20.0, 10.0, 5.0]
    assert result["classes"][1]["fsc"] == [0.99, 0.80, 0.10]
    assert result["classes"][1]["ssnr"] == [50.0, 10.0, 0.5]
    # class 2's fixture values are offset from class 1's (see _write_model_iteration)
    assert result["classes"][2]["fsc"][0] == pytest.approx(0.99 + 0.01)


def test_class_fsc_reads_a_specific_earlier_iteration(tmp_path):
    _write_model_iteration(tmp_path, 1, nc=1, fsc_shells=FSC_SHELLS)
    _write_model_iteration(tmp_path, 2, nc=1, fsc_shells=[{"resolution": 20.0, "fsc": 0.5, "ssnr": 5.0}])
    result = analyze.read_class_fsc(tmp_path, iteration=1)
    assert result["iteration"] == 1
    assert result["classes"][1]["fsc"] == [0.99, 0.80, 0.10]


def test_class_fsc_unknown_iteration_not_available(tmp_path):
    _write_model_iteration(tmp_path, 1, nc=1, fsc_shells=FSC_SHELLS)
    result = analyze.read_class_fsc(tmp_path, iteration=99)
    assert result == {"available": False, "iteration": None, "classes": {}}


def test_class_fsc_not_available_when_model_class_blocks_are_missing(tmp_path):
    # A model.star with the ordinary model_general/model_classes blocks but
    # no model_class_N sub-blocks at all (fsc_shells=None) -- e.g. a very
    # old RELION version, or a file this app hasn't seen the shape of yet.
    _write_model_iteration(tmp_path, 1, nc=2)
    result = analyze.read_class_fsc(tmp_path)
    assert result == {"available": False, "iteration": 1, "classes": {}}


def test_class_fsc_half_set_only_still_works(tmp_path):
    _write_model_iteration(tmp_path, 1, nc=1, fsc_shells=FSC_SHELLS, half=True)
    result = analyze.read_class_fsc(tmp_path)
    assert result["available"] is True
    assert result["classes"][1]["fsc"] == [0.99, 0.80, 0.10]


# --------------------------------------------------------------------------
# read_particle_scatter_columns
# --------------------------------------------------------------------------


def _write_particles_star(path, n=4):
    starfile.write({
        "optics": pd.DataFrame({
            "rlnOpticsGroup": [1],
            "rlnOpticsGroupName": ["opticsGroup1"],
            "rlnVoltage": [300.0],
        }),
        "particles": pd.DataFrame({
            "rlnMicrographName": [f"mic_{i}.mrc" for i in range(n)],
            "rlnImageName": [f"{i + 1:06d}@Extract/job010/particles.mrcs" for i in range(n)],
            "rlnCoordinateX": [100.0 + i for i in range(n)],
            "rlnCoordinateY": [200.0 + i * 2 for i in range(n)],
            "rlnDefocusU": [12000.0 + i * 100 for i in range(n)],
            "rlnOpticsGroup": [1] * n,
        }),
    }, path, overwrite=True)


def test_particle_scatter_columns_excludes_name_and_non_numeric(tmp_path):
    star = tmp_path / "particles.star"
    _write_particles_star(star)
    result = analyze.read_particle_scatter_columns(tmp_path, "particles.star")
    assert result["available"] is True
    assert "rlnMicrographName" not in result["columns"]
    assert "rlnImageName" not in result["columns"]
    assert set(result["columns"]) == {"rlnCoordinateX", "rlnCoordinateY", "rlnDefocusU", "rlnOpticsGroup"}


def test_particle_scatter_rows_carry_a_row_index(tmp_path):
    star = tmp_path / "particles.star"
    _write_particles_star(star, n=3)
    result = analyze.read_particle_scatter_columns(tmp_path, "particles.star")
    assert [r["_row_index"] for r in result["rows"]] == [0, 1, 2]
    assert result["rows"][1]["rlnCoordinateX"] == 101.0


def test_particle_scatter_missing_file_raises_analyze_error(tmp_path):
    with pytest.raises(analyze.AnalyzeError):
        analyze.read_particle_scatter_columns(tmp_path, "does_not_exist.star")


def test_particle_scatter_path_escaping_project_dir_raises_analyze_error(tmp_path):
    outside = tmp_path.parent / "outside_the_project.star"
    _write_particles_star(outside)
    with pytest.raises(analyze.AnalyzeError):
        analyze.read_particle_scatter_columns(tmp_path, str(outside))


def test_particle_scatter_no_particles_block_not_available(tmp_path):
    star = tmp_path / "empty.star"
    starfile.write({"optics": pd.DataFrame({"rlnOpticsGroup": [1]})}, star, overwrite=True)
    result = analyze.read_particle_scatter_columns(tmp_path, "empty.star")
    assert result == {"available": False, "columns": [], "rows": []}


# --------------------------------------------------------------------------
# export_star_subset -- this repo's first STAR-*writing* code, so every test
# here does a real write-then-reread round trip via starfile.read, not just
# checking the function's own return value.
# --------------------------------------------------------------------------


def test_export_selected_rows_round_trips(tmp_path):
    star = tmp_path / "particles.star"
    _write_particles_star(star, n=6)
    result = analyze.export_star_subset(tmp_path, "particles.star", [0, 2, 4], False, "selected.star")
    assert result["rows"] == 3
    assert result["path"] == "selected.star"

    reread = starfile.read(tmp_path / "selected.star", always_dict=True)
    assert len(reread["particles"]) == 3
    # rows 0/2/4 (0-based) -> rlnCoordinateX 100/102/104 (see _write_particles_star)
    assert sorted(reread["particles"]["rlnCoordinateX"].tolist()) == [100.0, 102.0, 104.0]
    # optics block carried over unchanged
    assert reread["optics"]["rlnVoltage"].iloc[0] == 300.0


def test_export_complement_writes_everything_else(tmp_path):
    star = tmp_path / "particles.star"
    _write_particles_star(star, n=6)
    result = analyze.export_star_subset(tmp_path, "particles.star", [0, 2, 4], True, "rest.star")
    assert result["rows"] == 3
    reread = starfile.read(tmp_path / "rest.star", always_dict=True)
    assert sorted(reread["particles"]["rlnCoordinateX"].tolist()) == [101.0, 103.0, 105.0]


def test_export_appends_star_extension_if_missing(tmp_path):
    star = tmp_path / "particles.star"
    _write_particles_star(star, n=3)
    result = analyze.export_star_subset(tmp_path, "particles.star", [0], False, "no_extension")
    assert result["path"] == "no_extension.star"
    assert (tmp_path / "no_extension.star").exists()


def test_export_refuses_to_overwrite_an_existing_file(tmp_path):
    star = tmp_path / "particles.star"
    _write_particles_star(star, n=3)
    (tmp_path / "taken.star").write_text("# already here\n")
    with pytest.raises(analyze.AnalyzeError):
        analyze.export_star_subset(tmp_path, "particles.star", [0], False, "taken.star")


def test_export_rejects_a_filename_with_a_path_separator(tmp_path):
    star = tmp_path / "particles.star"
    _write_particles_star(star, n=3)
    with pytest.raises(analyze.AnalyzeError):
        analyze.export_star_subset(tmp_path, "particles.star", [0], False, "../escape.star")
    with pytest.raises(analyze.AnalyzeError):
        analyze.export_star_subset(tmp_path, "particles.star", [0], False, "sub/dir.star")


def test_export_source_path_escaping_project_dir_raises(tmp_path):
    outside = tmp_path.parent / "outside2.star"
    _write_particles_star(outside, n=3)
    with pytest.raises(analyze.AnalyzeError):
        analyze.export_star_subset(tmp_path, str(outside), [0], False, "out.star")


def test_export_empty_selection_raises(tmp_path):
    star = tmp_path / "particles.star"
    _write_particles_star(star, n=3)
    # Row indices that don't exist in this file -- intersecting with the
    # real range leaves nothing to export.
    with pytest.raises(analyze.AnalyzeError):
        analyze.export_star_subset(tmp_path, "particles.star", [99, 100], False, "empty.star")


def test_export_writes_into_the_sources_own_directory(tmp_path):
    sub = tmp_path / "Extract" / "job010"
    sub.mkdir(parents=True)
    star = sub / "particles.star"
    _write_particles_star(star, n=4)
    result = analyze.export_star_subset(tmp_path, "Extract/job010/particles.star", [0, 1], False, "out.star")
    assert result["path"] == "Extract/job010/out.star"
    assert (sub / "out.star").exists()
