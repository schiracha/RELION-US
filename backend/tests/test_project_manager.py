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
