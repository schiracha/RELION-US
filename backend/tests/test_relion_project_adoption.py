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


def test_missing_pipeline_file_is_not_an_error(tmp_path):
    assert project_manager.read_relion_pipeline(tmp_path) == {
        "job_counter": None, "processes": []}


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


def test_imported_jobs_carry_no_invented_timestamps(relion_project):
    """RELION's pipeline records none, and a directory mtime is not a start
    time. A blank Started column beats a plausible wrong one."""
    runs = job_runner.JobRunManager(relion_project).list_runs(relion_project)
    assert all(r["started_at"] is None and r["ended_at"] is None for r in runs)


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
