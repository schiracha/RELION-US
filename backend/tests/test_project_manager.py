"""
Tests for project_manager.py — the "Change Project" feature (switching the
active RELION project directory at runtime instead of it being hardcoded to
wherever the app happens to be installed) and its history persistence.
"""
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
