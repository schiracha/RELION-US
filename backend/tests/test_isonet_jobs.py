"""
Tests for isonet_jobs.py and its wiring into job_catalog.py/main.py.

build_isonet_command's flags are deliberately SPACE-separated (`--key
value`, not `--key=value`) -- confirmed running the real `conda run -n
isonet2_environment isonet.py ...` invocation by hand: this machine's conda
(25.1.1) mishandles `--key=value` tokens immediately after `conda run`,
stripping the leading `--` before isonet.py ever sees them. See
build_isonet_command's own docstring. These tests assert on that exact
space-separated shape, not the `=` form isonet.py's own `--help` documents.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_catalog
import main
import project_manager
from isonet_jobs import ISONET_JOB_DEFINITIONS, build_isonet_command

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient


def test_every_isonet_job_is_in_the_catalog():
    """job_catalog.JOB_DIRNAME's own assert already enforces this at import
    time (set(JOB_DIRNAME) == set(JOB_CATALOG) | set(CUSTOM_JOBS)) -- this
    test just names WHICH invariant that covers, for when it breaks."""
    for internal_name in ISONET_JOB_DEFINITIONS:
        assert internal_name in job_catalog.CUSTOM_JOBS, internal_name
        assert internal_name in job_catalog.JOB_DIRNAME, internal_name
        assert job_catalog.CUSTOM_JOBS[internal_name]["label_new"].startswith("custom.isonet_")


def test_isonet_jobs_are_not_also_registered_as_plain_custom_jobs():
    """These run through start_subprocess_job (local + SLURM), not
    start_custom_job -- see isonet_jobs.py's module docstring for why. If a
    name ever ended up in BOTH dicts, main.py's CUSTOM_JOB_DEFINITIONS check
    (which comes first) would silently steal it onto the wrong execution
    path with no error."""
    import custom_jobs
    assert set(ISONET_JOB_DEFINITIONS).isdisjoint(custom_jobs.CUSTOM_JOB_DEFINITIONS)


@pytest.mark.parametrize("internal_name", sorted(ISONET_JOB_DEFINITIONS))
def test_isonet_job_standard_fields_all_exist(internal_name):
    definition = ISONET_JOB_DEFINITIONS[internal_name]
    keys = {o["key"] for o in definition["options"]}
    placed = {k for g in definition["standard_groups"] for k in g["fields"]}
    assert placed <= keys


@pytest.mark.parametrize("internal_name", sorted(ISONET_JOB_DEFINITIONS))
def test_every_isonet_job_has_a_conda_env_option(internal_name):
    keys = {o["key"] for o in ISONET_JOB_DEFINITIONS[internal_name]["options"]}
    assert "conda_env" in keys


@pytest.mark.parametrize("internal_name", sorted(ISONET_JOB_DEFINITIONS))
def test_default_field_values_produce_the_bare_command(internal_name):
    """With every field left at its own declared default, the draft should
    be just the conda/isonet.py/subcommand prefix plus whatever this module
    ALWAYS needs (output_dir, or star_name for prepare_star) -- no stray
    flags for values that don't differ from doing nothing."""
    definition = ISONET_JOB_DEFINITIONS[internal_name]
    defaults = {opt["key"]: opt.get("default", "") for opt in definition["options"]}
    cmd = build_isonet_command(internal_name, defaults, f"{internal_name}/job001")
    tokens = cmd.split()
    assert tokens[:6] == ["conda", "run", "--no-capture-output", "-n", "isonet2_environment", "isonet.py"]
    assert tokens[6] == definition["subcommand"]
    if internal_name == "IsonetPrepareStar":
        assert "--output_dir" not in tokens
        assert "--star_name" in tokens
    else:
        assert "--output_dir" in tokens
        assert tokens[tokens.index("--output_dir") + 1] == f"{internal_name}/job001/"


def test_build_isonet_command_uses_space_separated_flags_not_equals():
    """The `--key=value` form isonet.py's own --help documents breaks when
    run through `conda run` on this machine -- see the module docstring.
    Every flag this builder emits must be its own token, not glued to its
    value with `=`."""
    cmd = build_isonet_command(
        "IsonetDeconv", {"star_file": "Import/job001/tomograms.star", "ncpus": "8"},
        "IsonetDeconv/job002",
    )
    assert "=" not in cmd
    tokens = cmd.split()
    assert "--ncpus" in tokens
    assert tokens[tokens.index("--ncpus") + 1] == "8"
    assert "--star_file" in tokens
    assert tokens[tokens.index("--star_file") + 1] == "Import/job001/tomograms.star"


def test_conda_env_field_changes_the_dash_n_token_not_a_flag():
    cmd = build_isonet_command("IsonetPrepareStar", {"conda_env": "my_custom_env"}, "IsonetPrepareStar/job003")
    tokens = cmd.split()
    assert tokens[3] == "-n"
    assert tokens[4] == "my_custom_env"
    assert "--conda_env" not in tokens  # consumed into the conda prefix, not an isonet.py flag


def test_conda_env_blank_falls_back_to_the_documented_default():
    cmd = build_isonet_command("IsonetPrepareStar", {"conda_env": "  "}, "IsonetPrepareStar/job004")
    tokens = cmd.split()
    assert tokens[4] == "isonet2_environment"


def test_prepare_star_folds_the_job_dir_into_star_name_when_unqualified():
    cmd = build_isonet_command("IsonetPrepareStar", {}, "IsonetPrepareStar/job005")
    tokens = cmd.split()
    assert tokens[tokens.index("--star_name") + 1] == "IsonetPrepareStar/job005/tomograms.star"


def test_prepare_star_respects_a_user_supplied_path_containing_a_slash():
    cmd = build_isonet_command(
        "IsonetPrepareStar", {"star_name": "somewhere/else.star"}, "IsonetPrepareStar/job006",
    )
    tokens = cmd.split()
    assert tokens[tokens.index("--star_name") + 1] == "somewhere/else.star"


@pytest.mark.parametrize("internal_name", sorted(ISONET_JOB_DEFINITIONS))
def test_boolean_fields_render_as_explicit_true_false(internal_name):
    definition = ISONET_JOB_DEFINITIONS[internal_name]
    bool_opts = [o for o in definition["options"] if o["field_type"] == "boolean"]
    if not bool_opts:
        pytest.skip("no boolean fields on this job")
    opt = bool_opts[0]
    flipped = not opt["default"]
    cmd = build_isonet_command(internal_name, {opt["key"]: flipped}, f"{internal_name}/job007")
    tokens = cmd.split()
    assert tokens[tokens.index(f"--{opt['key']}") + 1] == ("True" if flipped else "False")


def test_negative_number_values_are_not_mistaken_for_flags():
    """A value like -60 immediately after its own --key is fine (confirmed
    running the real conda env by hand: `--tilt_min -60` parses correctly,
    unlike the `=` form which breaks for an unrelated reason -- see the
    module docstring). Nothing here should quote/escape it into something
    conda or isonet.py's own parser would choke on."""
    cmd = build_isonet_command("IsonetPrepareStar", {"tilt_min": "-70"}, "IsonetPrepareStar/job008")
    tokens = cmd.split()
    assert tokens[tokens.index("--tilt_min") + 1] == "-70"


# --------------------------------------------------------------------------
# main.py routing -- ISONET_JOB_DEFINITIONS must never fall into either the
# CUSTOM_JOB_DEFINITIONS (in-process, no SLURM) branch or silently 404 in
# the real-RELION-job fallback.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("internal_name", sorted(ISONET_JOB_DEFINITIONS))
def test_get_job_definition_routes_isonet_jobs_to_isonet_helper(internal_name):
    definition = main.get_job_definition(internal_name)
    assert definition["internal_name"] == internal_name
    assert "draft_command" in definition
    assert definition["draft_command"].split()[6] == ISONET_JOB_DEFINITIONS[internal_name]["subcommand"]


@pytest.mark.parametrize("internal_name", sorted(ISONET_JOB_DEFINITIONS))
def test_cli_options_endpoint_does_not_crash_for_isonet_jobs(internal_name):
    payload = main.job_cli_options(internal_name)
    assert payload["available"] is False
    assert payload["reason"] == "isonet_job"


@pytest.mark.parametrize("internal_name", sorted(ISONET_JOB_DEFINITIONS))
def test_draft_endpoint_builds_a_real_command_for_isonet_jobs(internal_name):
    req = main.DraftRequest(field_values={}, output_subdir=f"{internal_name}/job009")
    result = main.recompute_draft(internal_name, req)
    assert result["output_subdir"] == f"{internal_name}/job009"
    assert result["unmapped_fields"] == []
    assert ISONET_JOB_DEFINITIONS[internal_name]["subcommand"] in result["draft_command"]


# ---------------------------------------------------------------------------
# Full HTTP-level check, same pattern as test_main_endpoints.py's own
# `client` fixture (duplicated, not imported -- see test_custom_jobs.py's
# synced_project fixture for why this file follows the same "no
# conftest.py yet, so duplicate the small fixture" convention).
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    project_dir = tmp_path / "project"
    project_manager.init_new_project(project_dir)
    main.run_manager.set_project_dir(project_dir)
    return TestClient(main.app)


def _wait_for_status(client, run_id, statuses, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        resp = client.get(f"/api/runs/{run_id}")
        last = resp.json()
        if last.get("status") in statuses:
            return last
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never reached {statuses}, last seen: {last}")


def test_start_run_over_http_uses_start_subprocess_job_not_start_custom_job(client):
    """The whole point of NOT building IsoNet2 on custom_jobs.py's
    start_custom_job (see isonet_jobs.py's module docstring) is SLURM
    eligibility -- the concrete, observable difference is that the run goes
    through start_subprocess_job, which means a real OS subprocess (run.proc
    gets set, an actual command string is recorded) rather than an
    in-process asyncio coroutine with a synthetic "<in-process:...>"
    command. "echo hello" stands in for the real conda/isonet.py command
    here (cheap and deterministic), same substitution
    test_start_list_get_a_run in test_main_endpoints.py makes for a real
    RELION job."""
    resp = client.post("/api/runs", json={
        "internal_name": "IsonetPrepareStar",
        "command": "echo hello",
        "subdir": "IsonetPrepareStar/job001",
    })
    assert resp.status_code == 200
    run = resp.json()
    assert run["internal_name"] == "IsonetPrepareStar"
    assert run["command"] == "echo hello"
    run_id = run["run_id"]

    final = _wait_for_status(client, run_id, {"completed", "failed"})
    assert final["status"] == "completed"
    # Presence, not content -- test_start_list_get_a_run in
    # test_main_endpoints.py makes the same choice: stdout capture from the
    # background reader task can genuinely still be in flight the instant
    # status flips to "completed" (both are set by the same _run_subprocess
    # coroutine, but not atomically together), so asserting exact content
    # here would be a race, not a real check of this app's own wiring.
    assert "stdout_lines" in final


def test_start_run_over_http_builds_the_real_command_when_none_is_given(client):
    """A client that never called the draft endpoint first (or one that did,
    then the user never touched a field) can still POST with no `command` --
    main.py's ISONET_JOB_DEFINITIONS branch falls back to building it fresh
    via build_isonet_command, the same as the draft endpoint would have."""
    resp = client.post("/api/runs", json={"internal_name": "IsonetPrepareStar"})
    assert resp.status_code == 200
    run = resp.json()
    assert run["command"].split()[6] == "prepare_star"
    _wait_for_status(client, run["run_id"], {"completed", "failed"})
