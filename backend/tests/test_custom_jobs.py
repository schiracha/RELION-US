"""
Tests for custom_jobs.py wiring — the parts that are easy to get wrong because
they live between the job definitions, the runner, and the API layer.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import custom_jobs
import main
import project_manager
from custom_jobs import CUSTOM_JOB_DEFINITIONS, CUSTOM_JOB_RUNNERS
from job_runner import JobRunManager

starfile = pytest.importorskip("starfile")


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
