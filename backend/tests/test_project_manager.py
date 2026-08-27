"""
Tests for project_manager.py — the "Change Project" feature (switching the
active RELION project directory at runtime instead of it being hardcoded to
wherever the app happens to be installed) and its history persistence.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project_manager


def test_fresh_dir_is_not_a_project(tmp_path):
    d = tmp_path / "not_a_project"
    d.mkdir()
    assert project_manager.is_relion_project(d) is False


def test_dir_with_real_pipeline_star_is_a_project(tmp_path):
    d = tmp_path / "real_relion_project"
    d.mkdir()
    (d / "default_pipeline.star").write_text("# fake pipeline star for testing\n")
    assert project_manager.is_relion_project(d) is True


def test_nonexistent_path_is_not_a_project(tmp_path):
    assert project_manager.is_relion_project(tmp_path / "does_not_exist") is False


def test_init_new_project_marks_it_as_a_project(tmp_path):
    d = tmp_path / "new_project"
    assert project_manager.is_relion_project(d) is False
    project_manager.init_new_project(d)
    assert d.is_dir()
    assert project_manager.is_relion_project(d) is True


def test_init_new_project_does_not_fabricate_a_pipeline_star(tmp_path):
    """Regression guard: this app must never write RELION's own
    default_pipeline.star itself -- that's exactly the kind of "something
    inserted under the hood" behavior the whole app exists to avoid.
    RELION's own tools create that file correctly on first real job."""
    d = tmp_path / "new_project"
    project_manager.init_new_project(d)
    assert not (d / "default_pipeline.star").exists()


def test_init_is_idempotent(tmp_path):
    d = tmp_path / "proj"
    project_manager.init_new_project(d)
    project_manager.save_history(d, [{"run_id": "abc", "status": "completed"}])
    project_manager.init_new_project(d)  # should not clobber existing history
    assert project_manager.load_history(d) == [{"run_id": "abc", "status": "completed"}]


def test_history_round_trips(tmp_path):
    d = tmp_path / "proj"
    project_manager.init_new_project(d)
    entries = [
        {"run_id": "r1", "display_name": "Import", "status": "completed", "started_at": 1.0},
        {"run_id": "r2", "display_name": "MotionCorr", "status": "running", "started_at": 2.0},
    ]
    project_manager.save_history(d, entries)
    assert project_manager.load_history(d) == entries


def test_load_history_missing_file_returns_empty_list(tmp_path):
    d = tmp_path / "proj_no_history"
    d.mkdir()
    assert project_manager.load_history(d) == []


def test_load_history_corrupt_file_returns_empty_list_not_error(tmp_path):
    d = tmp_path / "proj"
    project_manager.init_new_project(d)
    (d / project_manager.MARKER_DIRNAME / project_manager.HISTORY_FILENAME).write_text("{not json")
    assert project_manager.load_history(d) == []


def test_list_dir_reports_subdirectories_and_project_status(tmp_path):
    d = tmp_path / "parent_dir"
    d.mkdir()
    (d / "sub_a").mkdir()
    (d / "sub_b").mkdir()
    (d / "a_file.txt").write_text("x")
    (d / ".hidden_dir").mkdir()

    listing = project_manager.list_dir(d)
    names = {e["name"] for e in listing["entries"]}
    assert "sub_a" in names and "sub_b" in names
    assert "a_file.txt" in names
    assert ".hidden_dir" not in names  # dotfiles/dirs filtered out of the browser
    assert listing["is_relion_project"] is False
    assert listing["parent"] == str(d.parent)


def test_list_dir_raises_on_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        project_manager.list_dir(tmp_path / "nope")


def test_list_dir_raises_on_file_not_directory(tmp_path):
    f = tmp_path / "a_file.txt"
    f.write_text("x")
    with pytest.raises(NotADirectoryError):
        project_manager.list_dir(f)


# --- create_folder / "Create Folder" button in the Change Project browser --

def test_create_folder_creates_directory(tmp_path):
    target = tmp_path / "brand_new_subdir"
    project_manager.create_folder(target)
    assert target.is_dir()


def test_create_folder_is_idempotent_on_existing_dir(tmp_path):
    target = tmp_path / "already_here"
    target.mkdir()
    project_manager.create_folder(target)  # should not raise
    assert target.is_dir()


def test_create_folder_wraps_permission_error(tmp_path, monkeypatch):
    """The sandbox this runs in executes as root, which bypasses normal
    chmod-based permission checks, so a real permission failure can't be
    reproduced with chmod here -- monkeypatching Path.mkdir to raise
    PermissionError exercises the exact except-branch in create_folder()
    directly instead."""

    def fake_mkdir(self, *args, **kwargs):
        raise PermissionError("[Errno 13] Permission denied")

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    with pytest.raises(project_manager.PermissionDeniedError) as excinfo:
        project_manager.create_folder(tmp_path / "no_access")
    assert str(excinfo.value) == project_manager.PERMISSION_ERROR_MESSAGE


def test_init_new_project_wraps_permission_error(tmp_path, monkeypatch):
    def fake_mkdir(self, *args, **kwargs):
        raise PermissionError("[Errno 13] Permission denied")

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    with pytest.raises(project_manager.PermissionDeniedError) as excinfo:
        project_manager.init_new_project(tmp_path / "no_access_project")
    assert str(excinfo.value) == project_manager.PERMISSION_ERROR_MESSAGE


# --- detect_pipeline_hint / SPA-Tomo-All toggle auto-switch -----------------
# Builds a real default_pipeline.star with `starfile` (same tool the app
# itself uses -- see converters/star_io.py) rather than hand-typing STAR
# text, so these tests exercise the actual parse path.

def _write_fake_pipeline_star(project_dir: Path, type_labels: list[str]) -> None:
    import pandas as pd
    import starfile

    project_dir.mkdir(parents=True, exist_ok=True)
    general = pd.DataFrame({"rlnPipeLineJobCounter": [len(type_labels)]})
    processes = pd.DataFrame(
        {
            "rlnPipeLineProcessName": [f"Job/job{i:03d}/" for i in range(len(type_labels))],
            "rlnPipeLineProcessAlias": ["None"] * len(type_labels),
            "rlnPipeLineProcessTypeLabel": type_labels,
            "rlnPipeLineProcessStatusLabel": ["Succeeded"] * len(type_labels),
        }
    )
    starfile.write(
        {"pipeline_general": general, "pipeline_processes": processes},
        project_dir / project_manager.RELION_PIPELINE_STAR,
        overwrite=True,
    )


def test_detect_pipeline_hint_unknown_for_brand_new_project(tmp_path):
    d = tmp_path / "new_project"
    project_manager.init_new_project(d)  # no default_pipeline.star written
    assert project_manager.detect_pipeline_hint(d) == "unknown"


def test_detect_pipeline_hint_unknown_for_nonexistent_dir(tmp_path):
    assert project_manager.detect_pipeline_hint(tmp_path / "does_not_exist") == "unknown"


def test_detect_pipeline_hint_spa_only(tmp_path):
    d = tmp_path / "spa_project"
    # relion.motioncorr / relion.ctffind are 'shared' (see job_catalog.py),
    # relion.extract is SPA-only -- only the SPA-only label should tip it.
    _write_fake_pipeline_star(d, ["relion.motioncorr", "relion.ctffind", "relion.extract"])
    assert project_manager.detect_pipeline_hint(d) == "spa"


def test_detect_pipeline_hint_tomo_only(tmp_path):
    d = tmp_path / "tomo_project"
    _write_fake_pipeline_star(d, ["relion.importtomo", "relion.motioncorr", "relion.aligntiltseries"])
    assert project_manager.detect_pipeline_hint(d) == "tomo"


def test_detect_pipeline_hint_mixed_when_both_present(tmp_path):
    d = tmp_path / "mixed_project"
    _write_fake_pipeline_star(d, ["relion.extract", "relion.importtomo"])
    assert project_manager.detect_pipeline_hint(d) == "mixed"


def test_detect_pipeline_hint_unknown_when_only_shared_labels_present(tmp_path):
    d = tmp_path / "shared_only_project"
    _write_fake_pipeline_star(d, ["relion.motioncorr", "relion.ctffind", "relion.class2d"])
    assert project_manager.detect_pipeline_hint(d) == "unknown"


def test_detect_pipeline_hint_unknown_on_corrupt_pipeline_star(tmp_path):
    d = tmp_path / "corrupt_project"
    d.mkdir()
    (d / project_manager.RELION_PIPELINE_STAR).write_text("this is not valid STAR\n")
    assert project_manager.detect_pipeline_hint(d) == "unknown"


# --------------------------------------------------------------------------
# estimate_job_timestamps -- neither RELION's own pipeline files nor a run
# this app lost track of are guaranteed to have real timing recorded, so
# this is the best-effort fallback: specific marker files' mtimes, never the
# job directory's own (which changes on any touch at all).
# --------------------------------------------------------------------------

def _touch_at(path, mtime):
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_estimate_timestamps_missing_directory_returns_nothing(tmp_path):
    assert project_manager.estimate_job_timestamps(tmp_path / "nope", "completed") == (None, None)


def test_estimate_start_from_job_star_mtime(tmp_path):
    d = tmp_path / "job001"
    d.mkdir()
    _touch_at(d / "job.star", 1000.0)
    started_at, ended_at = project_manager.estimate_job_timestamps(d, "completed")
    assert started_at == 1000.0
    assert ended_at is None   # no exit marker and no run.out/run.err to fall back to


def test_start_marker_priority_prefers_job_star_over_the_rest(tmp_path):
    d = tmp_path / "job001"
    d.mkdir()
    _touch_at(d / "run.out", 500.0)
    _touch_at(d / "job.star", 1000.0)
    started_at, _ = project_manager.estimate_job_timestamps(d, "completed")
    assert started_at == 1000.0


def test_estimate_end_from_exit_marker_mtime(tmp_path):
    d = tmp_path / "job001"
    d.mkdir()
    _touch_at(d / "job.star", 1000.0)
    _touch_at(d / "RELION_JOB_EXIT_SUCCESS", 1200.0)
    started_at, ended_at = project_manager.estimate_job_timestamps(d, "completed")
    assert started_at == 1000.0
    assert ended_at == 1200.0


def test_estimate_end_falls_back_to_run_out_err_without_an_exit_marker(tmp_path):
    d = tmp_path / "job001"
    d.mkdir()
    _touch_at(d / "job.star", 1000.0)
    _touch_at(d / "run.out", 1100.0)
    _touch_at(d / "run.err", 1150.0)   # the later of the two wins
    _, ended_at = project_manager.estimate_job_timestamps(d, "failed")
    assert ended_at == 1150.0


def test_no_end_estimate_for_a_still_running_job(tmp_path):
    """There is no sensible "end" for a job that hasn't ended -- even if an
    exit marker somehow exists (e.g. a stale one from a prior run in the
    same directory, via Overwrite), a "running" status must not report one."""
    d = tmp_path / "job001"
    d.mkdir()
    _touch_at(d / "job.star", 1000.0)
    _touch_at(d / "RELION_JOB_EXIT_SUCCESS", 1200.0)
    started_at, ended_at = project_manager.estimate_job_timestamps(d, "running")
    assert started_at == 1000.0
    assert ended_at is None


# --------------------------------------------------------------------------
# read_relion_last_command -- the one place a RELION-native job's real
# command survives at all (RELION's own pipeline file records none, and
# neither does job.star), letting an old job's command be shown, edited and
# copy-pasted here instead of starting blank.
# --------------------------------------------------------------------------


def test_read_last_command_extracts_the_verbatim_command(tmp_path):
    d = tmp_path / "job001"
    d.mkdir()
    (d / "note.txt").write_text(
        "\n ++++ Executing new job on Tue Aug 18 10:59:21 2026\n"
        " ++++ with the following command(s): \n"
        "`which relion_refine` --o Class3D/job027/run --ini_high 40 --iter 50\n"
        " ++++ \n"
    )
    assert project_manager.read_relion_last_command(d) == (
        "`which relion_refine` --o Class3D/job027/run --ini_high 40 --iter 50"
    )


def test_read_last_command_uses_the_most_recent_of_several_runs(tmp_path):
    """note.txt is append-only (RELION opens it with std::ofstream::app) --
    a job overwritten more than once has one block per run, and the LAST
    one is what actually produced the job's current output."""
    d = tmp_path / "job001"
    d.mkdir()
    (d / "note.txt").write_text(
        " ++++ Executing new job on Mon Aug 17 07:00:00 2026\n"
        " ++++ with the following command(s): \n"
        "relion_refine --o Class3D/job001/run --iter 25\n"
        " ++++ \n"
        " ++++ Executing new job on Tue Aug 18 10:59:21 2026\n"
        " ++++ with the following command(s): \n"
        "relion_refine --o Class3D/job001/run --iter 50\n"
        " ++++ \n"
    )
    assert "--iter 50" in project_manager.read_relion_last_command(d)


def test_read_last_command_captures_every_line_of_a_multi_command_job(tmp_path):
    """pipeliner.cpp writes one line per entry in the job's `commands`
    vector, and several real job types push more than one -- e.g. Inimodel
    (relion_refine, then a separate relion_align_symmetry that's the step
    which actually writes initial_model.mrc, the job's real deliverable).
    Confirmed against a real multi-command InitialModel job.star from a
    genuine RELION 5.0.1 project: the single-line-only version of this regex
    silently dropped everything after the first line."""
    d = tmp_path / "job021"
    d.mkdir()
    (d / "note.txt").write_text(
        " ++++ Executing new job on Wed Aug 12 13:30:46 2026\n"
        " ++++ with the following command(s): \n"
        "`which relion_refine` --o InitialModel/job021/run --iter 100\n"
        "rm -f InitialModel/job021/RELION_JOB_EXIT_SUCCESS\n"
        "`which relion_align_symmetry` --i InitialModel/job021/run_it100_model.star"
        " --o InitialModel/job021/initial_model.mrc --sym C1\n"
        "touch InitialModel/job021/RELION_JOB_EXIT_SUCCESS\n"
        " ++++ \n"
    )
    cmd = project_manager.read_relion_last_command(d)
    assert "relion_refine" in cmd
    assert "relion_align_symmetry" in cmd
    assert "initial_model.mrc" in cmd
    assert cmd.count("\n") == 3  # all 4 lines survived


def test_read_last_command_multi_line_still_uses_the_most_recent_run(tmp_path):
    """A multi-line block must not bleed into the next one -- the capture
    has to stop at ITS OWN closing "++++" marker, not the file's last one."""
    d = tmp_path / "job021"
    d.mkdir()
    (d / "note.txt").write_text(
        " ++++ Executing new job on Wed Aug 12 13:00:00 2026\n"
        " ++++ with the following command(s): \n"
        "relion_refine --o InitialModel/job021/run --iter 10\n"
        "relion_align_symmetry --o InitialModel/job021/initial_model.mrc\n"
        " ++++ \n"
        " ++++ Executing new job on Wed Aug 12 14:00:00 2026\n"
        " ++++ with the following command(s): \n"
        "relion_refine --o InitialModel/job021/run --iter 100\n"
        "relion_align_symmetry --o InitialModel/job021/initial_model.mrc --sym C1\n"
        " ++++ \n"
    )
    cmd = project_manager.read_relion_last_command(d)
    assert "--iter 10\n" not in cmd  # the FIRST block's line, not "--iter 100"'s prefix
    assert "--iter 100" in cmd
    assert cmd.count("relion_align_symmetry") == 1
    assert "--sym C1" in cmd


def test_read_last_command_missing_note_file_returns_empty(tmp_path):
    assert project_manager.read_relion_last_command(tmp_path / "job001") == ""


def test_read_last_command_unrecognized_format_returns_empty(tmp_path):
    d = tmp_path / "job001"
    d.mkdir()
    (d / "note.txt").write_text("just a free-text note, no command block here\n")
    assert project_manager.read_relion_last_command(d) == ""


# --------------------------------------------------------------------------
# read_relion_job_is_tomo -- disambiguates a REAL RELION-native job whose
# type label alone can't say SPA vs Tomo (relion.motioncorr/relion.ctffind,
# see job_catalog.TOMO_VARIANT_OF).
# --------------------------------------------------------------------------


def _write_job_star_with_is_tomo(job_dir, is_tomo_value):
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.star").write_text(
        "\n# version 30001\n\ndata_job\n\n"
        "_rlnJobTypeLabel                     relion.motioncorr\n"
        "_rlnJobIsContinue                             0\n"
        f"_rlnJobIsTomo                                 {is_tomo_value}\n\n\n"
    )


def test_read_relion_job_is_tomo_true(tmp_path):
    d = tmp_path / "MotionCorr" / "job001"
    _write_job_star_with_is_tomo(d, 1)
    assert project_manager.read_relion_job_is_tomo(d) is True


def test_read_relion_job_is_tomo_false(tmp_path):
    d = tmp_path / "MotionCorr" / "job002"
    _write_job_star_with_is_tomo(d, 0)
    assert project_manager.read_relion_job_is_tomo(d) is False


def test_read_relion_job_is_tomo_missing_job_star_returns_false(tmp_path):
    assert project_manager.read_relion_job_is_tomo(tmp_path / "job003") is False


def test_read_relion_job_is_tomo_missing_field_returns_false(tmp_path):
    d = tmp_path / "MotionCorr" / "job004"
    d.mkdir(parents=True)
    (d / "job.star").write_text(
        "\ndata_job\n\n_rlnJobTypeLabel                     relion.motioncorr\n"
        "_rlnJobIsContinue                             0\n\n\n"
    )
    assert project_manager.read_relion_job_is_tomo(d) is False


# --------------------------------------------------------------------------
# Recent-projects cache
#
# Every test redirects XDG_CONFIG_HOME into tmp_path -- the cache deliberately
# lives in the user's real config dir, and a test suite must never write there.
# --------------------------------------------------------------------------


@pytest.fixture
def recents_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


def test_recents_empty_when_no_cache_file(recents_home):
    assert project_manager.load_recent_projects() == []


def test_recents_path_follows_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert project_manager.recents_path() == tmp_path / "cfg" / "relion_us" / "recent_projects.json"


def test_remember_then_load_round_trips(recents_home):
    proj = recents_home / "projA"
    proj.mkdir()
    project_manager.remember_project(proj)
    recent = project_manager.load_recent_projects()
    assert [e["path"] for e in recent] == [str(proj.resolve())]
    assert recent[0]["name"] == "projA"
    assert recent[0]["exists"] is True


def test_most_recent_first(recents_home):
    for name in ("a", "b", "c"):
        (recents_home / name).mkdir()
        project_manager.remember_project(recents_home / name)
    assert [e["name"] for e in project_manager.load_recent_projects()] == ["c", "b", "a"]


def test_reopening_moves_to_top_without_duplicating(recents_home):
    for name in ("a", "b", "c"):
        (recents_home / name).mkdir()
        project_manager.remember_project(recents_home / name)
    project_manager.remember_project(recents_home / "a")
    names = [e["name"] for e in project_manager.load_recent_projects()]
    assert names == ["a", "c", "b"]
    assert names.count("a") == 1


def test_same_dir_via_different_paths_is_one_entry(recents_home):
    proj = recents_home / "projA"
    proj.mkdir()
    project_manager.remember_project(proj)
    # The same directory reached by a longer route. Resolving before comparing
    # is what keeps these from becoming three separate entries in the list.
    project_manager.remember_project(proj / ".")
    project_manager.remember_project(recents_home / "projA" / ".." / "projA")
    recent = project_manager.load_recent_projects()
    assert len(recent) == 1
    assert recent[0]["path"] == str(proj.resolve())


def test_recents_are_capped(recents_home):
    for i in range(project_manager.RECENTS_LIMIT + 5):
        d = recents_home / f"p{i:02d}"
        d.mkdir()
        project_manager.remember_project(d)
    recent = project_manager.load_recent_projects()
    assert len(recent) == project_manager.RECENTS_LIMIT
    # the oldest ones fell off, not the newest
    assert recent[0]["name"] == f"p{project_manager.RECENTS_LIMIT + 4:02d}"


def test_deleted_project_is_kept_but_flagged(recents_home):
    proj = recents_home / "gone"
    proj.mkdir()
    project_manager.remember_project(proj)
    proj.rmdir()
    recent = project_manager.load_recent_projects()
    assert len(recent) == 1
    assert recent[0]["exists"] is False
    assert recent[0]["is_project"] is False


def test_is_project_flag_is_recomputed_not_cached(recents_home):
    proj = recents_home / "later"
    proj.mkdir()
    project_manager.remember_project(proj)
    assert project_manager.load_recent_projects()[0]["is_project"] is False
    # RELION itself writes this the first time a real job runs -- this app is
    # not involved, so a cached flag would go stale.
    (proj / "default_pipeline.star").write_text("# written by RELION\n")
    assert project_manager.load_recent_projects()[0]["is_project"] is True


def test_forget_removes_only_that_entry(recents_home):
    for name in ("a", "b"):
        (recents_home / name).mkdir()
        project_manager.remember_project(recents_home / name)
    project_manager.forget_project(recents_home / "a")
    assert [e["name"] for e in project_manager.load_recent_projects()] == ["b"]


def test_forget_does_not_delete_the_directory(recents_home):
    proj = recents_home / "keepme"
    proj.mkdir()
    (proj / "data.star").write_text("x")
    project_manager.remember_project(proj)
    project_manager.forget_project(proj)
    assert proj.is_dir() and (proj / "data.star").exists()


def test_corrupt_cache_returns_empty_not_error(recents_home):
    p = project_manager.recents_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json at all")
    assert project_manager.load_recent_projects() == []


def test_cache_holding_a_json_object_returns_empty(recents_home):
    p = project_manager.recents_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"path": "/tmp"}')      # object, not the expected list
    assert project_manager.load_recent_projects() == []


def test_unwritable_config_dir_does_not_raise(recents_home, monkeypatch):
    # A read-only or full home directory must not stop the user opening a
    # project -- the recent list is a convenience, not the task.
    def boom(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr(project_manager.Path, "write_text", boom)
    project_manager.remember_project(recents_home)   # must not raise


# --------------------------------------------------------------------------
# Global (per-user) settings -- job-run defaults + a few app-behavior knobs,
# reusing the recents_home fixture above (same config_root(), same
# XDG_CONFIG_HOME redirection).
# --------------------------------------------------------------------------


def test_global_settings_missing_file_returns_defaults(recents_home):
    assert project_manager.load_global_settings() == project_manager.GLOBAL_SETTINGS_DEFAULTS


def test_global_settings_corrupt_file_returns_defaults_not_error(recents_home):
    p = project_manager.global_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json at all")
    assert project_manager.load_global_settings() == project_manager.GLOBAL_SETTINGS_DEFAULTS


def test_global_settings_file_holding_a_list_returns_defaults(recents_home):
    p = project_manager.global_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[1, 2, 3]")
    assert project_manager.load_global_settings() == project_manager.GLOBAL_SETTINGS_DEFAULTS


def test_global_settings_save_then_load_round_trips(recents_home):
    saved = project_manager.save_global_settings({"job_defaults.nr_mpi": 4})
    assert saved["job_defaults.nr_mpi"] == 4
    assert project_manager.load_global_settings()["job_defaults.nr_mpi"] == 4


def test_global_settings_partial_save_does_not_clobber_other_keys(recents_home):
    project_manager.save_global_settings({"job_defaults.nr_mpi": 4, "job_defaults.gpu_ids": "0:1"})
    project_manager.save_global_settings({"job_defaults.nr_mpi": 8})
    current = project_manager.load_global_settings()
    assert current["job_defaults.nr_mpi"] == 8
    assert current["job_defaults.gpu_ids"] == "0:1"   # untouched by the second save


def test_global_settings_unknown_keys_are_dropped_on_save(recents_home):
    saved = project_manager.save_global_settings({"job_defaults.nr_mpi": 2, "not_a_real_key": "x"})
    assert "not_a_real_key" not in saved
    assert "not_a_real_key" not in project_manager.load_global_settings()


def test_global_settings_unwritable_config_dir_does_not_raise(recents_home, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr(project_manager.Path, "write_text", boom)
    result = project_manager.save_global_settings({"job_defaults.nr_mpi": 4})   # must not raise
    assert result["job_defaults.nr_mpi"] == 4   # returns the merged value even though the write failed
