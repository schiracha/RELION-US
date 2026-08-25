"""
Tests for opening a project that was built in RELION's own GUI.

The failure this guards against is not an error message: it is RELION-US
restarting its job numbering at job001 in a project already twelve jobs deep,
and drafting an output path into somebody's existing results.

Every schema detail here is verified against RELION's own source:
  * default_pipeline.star   -> PipeLine::write(), src/pipeliner.cpp
  * job.star                -> RelionJob::write(), src/pipeline_jobs.cpp
  * status labels           -> procstatus_type2label, src/pipeline_jobs.h
  * per-job sub-labels      -> `label += ".movies"` etc. (35 sites)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_catalog
import job_runner
import project_manager


PIPELINE_STAR = """
# version 30001

data_pipeline_general

_rlnPipeLineJobCounter                      12


# version 30001

data_pipeline_processes

loop_
_rlnPipeLineProcessName #1
_rlnPipeLineProcessAlias #2
_rlnPipeLineProcessTypeLabel #3
_rlnPipeLineProcessStatusLabel #4
Import/job001/           None            relion.import.movies     Succeeded
MotionCorr/job002/       my_motioncorr   relion.motioncorr.own    Succeeded
CtfFind/job003/          None            relion.ctffind.ctffind4  Succeeded
Class2D/job005/          None            relion.class2d.em        Failed
Refine3D/job011/         None            relion.refine3d          Running


# version 30001

data_pipeline_output_edges

loop_
_rlnPipeLineEdgeProcess #1
_rlnPipeLineEdgeToNode #2
Import/job001/ Import/job001/movies.star
MotionCorr/job002/ MotionCorr/job002/corrected.star
CtfFind/job003/ CtfFind/job003/ctf.star
Class2D/job005/ Class2D/job005/particles.star


# version 30001

data_pipeline_input_edges

loop_
_rlnPipeLineEdgeFromNode #1
_rlnPipeLineEdgeProcess #2
Import/job001/movies.star MotionCorr/job002/
MotionCorr/job002/corrected.star CtfFind/job003/
CtfFind/job003/ctf.star Class2D/job005/
Class2D/job005/particles.star Refine3D/job011/
"""

JOB_STAR = """
# version 30001

data_job

_rlnJobTypeLabel                     relion.class2d.em
_rlnJobIsContinue                             0
_rlnJobIsTomo                                 0


# version 30001

data_joboptions_values

loop_
_rlnJobOptionVariable #1
_rlnJobOptionValue #2
fn_img            Select/job004/particles.star
nr_classes        50
tau_fudge         4
particle_diameter 180
do_ctf_correction Yes
nr_mpi            5
nr_threads        8
"""


@pytest.fixture
def relion_project(tmp_path):
    """A project as RELION's own GUI leaves it: a pipeline file, job
    directories, and no `.relion_us/` marker."""
    (tmp_path / "default_pipeline.star").write_text(PIPELINE_STAR)
    for d in ("Import/job001", "MotionCorr/job002", "Class2D/job005"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "Class2D/job005/job.star").write_text(JOB_STAR)
    return tmp_path


# --------------------------------------------------------------------------
# Reading RELION's pipeline
# --------------------------------------------------------------------------


def test_project_is_recognised_without_our_own_marker(relion_project):
    assert project_manager.is_relion_project(relion_project) is True
    assert not (relion_project / ".relion_us").exists()


def test_reads_job_counter_and_processes(relion_project):
    info = project_manager.read_relion_pipeline(relion_project)
    assert info["job_counter"] == 12
    assert [p["name"] for p in info["processes"]] == [
        "Import/job001", "MotionCorr/job002", "CtfFind/job003",
        "Class2D/job005", "Refine3D/job011",
    ]


def test_alias_none_is_read_as_no_alias(relion_project):
    """RELION writes the literal string "None" when a job has no alias."""
    procs = {p["name"]: p for p in project_manager.read_relion_pipeline(relion_project)["processes"]}
    assert procs["Import/job001"]["alias"] == ""
    assert procs["MotionCorr/job002"]["alias"] == "my_motioncorr"


def test_producers_are_read_from_relions_own_edge_tables(relion_project):
    """RELION's node graph (pipeline_input_edges + pipeline_output_edges),
    chained through the node each edge names, not a directory-path guess."""
    info = project_manager.read_relion_pipeline(relion_project)
    assert info["producers"] == {
        "MotionCorr/job002": ["Import/job001"],
        "CtfFind/job003": ["MotionCorr/job002"],
        "Class2D/job005": ["CtfFind/job003"],
        "Refine3D/job011": ["Class2D/job005"],
    }


def test_missing_pipeline_file_is_not_an_error(tmp_path):
    assert project_manager.read_relion_pipeline(tmp_path) == {
        "job_counter": None, "processes": [], "producers": {}}


def test_corrupt_pipeline_file_does_not_block_opening_the_project(tmp_path):
    (tmp_path / "default_pipeline.star").write_text("this is not a STAR file {{{")
    info = project_manager.read_relion_pipeline(tmp_path)
    assert info["job_counter"] is None and info["processes"] == []


# --------------------------------------------------------------------------
# Job numbering — the data-safety case
# --------------------------------------------------------------------------


def test_numbering_continues_relions_counter(relion_project):
    """The whole point: not job001."""
    m = job_runner.JobRunManager(relion_project)
    assert m._next_job_number(relion_project) == 12


def test_draft_output_directory_does_not_land_on_existing_results(relion_project):
    m = job_runner.JobRunManager(relion_project)
    for internal in ("Import", "Class2D", "Autorefine"):
        subdir = m.prospective_subdir(internal, relion_project)
        assert not (relion_project / subdir).exists(), subdir


def test_existing_directory_is_skipped_even_when_the_pipeline_forgot_it(tmp_path):
    """A job deleted from RELION's pipeline can still have its directory on
    disk. Numbering must step over it rather than write into it."""
    (tmp_path / "default_pipeline.star").write_text(
        "\ndata_pipeline_general\n\n_rlnPipeLineJobCounter 3\n")
    (tmp_path / "Class2D/job003").mkdir(parents=True)
    (tmp_path / "Class2D/job004").mkdir(parents=True)
    m = job_runner.JobRunManager(tmp_path)
    assert m.prospective_subdir("Class2D", tmp_path) == "Class2D/job005"


def test_a_fresh_project_still_starts_at_job001(tmp_path):
    project_manager.init_new_project(tmp_path)
    m = job_runner.JobRunManager(tmp_path)
    assert m.prospective_subdir("Import", tmp_path) == "Import/job001"


# --------------------------------------------------------------------------
# Command Center import
# --------------------------------------------------------------------------


def test_relion_jobs_appear_in_the_command_center(relion_project):
    runs = job_runner.JobRunManager(relion_project).list_runs(relion_project)
    assert [r["job_name"] for r in runs] == [
        "job001", "job002", "job003", "job005", "job011"]
    assert all(r["source"] == "relion" for r in runs)


def test_imported_jobs_are_sorted_by_job_number(relion_project):
    runs = job_runner.JobRunManager(relion_project).list_runs(relion_project)
    assert [r["job_number"] for r in runs] == [1, 2, 3, 5, 11]


def test_relions_own_edges_become_command_center_lineage(relion_project):
    """The producers graph read from default_pipeline.star (previous test)
    must reach the Command Center as the same input_links shape this app's
    own _attach_input_lineage produces for its own runs -- so a project built
    entirely in RELION's GUI (where nothing here ever ran _detect_inputs on
    any job) still gets a real, RELION-computed lineage instead of none."""
    runs = {r["job_name"]: r for r in
            job_runner.JobRunManager(relion_project).list_runs(relion_project)}
    assert runs["job001"].get("input_links", []) == []   # nothing feeds Import
    assert [l["job_name"] for l in runs["job002"]["input_links"]] == ["job001"]
    assert runs["job002"]["input_links"][0]["run_id"] == runs["job001"]["run_id"]
    assert [l["job_name"] for l in runs["job005"]["input_links"]] == ["job003"]
    assert [l["job_name"] for l in runs["job011"]["input_links"]] == ["job005"]


def test_relion_status_labels_map_onto_our_own(relion_project):
    runs = {r["job_name"]: r for r in
            job_runner.JobRunManager(relion_project).list_runs(relion_project)}
    assert runs["job001"]["status"] == "completed"   # Succeeded
    assert runs["job005"]["status"] == "failed"      # Failed
    assert runs["job011"]["status"] == "running"     # Running


def test_sub_labelled_types_resolve_to_the_right_job(relion_project):
    """RELION records "relion.class2d.em"; the catalog holds "relion.class2d"."""
    runs = {r["job_name"]: r for r in
            job_runner.JobRunManager(relion_project).list_runs(relion_project)}
    assert runs["job005"]["internal_name"] == "Class2D"
    assert runs["job005"]["display_name"] == "2D Classification"
    assert runs["job001"]["internal_name"] == "Import"


def test_missing_directories_are_flagged_not_hidden(relion_project):
    runs = {r["job_name"]: r for r in
            job_runner.JobRunManager(relion_project).list_runs(relion_project)}
    assert runs["job005"]["exists_on_disk"] is True
    assert runs["job003"]["exists_on_disk"] is False   # listed, never created here


def test_imported_jobs_without_a_job_star_carry_no_invented_timestamps(relion_project):
    """RELION's pipeline file itself records no timing at all, and a job
    directory's own mtime is not a start time -- only a job that actually
    has a job.star (or another once-at-a-specific-moment marker file, see
    project_manager.estimate_job_timestamps) gets an estimate. A job with
    neither (no directory, or an empty one) stays blank: a blank Started
    column beats a plausible wrong one."""
    runs = job_runner.JobRunManager(relion_project).list_runs(relion_project)
    without_job_star = [r for r in runs if r["job_number"] in (1, 2, 3, 11)]
    assert len(without_job_star) == 4
    assert all(r["started_at"] is None and r["ended_at"] is None for r in without_job_star)
    assert all(r["timestamp_estimated"] is False for r in without_job_star)


def test_imported_job_with_a_job_star_gets_an_estimated_start_time(relion_project):
    """job005 is the one job in this fixture that actually has a job.star
    on disk (see the fixture above) -- exactly the once-at-registration
    marker file estimate_job_timestamps looks for, so it should get an
    estimated started_at rather than a permanent blank."""
    runs = job_runner.JobRunManager(relion_project).list_runs(relion_project)
    job005 = next(r for r in runs if r["job_number"] == 5)
    job_star_mtime = (relion_project / "Class2D/job005/job.star").stat().st_mtime
    assert job005["timestamp_estimated"] is True
    assert job005["started_at"] == pytest.approx(job_star_mtime)


def test_our_own_runs_still_show_alongside(relion_project):
    project_manager.save_history(relion_project, [{
        "run_id": "abc123", "internal_name": "Class3D", "display_name": "3D Classification",
        "command": "relion_refine --o Class3D/job012/", "status": "completed",
        "started_at": 1.0, "job_number": 12, "job_name": "job012",
    }])
    runs = job_runner.JobRunManager(relion_project).list_runs(relion_project)
    assert [r["job_number"] for r in runs] == [1, 2, 3, 5, 11, 12]
    assert runs[-1].get("source") != "relion"


# --------------------------------------------------------------------------
# Reopening an imported job with the settings it ran with
# --------------------------------------------------------------------------


def test_reopening_reads_the_values_from_relions_job_star(relion_project):
    m = job_runner.JobRunManager(relion_project)
    detail = m.relion_run_detail("relion:job005", relion_project)
    assert detail is not None
    values = detail["field_values"]
    assert values["nr_classes"] == "50"
    assert values["fn_img"] == "Select/job004/particles.star"
    # the Running-tab fields RELION saves too
    assert values["nr_mpi"] == "5" and values["nr_threads"] == "8"


def test_reopening_reads_the_real_command_from_note_txt(relion_project):
    """RELION's own pipeline file records no command for a job it ran (same
    gap as timestamps -- see estimate_job_timestamps), but note.txt does:
    RELION appends a "with the following command(s):" block there every
    time a job runs. This is what makes an old RELION-native job's command
    show up ready to read/edit/copy instead of starting blank."""
    (relion_project / "Class2D/job005/note.txt").write_text(
        " ++++ Executing new job on Tue Aug 18 10:59:21 2026\n"
        " ++++ with the following command(s): \n"
        "`which relion_refine` --o Class2D/job005/run --K 50 --iter 25\n"
        " ++++ \n"
    )
    m = job_runner.JobRunManager(relion_project)
    detail = m.relion_run_detail("relion:job005", relion_project)
    assert detail["command"] == "`which relion_refine` --o Class2D/job005/run --K 50 --iter 25"


def test_overwrite_target_resolves_a_relion_native_run(relion_project):
    """The bug this fixes: "Recompute draft" on a reopened RELION-native
    job used to 409 with "Unknown run_id to overwrite" (it only ever
    looked in this app's OWN history, which never has these) instead of
    showing a real draft built from the job's own job.star values. The
    actual Overwrite ACTION stays blocked for these jobs regardless (see
    main.py's _reject_relion_run, checked before start_subprocess_job is
    ever reached) -- this only makes the read-only preview work."""
    m = job_runner.JobRunManager(relion_project)
    subdir = m.overwrite_target_subdir("relion:job005", relion_project)
    assert subdir == "Class2D/job005"


def test_overwrite_target_for_a_relion_native_job_with_no_directory_on_disk(relion_project):
    """job003 is in RELION's pipeline but its directory was never created in
    this fixture (see relion_project above) -- _resolve_overwrite_target
    only checks that a cwd was recorded at all (empty string), not that it
    exists on disk, so this still resolves to the theoretical path rather
    than raising. Harmless for a read-only draft preview; relion_run_detail
    is what actually surfaces "directory missing" to the user."""
    subdir = job_runner.JobRunManager(relion_project).overwrite_target_subdir(
        "relion:job003", relion_project)
    assert subdir == "CtfFind/job003"


def test_job_without_a_job_star_says_so_instead_of_pretending(relion_project):
    detail = job_runner.JobRunManager(relion_project).relion_run_detail(
        "relion:job001", relion_project)
    assert detail["field_values"] == {}
    assert "job.star" in detail["import_note"]


def test_job_whose_directory_is_gone_says_so(relion_project):
    detail = job_runner.JobRunManager(relion_project).relion_run_detail(
        "relion:job003", relion_project)
    assert detail["exists_on_disk"] is False
    assert "no longer on disk" in detail["import_note"]


def test_unknown_run_id_returns_none(relion_project):
    assert job_runner.JobRunManager(relion_project).relion_run_detail(
        "relion:job999", relion_project) is None


# --------------------------------------------------------------------------
# Read-only-ness
# --------------------------------------------------------------------------


def test_imported_run_ids_are_recognisable_as_relions(relion_project):
    assert job_runner.JobRunManager.is_relion_run("relion:job005") is True
    assert job_runner.JobRunManager.is_relion_run("abc123") is False


def test_imported_run_ids_survive_a_url_path_segment(relion_project):
    """An id carrying the job's directory ("relion:Class2D/job005") 404s before
    the route matches, because an encoded "/" is not allowed in a path
    segment. RELION's project-wide job number avoids the problem entirely."""
    runs = job_runner.JobRunManager(relion_project).list_runs(relion_project)
    assert [r["run_id"] for r in runs] == [
        "relion:job001", "relion:job002", "relion:job003",
        "relion:job005", "relion:job011",
    ]


def test_label_lookup_prefers_the_longest_matching_base():
    assert job_catalog.internal_name_for_label("relion.class2d.em") == "Class2D"
    assert job_catalog.internal_name_for_label("relion.import") == "Import"
    assert job_catalog.internal_name_for_label("something.unknown") is None
    assert job_catalog.internal_name_for_label("") is None


def test_pipeline_hint_matches_relions_sub_labelled_types(tmp_path):
    """The exact-match version returned "unknown" for every real project,
    silently disabling the SPA/Tomo auto-switch: RELION records
    "relion.autopick.log", the catalog holds "relion.autopick"."""
    (tmp_path / "default_pipeline.star").write_text("""
data_pipeline_processes

loop_
_rlnPipeLineProcessName #1
_rlnPipeLineProcessTypeLabel #2
AutoPick/job004/   relion.autopick.log
Class2D/job005/    relion.class2d.em
""")
    assert project_manager.detect_pipeline_hint(tmp_path) == "spa"


def test_pipeline_hint_is_unknown_when_only_shared_types_were_run(relion_project):
    """Import / MotionCorr / CtfFind / Class2D / Refine3D are all shared
    between the SPA and tomography pipelines, so there is nothing to infer --
    and the toggle is left wherever the user put it."""
    assert project_manager.detect_pipeline_hint(relion_project) == "unknown"
