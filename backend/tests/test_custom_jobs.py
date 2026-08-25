"""
Tests for custom_jobs.py wiring — the parts that are easy to get wrong because
they live between the job definitions, the runner, and the API layer.
"""
import asyncio
import os
import stat
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import custom_jobs
import main
import pipeline_bridge
import project_manager
from custom_jobs import CUSTOM_JOB_DEFINITIONS, CUSTOM_JOB_RUNNERS
from job_runner import JobRunManager

starfile = pytest.importorskip("starfile")
mrcfile = pytest.importorskip("mrcfile")

FAKE_PIPELINER = Path(__file__).resolve().parent / "fake_relion_pipeliner.py"


@pytest.fixture
def synced_project(tmp_path, monkeypatch):
    """An empty RELION project with the stub pipeliner on PATH and two-way
    sync turned on -- see test_pipeline_bridge.py's `project` fixture (same
    shape, duplicated here rather than shared since there's no conftest.py
    in this test suite yet)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "relion_pipeliner"
    shim.write_text(f"#!/usr/bin/env bash\nexec {sys.executable} {FAKE_PIPELINER} \"$@\"\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    proj = tmp_path / "proj"
    proj.mkdir()
    project_manager.init_new_project(proj)
    project_manager.set_pipeline_sync(proj, True)
    return proj


def test_every_custom_job_has_a_runner():
    assert set(CUSTOM_JOB_DEFINITIONS) == set(CUSTOM_JOB_RUNNERS)


@pytest.mark.parametrize("internal_name", sorted(CUSTOM_JOB_DEFINITIONS))
def test_api_serves_default_values_for_custom_jobs(internal_name):
    """Real RELION jobs get `default_values` from build_job_definition(); custom
    jobs had no such key, so the frontend fell back to {} and opened every field
    blank -- a blank numeric parses to NaN and a blank output path resolved to
    the job directory itself."""
    definition = main._custom_job_definition(internal_name)
    defaults = definition["default_values"]
    keys = {o["key"] for o in definition["options"]}
    assert set(defaults) == keys, "every option must have a default"
    for opt in definition["options"]:
        assert defaults[opt["key"]] == opt.get("default", "")


@pytest.mark.parametrize("internal_name", sorted(CUSTOM_JOB_DEFINITIONS))
def test_custom_job_standard_fields_all_exist(internal_name):
    definition = CUSTOM_JOB_DEFINITIONS[internal_name]
    keys = {o["key"] for o in definition["options"]}
    placed = {k for g in definition["standard_groups"] for k in g["fields"]}
    assert placed <= keys


def test_resolve_out_targets_the_job_dir_not_the_project_root(tmp_path):
    job_dir = tmp_path / "DeepETPickerImport" / "job001"
    assert custom_jobs._resolve_out(job_dir, "particles.star", "d.star") == job_dir / "particles.star"
    assert custom_jobs._resolve_out(job_dir, "", "d.star") == job_dir / "d.star"
    # an absolute path is still honoured verbatim
    absolute = tmp_path / "elsewhere.star"
    assert custom_jobs._resolve_out(job_dir, str(absolute), "d.star") == absolute


def test_custom_job_output_lands_in_its_own_job_dir(tmp_path):
    """Outputs used to go to the project root, leaving the tracked job dir
    empty -- so the Outputs tab, Clean and Delete all operated on nothing, and
    successive runs silently overwrote one shared particles.star."""
    project_manager.init_new_project(tmp_path)
    (tmp_path / "picks.coords").write_text("0 10 20 5\n1 30 40 15\n")
    manager = JobRunManager(tmp_path)
    values = {
        "coords_path": "picks.coords", "tomo_name": "TS_01",
        "binning_factor": 1.0, "out_path": "particles.star",
    }

    async def go():
        async def factory(job_dir):
            return await custom_jobs.run_deepetpicker_import(tmp_path, values, job_dir)
        run = await manager.start_custom_job(
            "DeepETPickerImport", "DeepETPicker", factory, field_values=values
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status not in ("pending", "running"):
                break
        return run

    run = asyncio.run(go())
    assert run.status == "completed", run.stderr_lines
    job_dir = Path(run.cwd)
    assert job_dir == tmp_path / "DeepETPickerImport" / "job001"
    assert (job_dir / "particles.star").is_file(), "output not in the job dir"
    assert not (tmp_path / "particles.star").exists(), "output leaked to the project root"


# --------------------------------------------------------------------------
# Manualpick / TomoManualPick -- see manual_pick.py for the STAR output
# these jobs' *picker* writes; run_manual_pick/run_tomo_manual_pick
# themselves don't pick anything, only validate the input and report a
# count (the popup's Picker button drives the actual picking afterward).
# --------------------------------------------------------------------------


def test_run_manual_pick_reports_micrograph_count(tmp_path):
    project_manager.init_new_project(tmp_path)
    df = pd.DataFrame({"rlnMicrographName": ["a.mrc", "b.mrc", "c.mrc"]})
    starfile.write({"micrographs": df}, tmp_path / "mics.star", overwrite=True)
    summary = asyncio.run(
        custom_jobs.run_manual_pick(tmp_path, {"fn_in": "mics.star"}, tmp_path / "job001"))
    assert "Found 3 micrograph(s)" in summary


def test_run_manual_pick_requires_fn_in(tmp_path):
    with pytest.raises(ValueError, match="required"):
        asyncio.run(custom_jobs.run_manual_pick(tmp_path, {}, tmp_path / "job001"))


def test_run_tomo_manual_pick_reports_tomogram_count(tmp_path):
    project_manager.init_new_project(tmp_path)
    vol = np.zeros((5, 6, 7), dtype="float32")
    with mrcfile.new(tmp_path / "TS_01.mrc", overwrite=True) as m:
        m.set_data(vol)
    tomo_df = pd.DataFrame({
        "rlnTomoName": ["TS_01"], "rlnTomoReconstructedTomogram": ["TS_01.mrc"],
    })
    starfile.write({"tomograms": tomo_df}, tmp_path / "tomograms.star", overwrite=True)
    summary = asyncio.run(custom_jobs.run_tomo_manual_pick(
        tmp_path, {"in_tomoset": "tomograms.star"}, tmp_path / "job001"))
    assert "Found 1 tomogram(s)" in summary


def test_run_tomo_manual_pick_requires_in_tomoset(tmp_path):
    with pytest.raises(ValueError, match="required"):
        asyncio.run(custom_jobs.run_tomo_manual_pick(tmp_path, {}, tmp_path / "job001"))


def test_manualpick_job_registers_in_relions_pipeline_and_gets_an_exit_marker(synced_project):
    """End-to-end: start_custom_job registers Manualpick under its real
    relion.manualpick label (job_runner._register_in_relion_pipeline), lands
    in the directory RELION's pipeliner allocated (not this app's own
    guess), and -- since there's no real subprocess for --pipeline_control
    to instrument -- _run_custom writes the RELION_JOB_EXIT_SUCCESS marker
    itself so relion_pipeliner --check_job_completion (and RELION's own GUI)
    sees it as finished rather than stuck "Running" forever."""
    project = synced_project
    (project / "mics.star").write_text(
        "\ndata_micrographs\n\nloop_\n_rlnMicrographName #1\na.mrc\nb.mrc\n"
    )
    manager = JobRunManager(project)
    values = {"fn_in": "mics.star"}

    async def go():
        async def factory(job_dir):
            return await custom_jobs.run_manual_pick(project, values, job_dir)
        run = await manager.start_custom_job("Manualpick", "Manual Picking", factory, field_values=values)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status not in ("pending", "running"):
                break
        return run

    run = asyncio.run(go())
    assert run.status == "completed", run.stderr_lines
    assert run.pipeline_sync_error is None
    job_dir = Path(run.cwd)
    assert job_dir.parent.name == "ManualPick"
    assert (job_dir / "RELION_JOB_EXIT_SUCCESS").is_file()
    pipeline = project_manager.read_relion_pipeline(project)
    assert any(p["name"] == f"ManualPick/{job_dir.name}" for p in pipeline["processes"])
