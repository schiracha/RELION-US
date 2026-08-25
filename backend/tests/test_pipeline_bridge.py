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
import asyncio
import os
import shutil
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
# _rlnJobIsTomo -- RelionJob's own getCommands*Job() dispatches to an
# entirely different validation/command-building branch depending on this
# flag (confirmed against a real 3D Auto-refine (tomo) job: hardcoding it to
# 0 made the pipeliner check the SPA fn_img field, which stays empty for a
# job actually using in_optimisation, and reject the job outright with
# "empty field for input STAR file").
# --------------------------------------------------------------------------


def test_is_tomo_true_when_using_the_optimisation_set_input():
    assert pipeline_bridge._is_tomo_job(
        {"in_optimisation": "Class3D/job027/run_it050_optimisation_set.star"}
    ) is True


def test_is_tomo_true_when_using_direct_entries():
    assert pipeline_bridge._is_tomo_job({"use_direct_entries": True}) is True


def test_is_tomo_false_for_a_plain_spa_job():
    """Neither of RELION's tomo input fields is set -- e.g. a genuine SPA
    Auto-refine using fn_img instead."""
    assert pipeline_bridge._is_tomo_job({"fn_img": "particles.star"}) is False


def test_is_tomo_false_for_an_empty_optimisation_field():
    """A job type that HAS the in_optimisation field (RELION-US shows it
    even outside RELION's own --tomo GUI mode) but where it's simply blank
    must not be misread as tomo."""
    assert pipeline_bridge._is_tomo_job({"in_optimisation": "", "fn_img": "particles.star"}) is False


def test_job_star_declares_is_tomo_for_a_tomo_job(tmp_path):
    star = pipeline_bridge.write_job_star(
        tmp_path / "job.star", "relion.refine3d",
        {"in_optimisation": "Class3D/job027/run_it050_optimisation_set.star"})
    assert "_rlnJobIsTomo                                 1" in star.read_text()


def test_job_star_declares_is_tomo_zero_for_a_spa_job(tmp_path):
    star = pipeline_bridge.write_job_star(
        tmp_path / "job.star", "relion.refine3d", {"fn_img": "particles.star"})
    assert "_rlnJobIsTomo                                 0" in star.read_text()


def test_is_tomo_true_from_field_values():
    """Motioncorr/Ctffind have neither in_optimisation nor use_direct_entries
    -- real RELION has one RelionJob class for either regardless of SPA vs
    tomo input, so their real is_tomo comes from field_values["is_tomo"],
    set by job_runner._register_in_relion_pipeline from WHICH of the two
    menu entries (Motioncorr/TomoMotioncorr, Ctffind/TomoCtffind -- see
    job_catalog.TOMO_VARIANT_OF) the user actually picked, not a value this
    function derives itself. Without the right value, every tomo Motioncorr/
    Ctffind job used to register the wrong output node type in RELION's own
    pipeline (confirmed against real RELION source: corrected_tilt_series.
    star/LABEL_MOCORR_TOMOGRAMS vs corrected_micrographs.star/
    LABEL_MOCORR_MICS)."""
    assert pipeline_bridge._is_tomo_job(
        {"input_star_mics": "TomoImport/job001/tilt_series.star", "is_tomo": True}
    ) is True
    assert pipeline_bridge._is_tomo_job(
        {"input_star_mics": "MotionCorr/job002/movies.star", "is_tomo": False}
    ) is False


# --------------------------------------------------------------------------
# Bootstrapping a brand-new project's default_pipeline.star
#
# Real relion_pipeliner cannot create this file from nothing -- every code
# path in src/apps/pipeliner.cpp does `pipeline.read(); pipeline.write();`
# unconditionally, and PipeLine::read() takes .relion_lock BEFORE finding
# out the file doesn't exist, then REPORT_ERRORs (which exits without
# releasing the lock). Confirmed for real against RELION 5.0.1: enabling
# sync on a project that had never been opened in RELION's native GUI
# orphaned .relion_lock on its very first job, permanently blocking every
# later sync attempt. fake_relion_pipeliner.py deliberately doesn't
# replicate the lock/read/REPORT_ERROR mechanics (see its own docstring),
# so this class of bug can only be exercised against a real install --
# these tests cover only the half this app controls: that the bootstrap
# file gets written, with the right (real-RELION-verified) content, before
# a lower-level fix in fake_relion_pipeliner.py or an integration test
# marked to skip without RELION would be needed for the rest.
# --------------------------------------------------------------------------


def test_ensure_pipeline_bootstrapped_writes_the_general_block_only(tmp_path):
    """MetaDataTable::write (src/metadata_table.cpp:1369-1372) skips a table
    entirely when it has zero rows ("Only write tables that have something
    in them") -- so a fresh pipeline's real on-disk shape is JUST
    pipeline_general, not five tables with four empty `loop_` blocks. A
    first version of this bootstrap wrote all five and crashed
    relion_pipeliner's reader for real (label-parsing skips blank lines
    while still hunting for more labels, so an empty loop's very next line
    -- the following block's `data_` header -- gets consumed as a bogus
    data row); this asserts the fix stays the minimal, real-RELION-verified
    shape rather than regressing to that."""
    pipeline_bridge._ensure_pipeline_bootstrapped(tmp_path)
    text = (tmp_path / "default_pipeline.star").read_text()
    assert "data_pipeline_general" in text
    assert "_rlnPipeLineJobCounter" in text
    for absent in ("data_pipeline_processes", "data_pipeline_nodes",
                   "data_pipeline_input_edges", "data_pipeline_output_edges",
                   "loop_"):
        assert absent not in text


def test_ensure_pipeline_bootstrapped_is_a_noop_once_the_file_exists(tmp_path):
    """Must never clobber a real project's pipeline -- checked first, so a
    project mid-write by an actual RELION process is left alone."""
    star = tmp_path / "default_pipeline.star"
    star.write_text("REAL RELION CONTENT, NOT THIS APP'S TO TOUCH")
    pipeline_bridge._ensure_pipeline_bootstrapped(tmp_path)
    assert star.read_text() == "REAL RELION CONTENT, NOT THIS APP'S TO TOUCH"


def test_register_job_bootstraps_a_missing_pipeline_before_calling_out(tmp_path, monkeypatch):
    """The bootstrap has to happen before the relion_pipeliner subprocess
    runs, not after -- that's the whole point (it's what the subprocess
    would otherwise crash on)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "relion_pipeliner"
    shim.write_text(f"#!/usr/bin/env bash\nexec {sys.executable} {FAKE} \"$@\"\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])

    proj = tmp_path / "proj"
    proj.mkdir()
    project_manager.init_new_project(proj)
    assert not (proj / "default_pipeline.star").exists()

    pipeline_bridge.register_job(proj, "relion.class2d", {})
    assert (proj / "default_pipeline.star").exists()


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


def test_manualpick_registers_under_its_real_relion_label(project):
    """Manualpick is a custom job (backend/custom_jobs.py) now, not a
    JOB_CATALOG subprocess one -- registration has to fall through to
    job_catalog.CUSTOM_JOBS's relion.manualpick label to find it at all."""
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)
    registered = m._register_in_relion_pipeline(project, "Manualpick", {"fn_in": "mics.star"})
    assert registered["process_name"] == "ManualPick/job007"


def test_tomo_manualpick_registers_under_the_same_label_as_automated_picking(project):
    """TomoManualPick shares relion.picktomo with the real, automated
    TomoPickTomograms job (see job_catalog.py's CUSTOM_JOBS docstring) so
    downstream tomo jobs treat either one's output the same way."""
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)
    registered = m._register_in_relion_pipeline(
        project, "TomoManualPick", {"in_tomoset": "tomograms.star"})
    assert registered["process_name"] == "Picks/job007"


def test_tomo_motioncorr_registers_is_tomo_and_shares_dirname_with_spa_sibling(project):
    """TomoMotioncorr and Motioncorr register under the SAME real RELION
    label (relion.motioncorr -- see job_catalog.TOMO_VARIANT_OF) and so land
    in the same MotionCorr/ dirname RELION's own pipeliner would allocate
    for either; only _rlnJobIsTomo in the written job.star differs, set from
    internal_name by _register_in_relion_pipeline."""
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)
    registered = m._register_in_relion_pipeline(
        project, "TomoMotioncorr", {"input_star_mics": "tilt_series.star"})
    assert registered["process_name"] == "MotionCorr/job007"
    job_star = (project / registered["process_name"] / "job.star").read_text()
    assert "_rlnJobIsTomo                                 1" in job_star


def test_spa_motioncorr_registers_is_tomo_zero(project):
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)
    registered = m._register_in_relion_pipeline(
        project, "Motioncorr", {"input_star_mics": "movies.star"})
    assert registered["process_name"] == "MotionCorr/job007"
    job_star = (project / registered["process_name"] / "job.star").read_text()
    assert "_rlnJobIsTomo                                 0" in job_star


def test_plain_custom_bridge_never_even_attempts_registration(project, monkeypatch):
    """Distinct from Manualpick/TomoManualPick above: a custom.* label
    (ImodImport etc) is never tried against relion_pipeliner at all, not
    just expected to fail gracefully -- confirmed here by making
    register_job blow up if it's ever reached."""
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)

    def boom(*a, **k):
        raise AssertionError("register_job should not be called for a custom.* label")
    monkeypatch.setattr(pipeline_bridge, "register_job", boom)
    assert m._register_in_relion_pipeline(project, "ImodImport", {}) is None


def test_registration_failure_raises_for_the_caller_to_handle(project, monkeypatch):
    """_register_in_relion_pipeline no longer swallows this itself -- it
    raises, and start_subprocess_job (tested below) is what falls back to
    this app's own numbering and decides how to surface the reason."""
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)

    def boom(*a, **k):
        raise pipeline_bridge.PipelineBridgeError("pipeline is locked")
    monkeypatch.setattr(pipeline_bridge, "register_job", boom)
    with pytest.raises(pipeline_bridge.PipelineBridgeError):
        m._register_in_relion_pipeline(project, "Class2D", {})
    # ...and the app's own numbering still produces a usable, unused slot
    assert m.prospective_subdir("Class2D", project) == "Class2D/job007"


def test_registration_failure_falls_back_and_surfaces_in_the_run(project, monkeypatch):
    """A job the user asked to run should still run when the pipeline is
    momentarily unavailable -- it just won't be in RELION's record -- and
    the reason should land on the run (and its Errors tab) instead of being
    swallowed, which is what a bare instance attribute nothing ever read
    used to do here."""
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)

    def boom(*a, **k):
        raise pipeline_bridge.PipelineBridgeError("pipeline is locked")
    monkeypatch.setattr(pipeline_bridge, "register_job", boom)

    async def go():
        run = await m.start_subprocess_job("Class2D", "Class2D", "echo hi", subdir="Class2D/job001")
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status in (job_runner.STATUS_COMPLETED, job_runner.STATUS_FAILED):
                break
        return run

    run = asyncio.run(go())
    assert run.job_number is not None   # fell back to the app's own numbering, not lost
    assert run.pipeline_sync_error is not None
    assert "pipeline is locked" in run.pipeline_sync_error
    assert any("pipeline is locked" in line for line in run.stderr_lines)


def test_synced_job_appears_once_not_twice_in_the_command_center(project):
    """list_runs() used to merge RELION's own default_pipeline.star entries
    keyed by run_id, alongside this app's own history keyed by ITS OWN
    (different) run_id -- so once a job was registered with RELION's
    pipeline, the SAME job showed up as two separate Command Center rows: a
    blank "source: relion" placeholder next to this app's own richer entry.
    Confirmed via a live repro (a 10-job synced project produced 20 rows)
    before this was fixed by skipping the placeholder whenever this app
    already has a record for that job number."""
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)

    async def go():
        run = await m.start_subprocess_job("Class2D", "Class2D", "echo hi", subdir="Class2D/job001")
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.status in (job_runner.STATUS_COMPLETED, job_runner.STATUS_FAILED):
                break
        return run

    run = asyncio.run(go())
    rows = m.list_runs(project)
    matching = [r for r in rows if r.get("job_number") == run.job_number]
    assert len(matching) == 1, f"job{run.job_number:03d} appeared {len(matching)} times: {matching}"
    # The app's own (richer: live status, real command) entry wins, not the
    # blank "source: relion" placeholder.
    assert matching[0]["run_id"] == run.run_id
    assert matching[0].get("source") != "relion"


def test_a_job_relion_ran_outside_this_app_still_appears(project):
    """The dedup above must only suppress the RELION-side placeholder when
    this app has its OWN record for that job number -- a job genuinely run
    outside this app entirely (RELION's own GUI, or a project adopted from
    disk) has no such record and must still show up."""
    # Simulate a job RELION's own GUI ran, entirely outside this app: add it
    # straight to the pipeline via the same call RELION's GUI itself drives.
    registered = pipeline_bridge.register_job(project, "relion.class2d", {"nr_classes": 4})
    m = job_runner.JobRunManager(project)
    rows = m.list_runs(project)
    matching = [r for r in rows if r.get("job_number") == registered["job_number"]]
    assert len(matching) == 1
    assert matching[0]["source"] == "relion"
    assert matching[0]["run_id"] == f"relion:job{registered['job_number']:03d}"


def test_a_relion_native_job_gets_an_estimated_timestamp_from_its_job_star(project):
    """RELION's own pipeline file records no timing at all -- the job.star
    register_job() just wrote into the job directory is exactly the kind of
    once-at-start marker estimate_job_timestamps looks for, so a job RELION
    ran outside this app should still show an (estimated) started_at rather
    than a permanent blank."""
    registered = pipeline_bridge.register_job(project, "relion.class2d", {"nr_classes": 4})
    job_dir = Path(project) / registered["process_name"]
    assert (job_dir / "job.star").exists()   # the fixture this test relies on

    m = job_runner.JobRunManager(project)
    rows = m.list_runs(project)
    entry = next(r for r in rows if r["run_id"] == f"relion:job{registered['job_number']:03d}")
    assert entry["source"] == "relion"   # still read-only -- estimating timing doesn't change that
    assert entry["timestamp_estimated"] is True
    assert entry["started_at"] == pytest.approx((job_dir / "job.star").stat().st_mtime)


def test_a_relion_native_job_with_no_directory_gets_no_estimate(project):
    """A pipeline entry whose job directory doesn't exist on disk (moved,
    deleted, or a project opened somewhere the files aren't) has nothing to
    estimate from -- must stay blank, not raise."""
    registered = pipeline_bridge.register_job(project, "relion.class2d", {"nr_classes": 4})
    job_dir = Path(project) / registered["process_name"]
    shutil.rmtree(job_dir)

    m = job_runner.JobRunManager(project)
    rows = m.list_runs(project)
    entry = next(r for r in rows if r["run_id"] == f"relion:job{registered['job_number']:03d}")
    assert entry["exists_on_disk"] is False
    assert entry["timestamp_estimated"] is False
    assert entry["started_at"] is None


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


# --------------------------------------------------------------------------
# Overwrite + sync: gui_mainwindow.cpp's cb_toggle_overwrite_continue reuses
# the SAME pipeline slot rather than adding a new one, so start_subprocess_
# job's overwrite_run_id branch must never call _register_in_relion_pipeline
# (that always allocates a NEW number via --addJobFromStar). It must,
# however, still append --pipeline_control when sync is on, so a re-run's
# relion_ binary writes the exit-status files --check_job_completion reads
# -- this used to be skipped entirely for every Overwrite, which is why an
# overwritten job never updated its status in RELION's own GUI.
# --------------------------------------------------------------------------


def test_overwrite_does_not_register_a_second_pipeline_entry(project):
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)

    async def go():
        first = await m.start_subprocess_job(
            "Class2D", "Class2D", "echo hi", subdir="Class2D/job001")
        for _ in range(200):
            await asyncio.sleep(0.02)
            if first.status in (job_runner.STATUS_COMPLETED, job_runner.STATUS_FAILED):
                break
        second = await m.start_subprocess_job(
            "Class2D", "Class2D", "`which relion_refine` --o Class2D/job007/run",
            subdir="Class2D/job007", overwrite_run_id=first.run_id,
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if second.status in (job_runner.STATUS_COMPLETED, job_runner.STATUS_FAILED):
                break
        return first, second

    first, second = asyncio.run(go())
    # Same run_id / job_number / directory reused, not a second entry.
    assert second.run_id == first.run_id
    assert second.job_number == first.job_number
    assert second.cwd == first.cwd


def test_overwrite_appends_pipeline_control_when_sync_is_on(project):
    project_manager.set_pipeline_sync(project, True)
    m = job_runner.JobRunManager(project)

    async def go():
        first = await m.start_subprocess_job(
            "Class2D", "Class2D", "echo hi", subdir="Class2D/job001")
        for _ in range(200):
            await asyncio.sleep(0.02)
            if first.status in (job_runner.STATUS_COMPLETED, job_runner.STATUS_FAILED):
                break
        authoritative = Path(first.cwd).name
        second = await m.start_subprocess_job(
            "Class2D", "Class2D",
            f"`which relion_refine` --o Class2D/{authoritative}/run",
            subdir=f"Class2D/{authoritative}", overwrite_run_id=first.run_id,
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if second.status in (job_runner.STATUS_COMPLETED, job_runner.STATUS_FAILED):
                break
        return second

    second = asyncio.run(go())
    assert "--pipeline_control Class2D/job007/" in second.command


def test_overwrite_leaves_command_alone_when_sync_is_off(project):
    """Sync stays opt-in: an Overwrite run in a project that hasn't turned
    it on must not gain a --pipeline_control flag."""
    m = job_runner.JobRunManager(project)

    async def go():
        first = await m.start_subprocess_job(
            "Class2D", "Class2D", "echo hi", subdir="Class2D/job001")
        for _ in range(200):
            await asyncio.sleep(0.02)
            if first.status in (job_runner.STATUS_COMPLETED, job_runner.STATUS_FAILED):
                break
        second = await m.start_subprocess_job(
            "Class2D", "Class2D", "`which relion_refine` --o Class2D/job001/run",
            subdir="Class2D/job001", overwrite_run_id=first.run_id,
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if second.status in (job_runner.STATUS_COMPLETED, job_runner.STATUS_FAILED):
                break
        return second

    second = asyncio.run(go())
    assert "--pipeline_control" not in second.command
