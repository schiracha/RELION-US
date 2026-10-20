"""
converters/centered_to_voxels.py and its use by the DeepETPicker Prepare job:
`deepetpicker prepare` takes voxel coordinates only (it refuses centred
Angstrom ones), so a particles.star from RELION's pick jobs or RELION-US's
picker is converted first, by a visible step at the start of the command.
"""
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
import project_manager
from converters import centered_to_voxels
from deepetpicker_jobs import (
    CONVERTED_COORDS_NAME, build_deepetpicker_command, validate_deepetpicker_fields,
)

mrcfile = pytest.importorskip("mrcfile")
starfile = pytest.importorskip("starfile")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

CENTERED = ["rlnCenteredCoordinateXAngst", "rlnCenteredCoordinateYAngst", "rlnCenteredCoordinateZAngst"]


def _tomogram(path, nx=40, ny=50, nz=30, voxel=10.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with mrcfile.new(path, overwrite=True) as m:
        m.set_data(np.zeros((nz, ny, nx), dtype=np.float32))
        m.voxel_size = voxel


def _project(tmp_path):
    project_manager.init_new_project(tmp_path)
    _tomogram(tmp_path / "Denoise/job1/tomograms/rec_TS_01.mrc")
    starfile.write({"global": pd.DataFrame({
        "rlnTomoName": ["TS_01"],
        "rlnTomoReconstructedTomogram": ["plain.mrc"],
        "rlnTomoReconstructedTomogramDenoised": ["Denoise/job1/tomograms/rec_TS_01.mrc"],
    })}, tmp_path / "tomograms.star", overwrite=True)
    starfile.write({"particles": pd.DataFrame({
        "rlnTomoName": ["TS_01", "TS_01"],
        CENTERED[0]: [0.0, 100.0], CENTERED[1]: [0.0, -50.0], CENTERED[2]: [0.0, 20.0],
    })}, tmp_path / "picks.star", overwrite=True)
    return tmp_path


def _out(path):
    return starfile.read(path, always_dict=True)["particles"]


def test_convert_places_picks_with_the_tomograms_own_size_and_voxel_size(tmp_path):
    p = _project(tmp_path)
    summary = centered_to_voxels.convert(
        p / "picks.star", p / "out.star", tomo_star=p / "tomograms.star",
        tomo_column="rlnTomoReconstructedTomogramDenoised", project_dir=p)
    df = _out(p / "out.star")
    # voxel = N/2 + angstrom/voxel_size, N = (40, 50, 30), 10 A/voxel
    assert df[["rlnCoordinateX", "rlnCoordinateY", "rlnCoordinateZ"]].to_numpy().tolist() == [
        [20.0, 25.0, 15.0], [30.0, 20.0, 17.0]]
    assert summary["tomograms"]["TS_01"]["outside"] == 0


def test_convert_follows_the_voxel_size_of_the_volume_it_is_given(tmp_path):
    """The same centred picks land on different voxels of a 5 A/voxel tomogram."""
    p = _project(tmp_path)
    _tomogram(p / "fine/TS_01.mrc", nx=80, ny=100, nz=60, voxel=5.0)
    centered_to_voxels.convert(p / "picks.star", p / "out.star", tomo_dir=p / "fine", project_dir=p)
    assert _out(p / "out.star")["rlnCoordinateX"].tolist() == [40.0, 60.0]


def test_convert_keeps_the_class_column(tmp_path):
    p = _project(tmp_path)
    df = starfile.read(p / "picks.star", always_dict=True)["particles"]
    df["rlnClassNumber"] = [1, 2]
    starfile.write({"particles": df}, p / "picks.star", overwrite=True)
    centered_to_voxels.convert(p / "picks.star", p / "out.star", tomo_star=p / "tomograms.star",
                               tomo_column="rlnTomoReconstructedTomogramDenoised", project_dir=p)
    assert _out(p / "out.star")["rlnClassNumber"].tolist() == [1, 2]


def test_convert_reports_picks_outside_the_volume(tmp_path):
    p = _project(tmp_path)
    df = starfile.read(p / "picks.star", always_dict=True)["particles"]
    df.loc[1, CENTERED[2]] = 5000.0
    starfile.write({"particles": df}, p / "picks.star", overwrite=True)
    s = centered_to_voxels.convert(p / "picks.star", p / "out.star", tomo_star=p / "tomograms.star",
                                   tomo_column="rlnTomoReconstructedTomogramDenoised", project_dir=p)
    assert s["tomograms"]["TS_01"]["outside"] == 1


def test_a_tomogram_missing_from_the_folder_fails_clearly(tmp_path, capsys):
    p = _project(tmp_path)
    (p / "empty").mkdir()
    code = centered_to_voxels.main(["--star", str(p / "picks.star"), "--tomo_dir", str(p / "empty"),
                                    "--out", str(p / "out.star")])
    assert code == 1 and "No tomogram for 'TS_01'" in capsys.readouterr().err
    assert not (p / "out.star").exists()


def test_giving_both_or_neither_tomogram_source_is_an_error(tmp_path):
    p = _project(tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        centered_to_voxels.convert(p / "picks.star", p / "o.star", project_dir=p)


def test_detects_only_stars_that_need_converting(tmp_path):
    p = _project(tmp_path)
    assert centered_to_voxels.is_centered_angstrom_star(p / "picks.star") is True
    starfile.write({"particles": pd.DataFrame({
        "rlnTomoName": ["TS_01"], "rlnCoordinateX": [1.0], "rlnCoordinateY": [1.0], "rlnCoordinateZ": [1.0]})},
        p / "voxels.star", overwrite=True)
    assert centered_to_voxels.is_centered_angstrom_star(p / "voxels.star") is False
    (p / "junk.star").write_text("not a star file")
    assert centered_to_voxels.is_centered_angstrom_star(p / "junk.star") is False
    assert centered_to_voxels.is_centered_angstrom_star(p / "missing.star") is False


PREPARE = {"tomo_star": "tomograms.star", "tomo_column": "rlnTomoReconstructedTomogramDenoised",
           "coords_star": "picks.star", "pixel_size": "10", "particle_diameter_A": "300"}
SUBDIR = "DeepETPickerPrepare/job001"


def test_prepare_draft_converts_centred_picks_first_and_points_prepare_at_the_result(tmp_path):
    p = _project(tmp_path)
    cmd = build_deepetpicker_command("DeepETPickerPrepare", PREPARE, SUBDIR, p)
    convert_step, prepare_step = cmd.split(" && ")
    assert convert_step.endswith(f"--out {SUBDIR}/{CONVERTED_COORDS_NAME}")
    assert "--star picks.star" in convert_step
    assert "--tomo_star tomograms.star --tomo_column rlnTomoReconstructedTomogramDenoised" in convert_step
    assert prepare_step.startswith("conda run")
    assert f"--coords_star {SUBDIR}/{CONVERTED_COORDS_NAME}" in prepare_step
    assert "picks.star" not in prepare_step


def test_prepare_draft_uses_the_tomogram_folder_when_that_is_what_was_given(tmp_path):
    p = _project(tmp_path)
    fields = {**PREPARE, "tomo_star": "", "tomo_dir": "Denoise/job1/tomograms"}
    convert_step = build_deepetpicker_command("DeepETPickerPrepare", fields, SUBDIR, p).split(" && ")[0]
    assert "--tomo_dir Denoise/job1/tomograms" in convert_step and "--tomo_star" not in convert_step


def test_prepare_draft_is_unchanged_for_voxel_picks_and_without_a_project_dir(tmp_path):
    p = _project(tmp_path)
    starfile.write({"particles": pd.DataFrame({
        "rlnTomoName": ["TS_01"], "rlnCoordinateX": [1.0], "rlnCoordinateY": [1.0], "rlnCoordinateZ": [1.0]})},
        p / "voxels.star", overwrite=True)
    assert " && " not in build_deepetpicker_command(
        "DeepETPickerPrepare", {**PREPARE, "coords_star": "voxels.star"}, SUBDIR, p)
    assert " && " not in build_deepetpicker_command("DeepETPickerPrepare", PREPARE, SUBDIR)


def test_other_deepetpicker_jobs_never_get_a_conversion_step(tmp_path):
    p = _project(tmp_path)
    cmd = build_deepetpicker_command("DeepETPickerPredict", {"train_job": "T/job001", "tomo_star": "tomograms.star",
                                                             "coords_star": "picks.star"}, "P/job001", p)
    assert " && " not in cmd


def test_the_conversion_step_really_runs_and_feeds_the_command(tmp_path):
    """Execute the drafted command's first half exactly as the runner would
    (a shell, from the project root) and check what it leaves for Prepare."""
    p = _project(tmp_path)
    (p / SUBDIR).mkdir(parents=True)
    convert_step = build_deepetpicker_command("DeepETPickerPrepare", PREPARE, SUBDIR, p).split(" && ")[0]
    done = subprocess.run(convert_step, shell=True, cwd=p, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "Converted 2 picks" in done.stdout
    assert _out(p / SUBDIR / CONVERTED_COORDS_NAME)["rlnCoordinateZ"].tolist() == [15.0, 17.0]


def test_a_failing_conversion_stops_the_chain(tmp_path):
    p = _project(tmp_path)
    cmd = build_deepetpicker_command("DeepETPickerPrepare", {**PREPARE, "tomo_star": "nope.star"}, SUBDIR, p)
    marker = p / "ran"
    chain = cmd.replace(" && conda run", f" && touch {marker} && conda run", 1)
    done = subprocess.run(chain, shell=True, cwd=p, capture_output=True, text=True)
    assert done.returncode != 0 and not marker.exists()


def test_rescaling_centred_picks_is_refused(tmp_path):
    p = _project(tmp_path)
    assert not any("scale" in x for x in validate_deepetpicker_fields("DeepETPickerPrepare", PREPARE, p))
    for scale in ("1.667", "10.5/6.3"):
        problems = validate_deepetpicker_fields("DeepETPickerPrepare", {**PREPARE, "coords_scale": scale}, p)
        assert any("scale factor at 1.0" in x for x in problems), scale
    # voxel picks may need a scale factor, as before
    starfile.write({"particles": pd.DataFrame({
        "rlnTomoName": ["TS_01"], "rlnCoordinateX": [1.0], "rlnCoordinateY": [1.0], "rlnCoordinateZ": [1.0]})},
        p / "voxels.star", overwrite=True)
    assert not any("scale" in x for x in validate_deepetpicker_fields(
        "DeepETPickerPrepare", {**PREPARE, "coords_star": "voxels.star", "coords_scale": "1.667"}, p))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    project = _project(tmp_path / "project")
    main.run_manager.set_project_dir(project)
    return TestClient(main.app), project


def test_draft_endpoint_includes_the_conversion_step(client):
    c, project = client
    values = {**PREPARE, "num_cls": "1"}
    resp = c.post("/api/jobs/DeepETPickerPrepare/draft", json={"field_values": values})
    assert resp.status_code == 200, resp.text
    assert " && conda run" in resp.json()["draft_command"]
    assert f"--coords_star {resp.json()['output_subdir'].rstrip('/')}/{CONVERTED_COORDS_NAME}" in resp.json()["draft_command"]


def test_run_refuses_a_rescaled_centred_star_while_the_command_is_still_the_draft(client):
    c, project = client
    values = {**PREPARE, "coords_scale": "1.667"}
    command = c.post("/api/jobs/DeepETPickerPrepare/draft", json={"field_values": values}).json()["draft_command"]
    resp = c.post("/api/runs", json={"internal_name": "DeepETPickerPrepare", "field_values": values,
                                     "command": command})
    assert resp.status_code == 400 and "scale factor at 1.0" in resp.json()["detail"]
