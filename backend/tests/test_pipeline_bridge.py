"""
Tests for two-way sync with RELION's own pipeline.

RELION-US never writes `default_pipeline.star` itself — it drives RELION's
`relion_pipeliner`, which is what computes the node graph, allocates the job
number and honours the `.relion_lock` mutex. These tests therefore check the
*integration*: that the job.star handed over says what RELION reads, that the
slot RELION gives back is the one used, and that a failure degrades to running
the job anyway rather than losing it.

The stand-in binary is backend/tests/fake_relion_pipeliner.py; see its docstring
for what it does and does not imitate.
"""
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_runner
import pipeline_bridge
import project_manager

FAKE = Path(__file__).resolve().parent / "fake_relion_pipeliner.py"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An empty RELION project with the stub pipeliner on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "relion_pipeliner"
    shim.write_text(f"#!/usr/bin/env bash\nexec {sys.executable} {FAKE} \"$@\"\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "default_pipeline.star").write_text(
        "\ndata_pipeline_general\n\n_rlnPipeLineJobCounter                      7\n")
    project_manager.init_new_project(proj)
    return proj


# --------------------------------------------------------------------------
# job.star — what RELION will read back
# --------------------------------------------------------------------------


def test_booleans_are_written_as_relion_spells_them(tmp_path):
    """`JobOption::getBoolean()` literally tests `value == "Yes"`, so writing
    True/true/1 reads back as *false* — set in the file, off in the job."""
    star = pipeline_bridge.write_job_star(
        tmp_path / "job.star", "relion.class2d",
        {"do_ctf_correction": True, "do_em": False, "ctf_intact_first_peak": "true"},
        {"ctf_intact_first_peak": {"field_type": "boolean"}},
    )
    text = star.read_text()
    assert 'do_ctf_correction "Yes"' in text
    assert 'do_em "No"' in text
    assert 'ctf_intact_first_peak "Yes"' in text


def test_values_are_quoted(tmp_path):
    """RELION's STAR reader is whitespace-separated; an unquoted path with a
    space, or an Additional arguments string, would split into columns."""
    star = pipeline_bridge.write_job_star(
        tmp_path / "job.star", "relion.class2d",
        {"fn_img": "My Data/particles.star", "other_args": "--verb 2 --pad 1"})
    text = star.read_text()
    assert 'fn_img "My Data/particles.star"' in text
    assert 'other_args "--verb 2 --pad 1"' in text


def test_job_star_declares_the_type_label(tmp_path):
    star = pipeline_bridge.write_job_star(tmp_path / "job.star", "relion.refine3d", {})
    assert "_rlnJobTypeLabel" in star.read_text()
    assert "relion.refine3d" in star.read_text()


def test_written_job_star_reads_back_through_our_own_reader(tmp_path):
    """Round trip: what we write is what the app reads when reopening a job."""
    pipeline_bridge.write_job_star(
        tmp_path / "job.star", "relion.class2d",
        {"nr_classes": 50, "do_ctf_correction": True})
    values = project_manager.read_relion_job_options(tmp_path)
    assert values["nr_classes"] == "50"
    assert values["do_ctf_correction"] == "Yes"


# --------------------------------------------------------------------------
# Registering
# --------------------------------------------------------------------------


def test_register_returns_the_slot_relion_allocated(project):
    out = pipeline_bridge.register_job(project, "relion.class2d", {"nr_classes": 4})
    assert out["process_name"] == "Class2D/job007"
    assert out["job_number"] == 7
    assert (project / "Class2D/job007").is_dir()


def test_registering_appears_in_the_pipeline_for_relions_gui(project):
    pipeline_bridge.register_job(project, "relion.class2d", {})
    info = project_manager.read_relion_pipeline(project)
    assert [p["name"] for p in info["processes"]] == ["Class2D/job007"]
    assert info["job_counter"] == 8       # RELION's counter moved on


def test_the_job_star_lands_in_the_job_directory(project):
    """RELION writes job.star into the job's own directory, which is what its
    GUI reads to reopen the job — so reopening in either GUI shows the same
    settings."""
    pipeline_bridge.register_job(project, "relion.class2d", {"nr_classes": 8})
    values = project_manager.read_relion_job_options(project / "Class2D/job007")
    assert values["nr_classes"] == "8"


def test_consecutive_jobs_get_consecutive_numbers(project):
    a = pipeline_bridge.register_job(project, "relion.class2d", {})
    b = pipeline_bridge.register_job(project, "relion.refine3d", {})
    assert (a["job_number"], b["job_number"]) == (7, 8)
    assert b["process_name"] == "Refine3D/job008"


def test_alias_is_passed_through(project):
    pipeline_bridge.register_job(project, "relion.class2d", {}, alias="my_first_2d")
    procs = project_manager.read_relion_pipeline(project)["processes"]
    assert procs[0]["alias"] == "my_first_2d"


def test_unknown_job_type_raises_rather_than_inventing_a_slot(project):
    with pytest.raises(pipeline_bridge.PipelineBridgeError):
        pipeline_bridge.register_job(project, "relion.not_a_real_job", {})


def test_missing_pipeliner_raises_with_an_actionable_message(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert pipeline_bridge.is_available() is False
    with pytest.raises(pipeline_bridge.PipelineBridgeError) as exc:
        pipeline_bridge.register_job(tmp_path, "relion.class2d", {})
    assert "relion_pipeliner" in str(exc.value)


# --------------------------------------------------------------------------
# Completion
# --------------------------------------------------------------------------


def test_completion_moves_the_job_to_succeeded_in_relions_record(project):
    out = pipeline_bridge.register_job(project, "relion.class2d", {})
    # what a relion_ program writes when it ends, because of --pipeline_control
    (project / out["process_name"] / "RELION_JOB_EXIT_SUCCESS").write_text("")
    assert pipeline_bridge.check_job_completion(project) is True
    procs = project_manager.read_relion_pipeline(project)["processes"]
    assert procs[0]["status_label"] == "Succeeded"


def test_a_failed_job_reaches_relion_as_failed(project):
    out = pipeline_bridge.register_job(project, "relion.class2d", {})
    (project / out["process_name"] / "RELION_JOB_EXIT_FAILURE").write_text("")
    pipeline_bridge.check_job_completion(project)
    procs = project_manager.read_relion_pipeline(project)["processes"]
    assert procs[0]["status_label"] == "Failed"


def test_pipeline_control_flag_is_appended_to_relion_commands():
    cmd = pipeline_bridge.pipeline_control_args(
        "`which relion_refine` --o Class2D/job007/ --i a.star", "Class2D/job007")
    assert cmd.endswith("--pipeline_control Class2D/job007/")


def test_pipeline_control_flag_uses_the_python_tools_spelling():
    cmd = pipeline_bridge.pipeline_control_args(
        "relion_python_tomo_import SerialEM --output-directory Tomo/job007/", "Tomo/job007")
    assert "--pipeline-control Tomo/job007/" in cmd
    assert "--pipeline_control" not in cmd


def test_non_relion_commands_are_left_alone():
    """RELION only adds the flag to commands containing "relion_" — a converter
    or a user's own script would just choke on an argument it doesn't know."""
    cmd = "python3 my_own_script.py --o Class2D/job007/"
    assert pipeline_bridge.pipeline_control_args(cmd, "Class2D/job007") == cmd


def test_the_flag_is_not_added_twice():
    once = pipeline_bridge.pipeline_control_args("relion_refine --o x/", "x")
    assert pipeline_bridge.pipeline_control_args(once, "x") == once


# --------------------------------------------------------------------------
# The per-project setting
# --------------------------------------------------------------------------


def test_sync_is_off_until_asked_for(project):
    """Writing another tool's state file is something to opt into, not to
    inherit by opening a folder."""
    assert project_manager.pipeline_sync_setting(project) is False
    assert job_runner.JobRunManager(project).pipeline_sync_enabled(project) is False


def test_setting_persists_per_project(project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    project_manager.init_new_project(other)
    project_manager.set_pipeline_sync(project, True)
    assert project_manager.pipeline_sync_setting(project) is True
    assert project_manager.pipeline_sync_setting(other) is False


def test_enabled_needs_both_the_setting_and_the_binary(project, monkeypatch, tmp_path):
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)
    assert m.pipeline_sync_enabled(project) is True
    monkeypatch.setenv("PATH", str(tmp_path))       # binary gone
    assert m.pipeline_sync_enabled(project) is False


def test_a_read_only_project_still_opens(project, monkeypatch):
    """The setting is a convenience; failing to persist it must not stop the
    project being used."""
    def boom(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr(project_manager.Path, "write_text", boom)
    project_manager.set_pipeline_sync(project, True)      # must not raise


# --------------------------------------------------------------------------
# The runner's use of it
# --------------------------------------------------------------------------


def test_runner_uses_relions_slot_not_its_own_guess(project):
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)
    registered = m._register_in_relion_pipeline(project, "Class2D", {"nr_classes": 3})
    assert registered["process_name"] == "Class2D/job007"


def test_registration_failure_falls_back_instead_of_losing_the_job(project, monkeypatch):
    """A job the user asked to run should still run when the pipeline is
    momentarily unavailable -- it just won't be in RELION's record."""
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)

    def boom(*a, **k):
        raise pipeline_bridge.PipelineBridgeError("pipeline is locked")
    monkeypatch.setattr(pipeline_bridge, "register_job", boom)
    assert m._register_in_relion_pipeline(project, "Class2D", {}) is None
    # ...and the app's own numbering still produces a usable, unused slot
    assert m.prospective_subdir("Class2D", project) == "Class2D/job007"


def test_custom_bridges_are_never_registered(project):
    """The four import bridges are RELION-US's own; RELION has no job type for
    them, and inventing one would put a job in RELION's pipeline that its GUI
    cannot open."""
    m = job_runner.JobRunManager(project)
    assert m._register_in_relion_pipeline(project, "ImodImport", {}) is None


def test_lock_is_visible_so_it_can_be_explained(project):
    assert pipeline_bridge.is_locked(project) is False
    (project / ".relion_lock").mkdir()
    assert pipeline_bridge.is_locked(project) is True
