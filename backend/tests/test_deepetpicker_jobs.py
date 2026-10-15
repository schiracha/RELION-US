"""
Tests for deepetpicker_jobs.py and its wiring into job_catalog.py/main.py.

The three job types run the `deepetpicker` command line from the DeepETPicker
repository (schiracha/DeepETPicker_Sc) as a subprocess, like the IsoNet2 jobs.
EXPECTED_FLAGS below is that command line's argparse surface, copied from
`deepetpicker.cli.build_parser()`; the DeepETPicker repo's own test suite and
this one meet at that list.
"""
import shlex
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_catalog
import main
import project_manager
from deepetpicker_jobs import (
    DEEPETPICKER_JOB_DEFINITIONS,
    DEEPETPICKER_LOSS_JOB_TYPES,
    build_deepetpicker_command,
    validate_deepetpicker_fields,
)

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

NAMES = sorted(DEEPETPICKER_JOB_DEFINITIONS)

# every flag the CLI accepts, per subcommand, and the ones this UI deliberately does not expose
EXPECTED_FLAGS = {
    "prepare": {"output_dir", "tomo_dir", "tomo_star", "tomo_column", "tomo_ext", "project_dir", "coords_dir",
                "coords_star", "coords_ext", "coords_scale", "num_cls", "label_type", "label_diameter",
                "ocp_diameter", "pixel_size", "particle_diameter_A", "norm_type"},
    "train": {"output_dir", "prepare_job", "val_tomo", "train_tomos", "batch_size", "patch_size", "pad_size",
              "lr", "max_epochs", "seg_thresh", "gpu", "resume"},
    "predict": {"output_dir", "train_job", "model", "tomo_dir", "tomo_star", "tomo_column", "tomo_ext",
                "project_dir", "already_standardized", "batch_size", "patch_size", "pad_size", "seg_thresh",
                "gpu", "refine_centroids", "relion_scale"},
}
NOT_EXPOSED = {"tomo_ext", "project_dir", "coords_ext", "output_dir"}


# ---- catalog ---------------------------------------------------------
def test_every_job_is_in_the_catalog_under_its_own_category():
    for name in NAMES:
        assert name in job_catalog.CUSTOM_JOBS and name in job_catalog.JOB_DIRNAME
        meta = job_catalog.CUSTOM_JOBS[name]
        assert meta["label_new"].startswith("custom.deepetpicker_")
        assert meta["category"] == "DeepETPicker (Beta)"
        assert meta["category"] in job_catalog.CATEGORIES
        assert job_catalog.pipeline_type(name) == "tomo"


def test_not_registered_as_in_process_custom_jobs_or_isonet_jobs():
    import custom_jobs
    from isonet_jobs import ISONET_JOB_DEFINITIONS
    assert set(NAMES).isdisjoint(custom_jobs.CUSTOM_JOB_DEFINITIONS)
    assert set(NAMES).isdisjoint(ISONET_JOB_DEFINITIONS)
    assert "DeepETPickerImport" in custom_jobs.CUSTOM_JOB_DEFINITIONS      # the converter is untouched


@pytest.mark.parametrize("name", NAMES)
def test_standard_fields_exist_and_cover_every_option(name):
    d = DEEPETPICKER_JOB_DEFINITIONS[name]
    keys = {o["key"] for o in d["options"]}
    placed = [k for g in d["standard_groups"] for k in g["fields"]]
    assert set(placed) == keys and len(placed) == len(keys)


@pytest.mark.parametrize("name", NAMES)
def test_option_keys_are_real_cli_flags(name):
    d = DEEPETPICKER_JOB_DEFINITIONS[name]
    flags = EXPECTED_FLAGS[d["subcommand"]]
    keys = {o["key"] for o in d["options"]} - {"conda_env"}
    assert keys <= flags, keys - flags
    assert flags - keys - NOT_EXPOSED == set()          # nothing the CLI offers is silently unreachable


@pytest.mark.parametrize("name", NAMES)
def test_defaults_give_the_bare_command(name):
    d = DEEPETPICKER_JOB_DEFINITIONS[name]
    defaults = {o["key"]: o.get("default", "") for o in d["options"]}
    cmd = build_deepetpicker_command(name, defaults, f"{name}/job001")
    assert cmd == (f"conda run --no-capture-output -n deepetpicker deepetpicker {d['subcommand']} "
                   f"--output_dir {name}/job001/")


# ---- command building ------------------------------------------------
def test_prepare_command():
    cmd = build_deepetpicker_command("DeepETPickerPrepare", {
        "tomo_dir": "Tomograms/job006/tomos", "coords_star": "Picks/job010/particles.star",
        "coords_scale": "1.6667", "pixel_size": "6.3", "particle_diameter_A": "150", "norm_type": "standardization",
        "label_type": "sphere",
    }, "DeepETPickerPrepare/job011")
    assert shlex.split(cmd)[6:] == [
        "prepare", "--output_dir", "DeepETPickerPrepare/job011/",
        "--tomo_dir", "Tomograms/job006/tomos", "--coords_star", "Picks/job010/particles.star",
        "--coords_scale", "1.6667", "--label_type", "sphere", "--pixel_size", "6.3",
        "--particle_diameter_A", "150"]


def test_train_command_and_env():
    cmd = build_deepetpicker_command("DeepETPickerTrain", {
        "conda_env": "my env", "prepare_job": "DeepETPickerPrepare/job011", "val_tomo": "TS_002",
        "train_tomos": "TS_000,TS_001", "batch_size": 4, "patch_size": 64, "gpu": "0,1",
    }, "DeepETPickerTrain/job012/")
    t = shlex.split(cmd)
    assert t[:6] == ["conda", "run", "--no-capture-output", "-n", "my env", "deepetpicker"]
    assert t[6:] == ["train", "--output_dir", "DeepETPickerTrain/job012/",
                     "--prepare_job", "DeepETPickerPrepare/job011", "--val_tomo", "TS_002",
                     "--train_tomos", "TS_000,TS_001", "--batch_size", "4", "--patch_size", "64", "--gpu", "0,1"]


def test_booleans_are_bare_switches_and_false_is_omitted():
    base = {"train_job": "DeepETPickerTrain/job012", "tomo_dir": "t"}
    on = build_deepetpicker_command("DeepETPickerPredict", {**base, "refine_centroids": True,
                                                            "already_standardized": "true"}, "P/job1")
    off = build_deepetpicker_command("DeepETPickerPredict", {**base, "refine_centroids": False,
                                                             "already_standardized": False}, "P/job1")
    assert "--refine_centroids" in on.split() and "--already_standardized" in on.split()
    assert "True" not in on and "--refine_centroids" not in off and "--already_standardized" not in off


def test_paths_with_spaces_are_quoted_and_negative_numbers_are_values():
    cmd = build_deepetpicker_command("DeepETPickerPredict", {
        "train_job": "Deep ETPicker/job 2", "tomo_dir": "my tomos", "relion_scale": "-1"}, "P/job1")
    t = shlex.split(cmd)
    assert t[t.index("--train_job") + 1] == "Deep ETPicker/job 2"
    assert t[t.index("--relion_scale") + 1] == "-1"


# ---- validation ------------------------------------------------------
@pytest.mark.parametrize("name,values,fragment", [
    ("DeepETPickerPrepare", {}, "tomograms"),
    ("DeepETPickerPrepare", {"tomo_dir": "a", "tomo_star": "b"}, "tomograms"),
    ("DeepETPickerPrepare", {"tomo_dir": "a"}, "particles STAR"),
    ("DeepETPickerPrepare", {"tomo_dir": "a", "coords_dir": "c"}, "diameters"),
    ("DeepETPickerPrepare", {"tomo_dir": "a", "coords_dir": "c", "label_diameter": "10", "ocp_diameter": "11"}, "odd"),
    ("DeepETPickerPrepare", {"tomo_dir": "a", "coords_dir": "c", "label_diameter": "9", "ocp_diameter": "11,x"}, "odd"),
    ("DeepETPickerTrain", {"prepare_job": "p"}, "validation"),
    ("DeepETPickerTrain", {"val_tomo": "v"}, "Prepare job"),
    ("DeepETPickerPredict", {"tomo_dir": "t"}, "Train job"),
    ("DeepETPickerPredict", {"train_job": "j"}, "tomograms"),
])
def test_validation_rejects(name, values, fragment):
    assert any(fragment in p for p in validate_deepetpicker_fields(name, values))


def test_validation_accepts_good_input():
    assert validate_deepetpicker_fields("DeepETPickerPrepare", {
        "tomo_star": "t.star", "coords_dir": "c", "pixel_size": "6.3", "particle_diameter_A": "150"}) == []
    assert validate_deepetpicker_fields("DeepETPickerPrepare", {
        "tomo_dir": "t", "coords_star": "c.star", "label_diameter": "9", "ocp_diameter": "11,13"}) == []
    assert validate_deepetpicker_fields("DeepETPickerTrain", {"prepare_job": "p", "val_tomo": "v"}) == []
    assert validate_deepetpicker_fields("DeepETPickerPredict", {"train_job": "j", "tomo_dir": "t"}) == []


# ---- endpoints -------------------------------------------------------
@pytest.mark.parametrize("name", NAMES)
def test_job_definition_endpoint(name):
    d = main.get_job_definition(name)
    assert d["internal_name"] == name and DEEPETPICKER_JOB_DEFINITIONS[name]["subcommand"] in d["draft_command"].split()
    assert d["output_subdir"].startswith(name + "/job")


@pytest.mark.parametrize("name", NAMES)
def test_cli_options_and_draft_endpoints(name):
    assert main.job_cli_options(name)["reason"] == "deepetpicker_job"
    r = main.recompute_draft(name, main.DraftRequest(field_values={}, output_subdir=f"{name}/job009"))
    assert r["output_subdir"] == f"{name}/job009" and f"--output_dir {name}/job009/" in r["draft_command"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    project_dir = tmp_path / "project"
    project_manager.init_new_project(project_dir)
    main.run_manager.set_project_dir(project_dir)
    return TestClient(main.app)


def _wait(client, run_id, statuses, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = client.get(f"/api/runs/{run_id}").json()
        if last.get("status") in statuses:
            return last
        time.sleep(0.02)
    raise AssertionError(f"never reached {statuses}: {last}")


def test_run_goes_through_a_real_subprocess(client):
    resp = client.post("/api/runs", json={"internal_name": "DeepETPickerTrain", "command": "echo hello",
                                          "subdir": "DeepETPickerTrain/job001"})
    assert resp.status_code == 200 and resp.json()["command"] == "echo hello"
    assert _wait(client, resp.json()["run_id"], {"completed", "failed"})["status"] == "completed"


def test_run_without_command_builds_it_and_validates_first(client):
    bad = client.post("/api/runs", json={"internal_name": "DeepETPickerTrain", "field_values": {"prepare_job": "p"}})
    assert bad.status_code == 400 and "validation tomogram" in bad.json()["detail"]
    good = client.post("/api/runs", json={"internal_name": "DeepETPickerTrain",
                                          "field_values": {"prepare_job": "p", "val_tomo": "TS_001"}})
    assert good.status_code == 200
    run = good.json()
    assert run["command"].startswith("conda run --no-capture-output -n deepetpicker deepetpicker train ")
    assert "--val_tomo TS_001" in run["command"]
    _wait(client, run["run_id"], {"completed", "failed"})          # conda is not installed here; it fails, which is fine


def test_an_unedited_drafted_command_is_validated_like_no_command(client):
    """What the popup really sends: the drafted command AND the (incomplete) fields."""
    sub = "DeepETPickerTrain/job001"
    for fields in ({}, {"prepare_job": "p"}):
        draft = main.build_external_command("DeepETPickerTrain", fields, sub)
        resp = client.post("/api/runs", json={"internal_name": "DeepETPickerTrain", "command": draft,
                                              "field_values": fields, "subdir": sub})
        assert resp.status_code == 400 and "validation tomogram" in resp.json()["detail"]
    assert client.get("/api/runs").json() == []                      # nothing was started


def test_hand_edited_command_skips_field_validation(client):
    resp = client.post("/api/runs", json={"internal_name": "DeepETPickerTrain", "command": "true",
                                          "field_values": {}})
    assert resp.status_code == 200
    _wait(client, resp.json()["run_id"], {"completed", "failed"})


# ---- progress tab ----------------------------------------------------
def test_train_job_gets_the_loss_curve_view():
    assert DEEPETPICKER_LOSS_JOB_TYPES == {"DeepETPickerTrain"}
    js = (Path(__file__).resolve().parents[2] / "frontend" / "app.js").read_text()
    line = next(l for l in js.splitlines() if l.startswith("const ISONET_LOSS_JOB_TYPES"))
    assert '"DeepETPickerTrain"' in line and "loss_full.png" in js
