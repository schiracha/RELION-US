"""
Tests for select_interactive.py -- the browser-based class selector that
replaces the Select job's interactive branch (relion_display --gui).

Fixtures mimic what a real Class2D/Class3D run + a downstream interactive
Select job actually produce (verified against src/pipeline_jobs.cpp
~2938-2995 and src/displayer.cpp, see select_interactive.py's own module
docstring): a Class2D/jobNNN/ directory with run_it025_model.star (or
_optimiser.star pointing at it) + the sibling run_it025_data.star (optics +
particles, rlnClassNumber per particle) + run_it025_classes.mrcs (the 2D
class-average stack).
"""
import asyncio
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import select_interactive

mrcfile = pytest.importorskip("mrcfile")
starfile = pytest.importorskip("starfile")

NC = 3


def _write_class2d_source(project: Path, job_rel="Class2D/job010", it=25, nc=NC, n_particles=10):
    job = project / job_rel
    job.mkdir(parents=True, exist_ok=True)
    prefix = f"run_it{it:03d}"

    stack = np.random.rand(nc, 16, 16).astype(np.float32)
    with mrcfile.new(job / f"{prefix}_classes.mrcs", overwrite=True) as m:
        m.set_data(stack)
    refs = [f"{k + 1:06d}@{prefix}_classes.mrcs" for k in range(nc)]

    model_blocks = {
        "model_general": {
            "rlnCurrentResolution": 1.0 / 10.0,
            "rlnNrClasses": nc,
            "rlnReferenceDimensionality": 2,
            "rlnPixelSize": 1.4,
        },
        "model_classes": pd.DataFrame({
            "rlnReferenceImage": refs,
            "rlnClassDistribution": [1.0 / nc] * nc,
            "rlnEstimatedResolution": [10.0 + k for k in range(nc)],
            "rlnAccuracyRotations": [3.0] * nc,
            "rlnAccuracyTranslationsAngst": [1.1] * nc,
        }),
    }
    model_path = job / f"{prefix}_model.star"
    starfile.write(model_blocks, model_path, overwrite=True)

    optimiser_blocks = {"optimiser_general": {"rlnModelStarFile": f"{job_rel}/{prefix}_model.star"}}
    optimiser_path = job / f"{prefix}_optimiser.star"
    starfile.write(optimiser_blocks, optimiser_path, overwrite=True)

    # particles spread round-robin across classes (1-indexed, matching
    # model_classes' own row-position convention)
    class_numbers = [(i % nc) + 1 for i in range(n_particles)]
    optics_df = pd.DataFrame({
        "rlnOpticsGroup": [1], "rlnOpticsGroupName": ["opticsGroup1"],
        "rlnVoltage": [300.0], "rlnImagePixelSize": [1.4],
    })
    particles_df = pd.DataFrame({
        "rlnImageName": [f"{i + 1:06d}@Extract/job005/particles.mrcs" for i in range(n_particles)],
        "rlnClassNumber": class_numbers,
        "rlnOpticsGroup": [1] * n_particles,
    })
    data_path = job / f"{prefix}_data.star"
    starfile.write({"optics": optics_df, "particles": particles_df}, data_path, overwrite=True)

    return {
        "job_dir": job, "model_path": model_path, "optimiser_path": optimiser_path,
        "data_path": data_path, "refs": refs, "class_numbers": class_numbers,
    }


def _project(tmp_path):
    (tmp_path / ".relion_us").mkdir(exist_ok=True)
    return tmp_path


# --------------------------------------------------------------------------
# resolve_model_star / data_star_path / is_class2d_source
# --------------------------------------------------------------------------


def test_resolve_model_star_via_optimiser_indirection(tmp_path):
    project = _project(tmp_path)
    fx = _write_class2d_source(project)
    resolved = select_interactive.resolve_model_star(project, "Class2D/job010/run_it025_optimiser.star")
    assert resolved == fx["model_path"]


def test_resolve_model_star_via_direct_model_star_backcompat(tmp_path):
    project = _project(tmp_path)
    fx = _write_class2d_source(project)
    resolved = select_interactive.resolve_model_star(project, "Class2D/job010/run_it025_model.star")
    assert resolved == fx["model_path"]


def test_resolve_model_star_missing_raises(tmp_path):
    project = _project(tmp_path)
    with pytest.raises(select_interactive.SelectInteractiveError, match="not found"):
        select_interactive.resolve_model_star(project, "Class2D/job999/run_it025_optimiser.star")


def test_resolve_model_star_empty_fn_model_raises(tmp_path):
    project = _project(tmp_path)
    with pytest.raises(select_interactive.SelectInteractiveError, match="required"):
        select_interactive.resolve_model_star(project, "")


@pytest.mark.parametrize("name,expected", [
    ("run_it025_model.star", "run_it025_data.star"),
    ("run_it025_optimiser.star", "run_it025_data.star"),
    ("run_it025_half1_model.star", "run_it025_data.star"),
    ("run_it025_half2_model.star", "run_it025_data.star"),
])
def test_data_star_path_strips_every_known_suffix(tmp_path, name, expected):
    p = select_interactive.data_star_path(tmp_path / name)
    assert p == tmp_path / expected


def test_data_star_path_unrecognized_name_raises(tmp_path):
    with pytest.raises(select_interactive.SelectInteractiveError, match="not a recognized"):
        select_interactive.data_star_path(tmp_path / "something_else.star")


@pytest.mark.parametrize("fn_model,expected", [
    ("Class2D/job010/run_it025_optimiser.star", True),
    ("Class3D/job012/run_it025_optimiser.star", False),
    ("", False),
])
def test_is_class2d_source(fn_model, expected):
    assert select_interactive.is_class2d_source(fn_model) is expected


# --------------------------------------------------------------------------
# list_classes
# --------------------------------------------------------------------------


def test_list_classes_field_mapping_and_particle_counts(tmp_path):
    project = _project(tmp_path)
    _write_class2d_source(project, nc=3, n_particles=10)
    classes = select_interactive.list_classes(project, "Class2D/job010/run_it025_optimiser.star")
    assert [c["class_number"] for c in classes] == [1, 2, 3]
    assert [c["index"] for c in classes] == [1, 2, 3]
    # round-robin over 10 particles across 3 classes -> class 1 gets 4, 2 and 3 get 3 each
    counts = {c["class_number"]: c["nr_particles"] for c in classes}
    assert counts == {1: 4, 2: 3, 3: 3}
    assert classes[0]["reference"] == "000001@run_it025_classes.mrcs"
    assert classes[0]["distribution"] == pytest.approx(1.0 / 3.0)
    assert classes[0]["resolution_A"] == pytest.approx(10.0)


def test_list_classes_missing_data_star_yields_zero_counts(tmp_path):
    project = _project(tmp_path)
    fx = _write_class2d_source(project)
    fx["data_path"].unlink()
    classes = select_interactive.list_classes(project, "Class2D/job010/run_it025_optimiser.star")
    assert all(c["nr_particles"] == 0 for c in classes)


# --------------------------------------------------------------------------
# thumbnail_source
# --------------------------------------------------------------------------


def test_thumbnail_source_points_at_the_source_jobs_own_directory(tmp_path):
    project = _project(tmp_path)
    fx = _write_class2d_source(project)
    assert select_interactive.thumbnail_source(project, "Class2D/job010/run_it025_optimiser.star") == fx["job_dir"]


def test_thumbnail_renders_via_progress_render_class_thumbnail(tmp_path):
    """Integration check: select_interactive doesn't reimplement image
    rendering, it hands progress.render_class_thumbnail the right
    directory -- confirm that round trip actually produces a real PNG."""
    import progress

    project = _project(tmp_path)
    fx = _write_class2d_source(project)
    source_dir = select_interactive.thumbnail_source(project, "Class2D/job010/run_it025_optimiser.star")
    png = progress.render_class_thumbnail(source_dir, fx["refs"][0])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------
# save_selection
# --------------------------------------------------------------------------


def test_save_selection_filters_particles_and_preserves_optics(tmp_path):
    project = _project(tmp_path)
    _write_class2d_source(project, nc=3, n_particles=10)
    job_dir = project / "Select" / "job020"
    result = select_interactive.save_selection(
        project, job_dir, "Class2D/job010/run_it025_optimiser.star", [1, 3],
    )
    assert result["n_classes_selected"] == 2
    assert result["n_particles"] == 4 + 3  # class 1 has 4, class 3 has 3
    assert result["class_averages_written"] is True

    written = starfile.read(job_dir / "particles.star", always_dict=True)
    assert "optics" in written
    assert list(written["optics"]["rlnOpticsGroupName"]) == ["opticsGroup1"]
    assert set(written["particles"]["rlnClassNumber"].astype(int)) == {1, 3}
    assert len(written["particles"]) == 7

    avgs = starfile.read(job_dir / "class_averages.star", always_dict=True)
    assert len(avgs["model_classes"]) == 2


def test_save_selection_omits_class_averages_for_class3d_source(tmp_path):
    project = _project(tmp_path)
    _write_class2d_source(project, job_rel="Class3D/job012", nc=3, n_particles=9)
    job_dir = project / "Select" / "job021"
    result = select_interactive.save_selection(
        project, job_dir, "Class3D/job012/run_it025_optimiser.star", [2],
    )
    assert result["class_averages_written"] is False
    assert (job_dir / "particles.star").is_file()
    assert not (job_dir / "class_averages.star").is_file()


def test_save_selection_does_not_accumulate_across_saves(tmp_path):
    """Always re-derives from the original _data.star/model.star -- a
    second save with a DIFFERENT selection must fully replace the first,
    never union with it (same no-accumulation guarantee as
    exclude_tilts.save_tilt_series_exclusions)."""
    project = _project(tmp_path)
    _write_class2d_source(project, nc=3, n_particles=9)
    job_dir = project / "Select" / "job022"
    select_interactive.save_selection(project, job_dir, "Class2D/job010/run_it025_optimiser.star", [1, 2, 3])
    select_interactive.save_selection(project, job_dir, "Class2D/job010/run_it025_optimiser.star", [1])

    written = starfile.read(job_dir / "particles.star", always_dict=True)
    assert set(written["particles"]["rlnClassNumber"].astype(int)) == {1}
    avgs = starfile.read(job_dir / "class_averages.star", always_dict=True)
    assert len(avgs["model_classes"]) == 1


def test_save_selection_missing_data_star_raises(tmp_path):
    project = _project(tmp_path)
    fx = _write_class2d_source(project)
    fx["data_path"].unlink()
    with pytest.raises(select_interactive.SelectInteractiveError, match="not found"):
        select_interactive.save_selection(project, project / "Select/job020", "Class2D/job010/run_it025_optimiser.star", [1])


# --------------------------------------------------------------------------
# clear_selection
# --------------------------------------------------------------------------


def test_clear_selection_removes_previously_written_files(tmp_path):
    project = _project(tmp_path)
    _write_class2d_source(project, nc=3, n_particles=6)
    job_dir = project / "Select" / "job023"
    select_interactive.save_selection(project, job_dir, "Class2D/job010/run_it025_optimiser.star", [1])
    assert (job_dir / "particles.star").is_file()
    assert (job_dir / "class_averages.star").is_file()

    removed = select_interactive.clear_selection(job_dir)
    assert removed == 2
    assert not (job_dir / "particles.star").exists()
    assert not (job_dir / "class_averages.star").exists()


def test_clear_selection_on_a_fresh_directory_is_a_noop(tmp_path):
    project = _project(tmp_path)
    job_dir = project / "Select" / "job099"
    job_dir.mkdir(parents=True)
    assert select_interactive.clear_selection(job_dir) == 0


# --------------------------------------------------------------------------
# run_select_interactive (the custom-job runner)
# --------------------------------------------------------------------------


def test_run_select_interactive_requires_fn_model(tmp_path):
    project = _project(tmp_path)
    job_dir = project / "Select" / "job030"
    with pytest.raises(ValueError, match="fn_model"):
        asyncio.run(select_interactive.run_select_interactive(project, {}, job_dir))


def test_run_select_interactive_rejects_fn_mic_only(tmp_path):
    project = _project(tmp_path)
    job_dir = project / "Select" / "job031"
    with pytest.raises(ValueError, match="isn't supported"):
        asyncio.run(select_interactive.run_select_interactive(
            project, {"fn_mic": "CtfFind/job002/micrographs_ctf.star"}, job_dir,
        ))


def test_run_select_interactive_success_reports_class_count_and_clears_prior_selection(tmp_path):
    project = _project(tmp_path)
    _write_class2d_source(project, nc=3, n_particles=6)
    job_dir = project / "Select" / "job032"
    select_interactive.save_selection(project, job_dir, "Class2D/job010/run_it025_optimiser.star", [1])
    assert (job_dir / "particles.star").is_file()

    msg = asyncio.run(select_interactive.run_select_interactive(
        project, {"fn_model": "Class2D/job010/run_it025_optimiser.star"}, job_dir,
    ))
    assert "3" in msg  # 3 classes found
    assert "Cleared" in msg
    assert not (job_dir / "particles.star").exists()
