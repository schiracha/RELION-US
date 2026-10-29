"""
Outputs-tab preview of a job's tables: .star (as before) and, new, DeepETPicker's
.coords and any .csv, all through GET /api/runs/{id}/files/preview.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
import project_manager
import table_preview

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def job(tmp_path, monkeypatch):
    """A finished run, and a function that writes a file into its directory
    and returns the preview response for it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    project = tmp_path / "project"
    project_manager.init_new_project(project)
    main.run_manager.set_project_dir(project)
    client = fastapi_testclient.TestClient(main.app)
    run = client.post("/api/runs", json={"internal_name": "IsonetDeconv", "command": "true"}).json()
    for _ in range(200):
        if client.get(f"/api/runs/{run['run_id']}").json()["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    cwd = Path(run["cwd"])

    def preview(name, content, **params):
        (cwd / name).write_text(content)
        return client.get(f"/api/runs/{run['run_id']}/files/preview", params={"path": name, **params})

    preview.client, preview.run, preview.cwd = client, run, cwd
    return preview


def test_a_four_column_coords_file_shows_class_and_xyz(job):
    resp = job("picks.coords", "1 10.5 20 30\n2 11 21 31\n")
    assert resp.status_code == 200
    (block,) = resp.json()["blocks"]
    assert block["columns"] == ["class_id", "x", "y", "z"]
    assert block["rows"] == [[1, 10.5, 20, 30], [2, 11, 21, 31]]
    assert block["total_rows"] == 2 and block["truncated"] is False


def test_coords_with_three_columns_or_the_area_variant_or_comments_are_shown_not_refused(job):
    three = job("a.coords", "# header comment\n\n5 6 7\n8 9 10\n").json()["blocks"][0]
    assert three["columns"] == ["x", "y", "z"] and three["total_rows"] == 2     # comment and blank skipped
    five = job("b.coords", "1 2 3 4 55.5\n").json()["blocks"][0]
    assert five["columns"] == ["class_id", "x", "y", "z", "area"]
    odd = job("c.coords", "1 2 3 4 5 6\n").json()["blocks"][0]
    assert odd["columns"] == [f"col{i}" for i in range(1, 7)]


def test_a_long_coords_file_is_cut_to_max_rows_but_counted(job):
    body = job("big.coords", "".join(f"1 {i} {i} {i}\n" for i in range(2500)), max_rows=50).json()["blocks"][0]
    assert len(body["rows"]) == 50 and body["total_rows"] == 2500 and body["truncated"] is True
    huge = job.client.get(f"/api/runs/{job.run['run_id']}/files/preview",
                          params={"path": "big.coords", "max_rows": 10**9}).json()["blocks"][0]
    assert len(huge["rows"]) == main.PREVIEW_MAX_ROWS_CAP                      # asking for everything is capped


def test_an_empty_coords_file_has_no_blocks(job):
    assert job("empty.coords", "\n\n# nothing\n").json()["blocks"] == []


def test_a_csv_shows_its_header_and_rows(job):
    block = job("metrics.csv", "epoch,loss,val_dice\n1,0.9,0.1\n2,0.5,0.4\n3,,0.6\n").json()["blocks"][0]
    assert block["columns"] == ["epoch", "loss", "val_dice"]
    assert block["rows"] == [[1, 0.9, 0.1], [2, 0.5, 0.4], [3, None, 0.6]]      # a gap is null, not NaN
    assert block["total_rows"] == 3 and block["truncated"] is False


def test_a_csv_is_cut_to_max_rows_and_counted(job):
    text = "i,sq\n" + "".join(f"{i},{i * i}\n" for i in range(500))
    block = job("table.csv", text, max_rows=20).json()["blocks"][0]
    assert len(block["rows"]) == 20 and block["total_rows"] == 500 and block["truncated"] is True


def test_a_csv_without_a_header_row_keeps_its_first_row(job):
    """Real case: DeepETPicker's num_name.csv is `0,<tomogram name>` rows with no header."""
    block = job("num_name.csv", "0,TS_001\n1,TS_002\n2,TS_003\n").json()["blocks"][0]
    assert block["columns"] == ["col1", "col2"]
    assert block["rows"][0] == [0, "TS_001"] and block["total_rows"] == 3


def test_tab_and_semicolon_separated_csv_files_are_split_too(job):
    assert job("t.csv", "a\tb\n1\t2\n").json()["blocks"][0]["columns"] == ["a", "b"]
    assert job("s.csv", "a;b;c\n1;2;3\n").json()["blocks"][0]["rows"] == [[1, 2, 3]]


def test_a_huge_file_is_not_counted_and_says_so(job, monkeypatch):
    monkeypatch.setattr(table_preview, "COUNT_LIMIT_BYTES", 10)
    block = job("big.csv", "a,b\n" + "1,2\n" * 50, max_rows=5).json()["blocks"][0]
    assert block["total_rows"] is None and block["truncated"] is True and len(block["rows"]) == 5


def test_an_empty_or_malformed_csv_is_handled_plainly(job):
    assert job("empty.csv", "").json()["blocks"] == []
    bad = job("bad.csv", "a,b\n1,2,3,4,5\n6\n")
    assert bad.status_code in (200, 400)                      # ragged rows: shown or refused, never a 500
    assert bad.status_code != 500


def test_star_files_still_preview_and_other_files_are_still_refused(job):
    star = job("x.star", "\ndata_particles\n\nloop_\n_rlnA #1\n_rlnB #2\n1 2\n3 4\n")
    assert star.status_code == 200
    block = star.json()["blocks"][0]
    assert block["name"] == "particles" and block["kind"] == "loop" and block["total_rows"] == 2
    refused = job("notes.txt", "hello")
    assert refused.status_code == 400 and ".coords" in refused.json()["detail"] and ".csv" in refused.json()["detail"]


def test_paths_cannot_escape_the_job_directory(job):
    resp = job.client.get(f"/api/runs/{job.run['run_id']}/files/preview", params={"path": "../../../etc/passwd.csv"})
    assert resp.status_code == 404
    assert job.client.get("/api/runs/nope/files/preview", params={"path": "a.csv"}).status_code == 404
