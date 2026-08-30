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


def test_run_warp_import_writes_particles_for_a_real_shaped_export(tmp_path):
    """run_warp_import had zero integration-level coverage before this test
    (confirmed via grep) despite being wired into the real job runner just
    like DeepETPickerImport above. Uses the real, WarpTools-docs-verified
    column shape (test_warp_bridge.py's REAL_WARPTOOLS_PARTICLE_COLUMNS) --
    rlnMicrographName as the tomogram-identity column, no rlnTomoName --
    to confirm the run_warp_import -> harmonize_particle_star wiring
    (added alongside the rlnMicrographName alternate-column fix) actually
    renames it and writes a valid particles.star end-to-end, not just at
    the unit level."""
    project_manager.init_new_project(tmp_path)
    warp_df = pd.DataFrame({
        "rlnCoordinateX": [443.701994], "rlnCoordinateY": [214.586768],
        "rlnCoordinateZ": [203.739618], "rlnAngleRot": [54.077932],
        "rlnAngleTilt": [-29.726951], "rlnAnglePsi": [92.706063],
        "rlnMicrographName": ["TS_01.tomostar"],
    })
    starfile.write({"particles": warp_df}, tmp_path / "warp_export.star", overwrite=True)
    manager = JobRunManager(tmp_path)
    values = {"warp_star_path": "warp_export.star", "block_name": "", "out_path": "particles.star"}

    async def go():
        async def factory(job_dir):
            return await custom_jobs.run_warp_import(tmp_path, values, job_dir)
        run = await manager.start_custom_job(
            "WarpImport", "Warp/M Import", factory, field_values=values
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status not in ("pending", "running"):
                break
        return run

    run = asyncio.run(go())
    assert run.status == "completed", run.stderr_lines
    job_dir = Path(run.cwd)
    out_path = job_dir / "particles.star"
    assert out_path.is_file(), "output not written -- harmonize wiring likely broken"
    written = starfile.read(out_path)
    assert "rlnTomoName" in written.columns
    assert written.loc[0, "rlnTomoName"] == "TS_01.tomostar"


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
    proc = next(p for p in pipeline["processes"] if p["name"] == f"ManualPick/{job_dir.name}")
    # Not just "the marker file exists" -- RELION's own pipeline must
    # actually have reached "Succeeded", not stayed stuck "Scheduled" (the
    # bug pipeline_bridge.set_process_status exists to fix: relion_pipeliner
    # --check_job_completion only ever promotes a process already marked
    # "Running", which nothing got here without the direct-write fix).
    assert proc["status_label"] == "Succeeded"


def test_stays_running_job_reaches_running_not_completed(synced_project):
    """Manualpick/TomoManualPick (main.py wires stays_running=is_picker) --
    a successful validation coroutine must leave the run at "running", not
    auto-complete it. The real picking happens afterward, out of band, via
    the Picker button; "Done" (set_status) is what actually finishes it."""
    project = synced_project
    (project / "mics.star").write_text(
        "\ndata_micrographs\n\nloop_\n_rlnMicrographName #1\na.mrc\n"
    )
    manager = JobRunManager(project)
    values = {"fn_in": "mics.star"}

    async def go():
        async def factory(job_dir):
            return await custom_jobs.run_manual_pick(project, values, job_dir)
        run = await manager.start_custom_job(
            "Manualpick", "Manual Picking", factory, field_values=values, stays_running=True)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status != "pending":
                break
        return run

    run = asyncio.run(go())
    assert run.status == "running"
    assert run.ended_at is None
    job_dir = Path(run.cwd)
    assert not (job_dir / "RELION_JOB_EXIT_SUCCESS").exists()  # not finished yet
    pipeline = project_manager.read_relion_pipeline(project)
    proc = next(p for p in pipeline["processes"] if p["name"] == f"ManualPick/{job_dir.name}")
    # Marked Running in RELION's own pipeline even though our own coroutine
    # already returned -- see JobRunManager._mark_pipeline_running.
    assert proc["status_label"] == "Running"


def test_done_button_finishes_a_stays_running_job(synced_project):
    """set_status("completed") on a still-"running" picking job is the
    "Done" button: it must do the FULL completion handshake (exit marker +
    --check_job_completion), not just flip run.status locally, or RELION's
    own GUI would stay stuck on "Running" forever."""
    project = synced_project
    (project / "mics.star").write_text(
        "\ndata_micrographs\n\nloop_\n_rlnMicrographName #1\na.mrc\n"
    )
    manager = JobRunManager(project)
    values = {"fn_in": "mics.star"}

    async def go():
        async def factory(job_dir):
            return await custom_jobs.run_manual_pick(project, values, job_dir)
        run = await manager.start_custom_job(
            "Manualpick", "Manual Picking", factory, field_values=values, stays_running=True)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status != "pending":
                break
        assert run.status == "running"
        updated = await manager.set_status(run.run_id, "completed")
        return run, updated

    run, updated = asyncio.run(go())
    assert run.status == "completed"
    assert run.ended_at is not None
    assert updated["status"] == "completed"
    job_dir = Path(run.cwd)
    assert (job_dir / "RELION_JOB_EXIT_SUCCESS").is_file()
    pipeline = project_manager.read_relion_pipeline(project)
    proc = next(p for p in pipeline["processes"] if p["name"] == f"ManualPick/{job_dir.name}")
    assert proc["status_label"] == "Succeeded"


def test_resume_run_returns_to_running_without_touching_picks(synced_project):
    """"Continue" -- job_runner.resume_run -- must be non-destructive: the
    picks already saved stay exactly as they are."""
    project = synced_project
    (project / "mics.star").write_text(
        "\ndata_micrographs\n\nloop_\n_rlnMicrographName #1\na.mrc\n"
    )
    manager = JobRunManager(project)
    values = {"fn_in": "mics.star"}

    async def go():
        async def factory(job_dir):
            return await custom_jobs.run_manual_pick(project, values, job_dir)
        run = await manager.start_custom_job(
            "Manualpick", "Manual Picking", factory, field_values=values, stays_running=True)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status != "pending":
                break
        import manual_pick
        manual_pick.save_spa_picks(project, Path(run.cwd), "a.mrc", [{"x": 1.0, "y": 2.0}])
        await manager.set_status(run.run_id, "completed")
        assert run.status == "completed"
        updated = await manager.resume_run(run.run_id)
        return run, updated

    run, updated = asyncio.run(go())
    assert run.status == "running"
    assert run.ended_at is None
    assert updated["status"] == "running"
    import manual_pick
    assert manual_pick.load_spa_picks(project, Path(run.cwd), "a.mrc") == [{"x": 1.0, "y": 2.0, "class": 1}]
    pipeline = project_manager.read_relion_pipeline(project)
    proc = next(p for p in pipeline["processes"] if p["name"] == f"ManualPick/{Path(run.cwd).name}")
    assert proc["status_label"] == "Running"


def test_resume_run_rejects_a_currently_running_job(synced_project):
    project = synced_project
    (project / "mics.star").write_text(
        "\ndata_micrographs\n\nloop_\n_rlnMicrographName #1\na.mrc\n"
    )
    manager = JobRunManager(project)
    values = {"fn_in": "mics.star"}

    async def go():
        async def factory(job_dir):
            return await custom_jobs.run_manual_pick(project, values, job_dir)
        run = await manager.start_custom_job(
            "Manualpick", "Manual Picking", factory, field_values=values, stays_running=True)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status != "pending":
                break
        assert run.status == "running"
        with pytest.raises(ValueError, match="resume"):
            await manager.resume_run(run.run_id)

    asyncio.run(go())


def test_overwrite_clears_prior_picks_and_resumes_running(synced_project):
    """Overwrite is the destructive counterpart to Continue -- it must
    genuinely clear existing picks (custom_jobs.run_manual_pick calls
    manual_pick.clear_spa_picks at the top), not just reset run.status."""
    project = synced_project
    (project / "mics.star").write_text(
        "\ndata_micrographs\n\nloop_\n_rlnMicrographName #1\na.mrc\n"
    )
    manager = JobRunManager(project)
    values = {"fn_in": "mics.star"}

    async def go():
        async def factory(job_dir):
            return await custom_jobs.run_manual_pick(project, values, job_dir)
        run = await manager.start_custom_job(
            "Manualpick", "Manual Picking", factory, field_values=values, stays_running=True)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status != "pending":
                break
        import manual_pick
        manual_pick.save_spa_picks(project, Path(run.cwd), "a.mrc", [{"x": 1.0, "y": 2.0}])
        await manager.set_status(run.run_id, "completed")

        overwritten = await manager.start_custom_job(
            "Manualpick", "Manual Picking", factory, field_values=values,
            overwrite_run_id=run.run_id, stays_running=True,
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if overwritten.status != "pending":
                break
        return run, overwritten

    run, overwritten = asyncio.run(go())
    assert overwritten.run_id == run.run_id  # same slot
    assert overwritten.status == "running"
    import manual_pick
    assert manual_pick.load_spa_picks(project, Path(overwritten.cwd), "a.mrc") == []


def test_overwrite_works_directly_on_a_still_running_picking_session(synced_project):
    """A picker's "running" status means "picking session open," not
    "compute in progress" -- Overwrite must be usable straight from that
    state without requiring Done first (found via browser verification:
    _resolve_overwrite_target used to reject ANY running run, picker or
    not)."""
    project = synced_project
    (project / "mics.star").write_text(
        "\ndata_micrographs\n\nloop_\n_rlnMicrographName #1\na.mrc\n"
    )
    manager = JobRunManager(project)
    values = {"fn_in": "mics.star"}

    async def go():
        async def factory(job_dir):
            return await custom_jobs.run_manual_pick(project, values, job_dir)
        run = await manager.start_custom_job(
            "Manualpick", "Manual Picking", factory, field_values=values, stays_running=True)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status != "pending":
                break
        assert run.status == "running"  # never touched Done/set_status

        overwritten = await manager.start_custom_job(
            "Manualpick", "Manual Picking", factory, field_values=values,
            overwrite_run_id=run.run_id, stays_running=True,
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if overwritten.status != "pending":
                break
        return run, overwritten

    run, overwritten = asyncio.run(go())
    assert overwritten.run_id == run.run_id
    assert overwritten.status == "running"
