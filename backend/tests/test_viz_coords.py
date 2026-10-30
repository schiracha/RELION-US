"""
The viewer's picks input accepts DeepETPicker .coords files and folders of them (and its main
input a folder of volumes), so a predict job can be previewed while it runs: its coords/ gains a
file as each tomogram finishes, and its tomograms_std/ already holds the volumes.
"""
import sys
from pathlib import Path

import mrcfile
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
import project_manager
import viz

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    project_manager.init_new_project(p)
    return p


def _coords(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _volume(path, shape=(6, 8, 10)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with mrcfile.new(path, overwrite=True) as m:
        m.set_data(np.zeros(shape, dtype=np.float32))
        m.voxel_size = 6.3
    return path


# ---- reading one file ---------------------------------------------------------

def test_the_forms_a_coords_file_comes_in(tmp_path):
    f = _coords(tmp_path / "a.coords", "# comment\n\n1\t10.5\t20\t30\n2 11 21 31\n3,4,5\n")
    d = viz.read_coords_file(f)
    assert d["class"].tolist() == [1.0, 2.0, 1.0]            # a bare "x y z" is class 1
    assert d["x"].tolist() == [10.5, 11.0, 3.0] and d["y"].tolist() == [20.0, 21.0, 4.0]
    assert d["z"].tolist() == [30.0, 31.0, 5.0]
    assert d["score"] is None


def test_the_five_column_form_carries_each_picks_score(tmp_path):
    d = viz.read_coords_file(_coords(tmp_path / "a.coords", "1.0\t10.0\t20.0\t30.0\t0.95\n1.0\t1\t2\t3\t0.40\n"))
    assert d["score"].tolist() == [0.95, 0.40] and d["x"].tolist() == [10.0, 1.0]


def test_an_unfinished_last_line_of_a_file_still_being_written_is_ignored(tmp_path):
    d = viz.read_coords_file(_coords(tmp_path / "a.coords", "1\t1\t2\t3\n1\t4\t5"))
    assert d["x"].tolist() == [1.0]
    with pytest.raises(viz.VizError, match="b.coords:2.*got 2"):          # but a bad COMPLETE line is an error
        viz.read_coords_file(_coords(tmp_path / "b.coords", "1\t1\t2\t3\n1\t4\n"))
    # a last line with no newline that fits the others is kept (other tools do not always end with one)
    assert viz.read_coords_file(_coords(tmp_path / "d.coords", "1\t1\t2\t3\n1\t4\t5\t6"))["x"].tolist() == [1.0, 4.0]
    with pytest.raises(viz.VizError, match="could not convert|expected"):
        viz.read_coords_file(_coords(tmp_path / "c.coords", "a b c\n"))


def test_an_empty_coords_file_is_no_picks(tmp_path):
    assert len(viz.read_coords_file(_coords(tmp_path / "a.coords", "\n# nothing\n"))["x"]) == 0


# ---- loading picks for the viewer ---------------------------------------------------

def test_a_coords_file_gives_that_tomograms_picks(project):
    _coords(project / "Predict/job1/coords/TS_001.coords", "1\t10\t20\t30\n1\t11\t21\t31\n")
    r = viz.load_picks(project, "Predict/job1/coords/TS_001.coords", "TS_001")
    assert [(p["x"], p["y"], p["z"], p["class"]) for p in r["picks"]] == [(10.0, 20.0, 30.0, 1), (11.0, 21.0, 31.0, 1)]
    assert r["matched"] is True and r["tomo_names"] == ["TS_001"] and r["source"] == "TS_001.coords"
    assert all("score" not in p for p in r["picks"])


def test_a_coords_file_for_another_tomogram_warns_but_still_loads_when_asked(project):
    _coords(project / "coords/TS_001.coords", "1\t10\t20\t30\n")
    r = viz.load_picks(project, "coords/TS_001.coords", "TS_002")
    assert r["matched"] is False and "TS_002" in r["message"] and len(r["picks"]) == 1


def test_scores_are_passed_through(project):
    _coords(project / "coords_with_area/TS_001.coords", "1.0\t10\t20\t30\t0.9\n")
    assert viz.load_picks(project, "coords_with_area/TS_001.coords", "TS_001")["picks"][0]["score"] == 0.9


def test_a_folder_gives_the_picks_of_the_chosen_tomogram(project):
    folder = project / "Predict/job1/coords"
    _coords(folder / "TS_001.coords", "1\t1\t1\t1\n")
    _coords(folder / "TS_010.coords", "1\t9\t9\t9\n1\t8\t8\t8\n")
    r = viz.load_picks(project, "Predict/job1/coords", "TS_010")
    assert r["matched"] is True and len(r["picks"]) == 2 and r["source"] == "TS_010.coords"
    assert r["tomo_names"] == ["TS_001", "TS_010"]
    # a name that is only a prefix of another must not match it (TS_01 vs TS_010)
    assert viz.load_picks(project, "Predict/job1/coords", "TS_01")["picks"] == []
    assert viz.load_picks(project, "Predict/job1/coords/", "rec_TS_001")["source"] == "TS_001.coords"


def test_a_tomogram_the_running_job_has_not_finished_says_so_instead_of_showing_picks(project):
    _coords(project / "Predict/job1/coords/TS_001.coords", "1\t1\t1\t1\n")
    r = viz.load_picks(project, "Predict/job1/coords", "TS_002")
    assert r["picks"] == [] and r["matched"] is False and r["source"] is None
    assert "No .coords file for 'TS_002'" in r["message"] and "as each tomogram finishes" in r["message"]


def test_a_folder_with_one_file_and_no_tomogram_name_uses_it(project):
    _coords(project / "coords/TS_001.coords", "1\t1\t1\t1\n")
    assert len(viz.load_picks(project, "coords", None)["picks"]) == 1
    _coords(project / "coords/TS_002.coords", "1\t1\t1\t1\n")
    assert viz.load_picks(project, "coords", None)["picks"] == []               # ambiguous: none


def test_picks_input_errors_are_plain_and_stay_inside_the_project(project, tmp_path):
    (project / "empty").mkdir()
    with pytest.raises(viz.VizError, match="no .coords files"):
        viz.load_picks(project, "empty", "TS_001")
    with pytest.raises(viz.VizError, match="outside the project"):
        viz.load_picks(project, str(tmp_path / "elsewhere.coords"), "TS_001")
    outside = _coords(tmp_path / "outside.coords", "1\t1\t1\t1\n")
    folder = project / "coords"
    folder.mkdir()
    (folder / "TS_001.coords").symlink_to(outside)                            # a link out of the project
    with pytest.raises(viz.VizError, match="outside the project"):
        viz.load_picks(project, "coords", "TS_001")


# ---- inspect: folder of volumes, folder / file of picks -----------------------------------

def test_a_folder_of_volumes_offers_every_volume(project):
    _volume(project / "Predict/job1/tomograms_std/TS_001.mrc")
    real = _volume(project / "Prepare/job0/TS_002.mrc")
    (project / "Predict/job1/tomograms_std/TS_002.mrc").symlink_to(real)      # as a predict job links them
    (project / "Predict/job1/tomograms_std/num_name.csv").write_text("0\tTS_001\n")
    info = viz.inspect(project, "Predict/job1/tomograms_std")
    assert info["kind"] == "volume_folder"
    assert [t["name"] for t in info["tomograms"]] == ["TS_001", "TS_002"]
    assert info["tomograms"][0]["mrc_path"].endswith("tomograms_std/TS_001.mrc")
    (project / "empty").mkdir()
    with pytest.raises(viz.VizError, match="no volumes"):
        viz.inspect(project, "empty")
    with pytest.raises(viz.VizError, match="not found"):
        viz.inspect(project, "nope")


def test_inspect_accepts_a_coords_file_or_folder_as_the_picks(project):
    vol = _volume(project / "T/TS_001.mrc")
    _coords(project / "c/TS_001.coords", "1\t1\t1\t1\n")
    assert viz.inspect(project, "T/TS_001.mrc", "c")["particles_path"].endswith("/c")
    assert viz.inspect(project, "T/TS_001.mrc", "c/TS_001.coords")["particles_path"].endswith("TS_001.coords")
    (project / "none").mkdir()
    with pytest.raises(viz.VizError, match="no .coords files"):
        viz.inspect(project, "T/TS_001.mrc", "none")
    with pytest.raises(viz.VizError, match="not found"):
        viz.inspect(project, "T/TS_001.mrc", "missing.coords")


# ---- over HTTP ------------------------------------------------------------------------------

@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    project = tmp_path / "project"
    project_manager.init_new_project(project)
    main.run_manager.set_project_dir(project)
    return fastapi_testclient.TestClient(main.app), project


def test_the_endpoints_preview_a_running_jobs_folders(http):
    client, project = http
    _volume(project / "Predict/job1/tomograms_std/TS_001.mrc")
    _volume(project / "Predict/job1/tomograms_std/TS_002.mrc")
    _coords(project / "Predict/job1/coords/TS_001.coords", "1\t1\t2\t3\n1\t4\t5\t6\n")
    info = client.post("/api/viz/inspect", json={"path": "Predict/job1/tomograms_std",
                                                 "particles_path": "Predict/job1/coords"}).json()
    assert [t["name"] for t in info["tomograms"]] == ["TS_001", "TS_002"] and info["particles_path"]
    first = client.post("/api/viz/picks", json={"particles_path": "Predict/job1/coords", "tomo_name": "TS_001"}).json()
    assert len(first["picks"]) == 2 and first["matched"] is True
    second = client.post("/api/viz/picks", json={"particles_path": "Predict/job1/coords", "tomo_name": "TS_002"}).json()
    assert second["picks"] == [] and second["matched"] is False and "No .coords file" in second["message"]
    _coords(project / "Predict/job1/coords/TS_002.coords", "1\t7\t8\t9\n")                  # the job finishes it
    third = client.post("/api/viz/picks", json={"particles_path": "Predict/job1/coords", "tomo_name": "TS_002"}).json()
    assert len(third["picks"]) == 1 and third["matched"] is True
    bad = client.post("/api/viz/picks", json={"particles_path": "/etc", "tomo_name": "x"})
    assert bad.status_code == 400
