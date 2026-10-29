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
import json
import os
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
    be just the copy-star-file prefix (for every job except Prepare Star,
    whose star_file default is a real, non-blank guess) plus the
    conda/isonet.py/subcommand invocation and whatever this module ALWAYS
    needs (output_dir, or star_name for prepare_star) -- no stray flags for
    values that don't differ from doing nothing."""
    definition = ISONET_JOB_DEFINITIONS[internal_name]
    defaults = {opt["key"]: opt.get("default", "") for opt in definition["options"]}
    cmd = build_isonet_command(internal_name, defaults, f"{internal_name}/job001")
    tokens = cmd.split()
    if internal_name == "IsonetPrepareStar":
        # No star_file input at all -- nothing to copy, so no "cp ... &&" prefix.
        assert tokens[:6] == ["conda", "run", "--no-capture-output", "-n", "isonet2_environment", "isonet.py"]
        assert tokens[6] == definition["subcommand"]
        assert "--output_dir" not in tokens
        assert "--star_name" in tokens
    else:
        # Default star_file guess is a real path, so a copy runs first (see
        # build_isonet_command's own docstring for why) -- "cp", src, dst, "&&".
        assert tokens[0] == "cp"
        assert tokens[3] == "&&"
        conda_idx = tokens.index("conda")
        assert tokens[conda_idx:conda_idx + 6] == [
            "conda", "run", "--no-capture-output", "-n", "isonet2_environment", "isonet.py"]
        assert tokens[conda_idx + 6] == definition["subcommand"]
        assert "--output_dir" in tokens
        assert tokens[tokens.index("--output_dir") + 1] == f"{internal_name}/job001/"
        assert "--star_file" in tokens
        assert tokens[tokens.index("--star_file") + 1] == f"{internal_name}/job001/tomograms.star"


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
    # star_file is copied into this job's own directory first; isonet.py is
    # pointed at that COPY, not the raw input -- see the docstring below.
    assert "--star_file" in tokens
    assert tokens[tokens.index("--star_file") + 1] == "IsonetDeconv/job002/tomograms.star"


def test_star_file_is_copied_not_mutated_in_place():
    """The whole point of copying first: deconv/make_mask/predict (and
    denoise/refine's own "with preview" step) all rewrite whatever file
    --star_file points at IN PLACE (confirmed reading IsoNet/utils/utils.py's
    process_tomograms: `starfile.write(new_star, star_path)`, star_path
    being the INPUT path). Pointing isonet.py at a copy inside this job's
    own directory, instead of the user's original input, is what keeps an
    earlier job's tracked output from being silently rewritten -- and keeps
    this job's own result inside ITS OWN directory (Outputs tab / Clean /
    Delete stay honest, matching every other job in this app)."""
    cmd = build_isonet_command(
        "IsonetMakeMask", {"star_file": "IsonetDeconv/job002/tomograms.star"},
        "IsonetMakeMask/job003",
    )
    tokens = cmd.split()
    assert tokens[:4] == [
        "cp", "IsonetDeconv/job002/tomograms.star", "IsonetMakeMask/job003/tomograms.star", "&&",
    ]
    # isonet.py itself must never see the original path anywhere in its own
    # argv -- only the copy.
    isonet_argv = tokens[tokens.index("isonet.py"):]
    assert "IsonetDeconv/job002/tomograms.star" not in isonet_argv
    assert tokens[tokens.index("--star_file") + 1] == "IsonetMakeMask/job003/tomograms.star"


def test_no_star_file_given_means_no_copy_and_no_flag():
    """A draft opened before the user has picked an input star (or one left
    blank) shouldn't try to `cp` an empty path -- confirmed this omits both
    the copy step and the --star_file flag entirely, same as any other
    blank field."""
    cmd = build_isonet_command("IsonetDeconv", {}, "IsonetDeconv/job004")
    tokens = cmd.split()
    assert tokens[0] != "cp"
    assert "--star_file" not in tokens


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
    # Not a fixed token index -- a leading "cp ... &&" (see
    # build_isonet_command's docstring) shifts where the subcommand lands
    # for every job except Prepare Star, whose default star_file guess is a
    # real path.
    assert ISONET_JOB_DEFINITIONS[internal_name]["subcommand"] in definition["draft_command"].split()


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
    full = Path(main.run_manager.project_dir) / "Tomograms"
    full.mkdir()
    (full / "TS_1.mrc").write_bytes(b"")
    resp = client.post("/api/runs", json={"internal_name": "IsonetPrepareStar",
                                          "field_values": {"full": "Tomograms"}})
    assert resp.status_code == 200
    run = resp.json()
    assert run["command"].split()[6] == "prepare_star"
    _wait_for_status(client, run["run_id"], {"completed", "failed"})


# ---------------------------------------------------------------------------
# IsoNet install detection: isonet.py needs PATH *and* PYTHONPATH (what
# isonet2.bashrc sets); a server started without it has neither.
# ---------------------------------------------------------------------------

def _fake_isonet_install(tmp_path):
    root = tmp_path / "IsoNet2"
    (root / "IsoNet" / "bin").mkdir(parents=True)
    (root / "IsoNet" / "bin" / "isonet.py").write_text("#!/usr/bin/env python3\n")
    return root


def test_no_isonet_install_leaves_the_command_unprefixed():
    cmd = build_isonet_command("IsonetPrepareStar", {}, "IsonetPrepareStar/job001")
    assert cmd.startswith("conda run ")


def test_isonet_dir_env_var_adds_path_and_pythonpath(tmp_path, monkeypatch):
    root = _fake_isonet_install(tmp_path)
    monkeypatch.setenv("ISONET_DIR", str(root))
    cmd = build_isonet_command("IsonetPrepareStar", {}, "IsonetPrepareStar/job001")
    assert cmd.startswith(f'PATH={root}/IsoNet/bin:"$PATH" PYTHONPATH={root}${{PYTHONPATH:+:$PYTHONPATH}} conda run ')


def test_install_found_from_a_path_entry_like_the_users_bashrc(tmp_path, monkeypatch):
    """The reported case: only IsoNet/bin was exported, no PYTHONPATH --
    PYTHONPATH is derived from it so `import IsoNet` works."""
    root = _fake_isonet_install(tmp_path)
    monkeypatch.setenv("PATH", f"{root}/IsoNet/bin/{os.pathsep}{os.environ['PATH']}")
    from isonet_jobs import find_isonet_dir
    assert find_isonet_dir() == str(root)


def test_install_found_from_pythonpath(tmp_path, monkeypatch):
    root = _fake_isonet_install(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(root))
    from isonet_jobs import find_isonet_dir
    assert find_isonet_dir() == str(root)


def test_prefix_goes_after_the_input_copy_and_applies_to_conda_only(tmp_path, monkeypatch):
    root = _fake_isonet_install(tmp_path)
    monkeypatch.setenv("ISONET_DIR", str(root))
    cmd = build_isonet_command("IsonetDeconv", {"star_file": "A/job001/tomograms.star"}, "IsonetDeconv/job002")
    assert cmd.startswith("cp A/job001/tomograms.star IsonetDeconv/job002/tomograms.star && PATH=")


def test_detected_install_runs_isonet_under_conda_with_a_bare_server_env(tmp_path, monkeypatch):
    """The command works when the SERVER env has neither variable set: the
    `VAR=... cmd` form must actually reach the child (sh semantics)."""
    import subprocess
    root = _fake_isonet_install(tmp_path)
    monkeypatch.setenv("ISONET_DIR", str(root))
    from isonet_jobs import _isonet_env_assignments
    probe = " ".join(_isonet_env_assignments()) + ' sh -c \'echo "$PATH|$PYTHONPATH"\''
    out = subprocess.run(["/bin/sh", "-c", probe], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"}).stdout.strip()
    path, pythonpath = out.split("|")
    assert path.startswith(f"{root}/IsoNet/bin:") and "/usr/bin" in path
    assert pythonpath == str(root)


def test_install_found_from_the_isoapp_gui_config(tmp_path, monkeypatch):
    """The reported case: the server's environment has nothing, but IsoNet's
    GUI saved its install path in ~/.config/isoapp/environment.json."""
    root = _fake_isonet_install(tmp_path)
    cfg = tmp_path / "cfg" / "isoapp"
    cfg.mkdir(parents=True)
    (cfg / "environment.json").write_text(json.dumps({"condaEnv": "x", "IsoNetPath": str(root)}))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from isonet_jobs import find_isonet_dir
    assert find_isonet_dir() == str(root)
    cmd = build_isonet_command("IsonetPrepareStar", {}, "IsonetPrepareStar/job001")
    assert f"PYTHONPATH={root}" in cmd


def test_a_stale_or_garbled_isoapp_config_is_ignored(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg" / "isoapp"
    cfg.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from isonet_jobs import find_isonet_dir
    for text in ("not json", json.dumps({"IsoNetPath": "/nonexistent"}), json.dumps({"IsoNetPath": ""}), "[]"):
        (cfg / "environment.json").write_text(text)
        assert find_isonet_dir() is None


# ---------------------------------------------------------------------------
# Prepare Star from a RELION tomograms STAR. RELION keeps full tomograms and
# both half-maps in ONE folder; isonet.py prepare_star wants one folder per
# kind and pairs them by sorted name, so pointing it at RELION's folder gave a
# star whose rows were a mix of full/half1/half2 files (reported on a real
# project: "Number of tomograms: 23" for 8 tomograms, Half1 == Half2).
# ---------------------------------------------------------------------------

import pandas as pd
import starfile

from converters import stage_isonet_tomograms as stager
from isonet_jobs import STAGED_DIRNAME, validate_isonet_fields


def _relion_tomograms(project, names=("TS_001", "TS_002"), full=True, halves=True, folder="Tomograms/job018/tomograms"):
    """A RELION-style project: one folder holding <n>.mrc, <n>_half1.mrc and
    <n>_half2.mrc, and the tomograms.star that says which is which."""
    d = project / folder
    d.mkdir(parents=True, exist_ok=True)
    rows = {"rlnTomoName": list(names)}
    for n in names:
        for suffix in ("", "_half1", "_half2"):
            (d / f"{n}{suffix}.mrc").write_bytes(b"x")
    if full:
        rows["rlnTomoReconstructedTomogram"] = [f"{folder}/{n}.mrc" for n in names]
    if halves:
        rows["rlnTomoReconstructedTomogramHalf1"] = [f"{folder}/{n}_half1.mrc" for n in names]
        rows["rlnTomoReconstructedTomogramHalf2"] = [f"{folder}/{n}_half2.mrc" for n in names]
    star = project / "Tomograms" / "job018" / "tomograms.star"
    starfile.write({"global": pd.DataFrame(rows)}, star, overwrite=True)
    return star


def test_stage_makes_one_folder_per_kind_with_matching_names(tmp_path):
    star = _relion_tomograms(tmp_path)
    made = stager.stage(star, tmp_path / "out", project_dir=tmp_path)
    assert set(made) == {"full", "even", "odd"}
    for kind, suffix in (("full", ""), ("even", "_half1"), ("odd", "_half2")):
        folder = tmp_path / "out" / kind
        assert sorted(p.name for p in folder.iterdir()) == ["TS_001.mrc", "TS_002.mrc"]
        assert (folder / "TS_001.mrc").resolve() == (tmp_path / f"Tomograms/job018/tomograms/TS_001{suffix}.mrc").resolve()


def test_stage_rerun_replaces_its_own_links_only(tmp_path):
    star = _relion_tomograms(tmp_path)
    stager.stage(star, tmp_path / "out", project_dir=tmp_path)
    stager.stage(star, tmp_path / "out", project_dir=tmp_path)           # a re-run is fine
    (tmp_path / "out" / "even" / "TS_001.mrc").unlink()
    (tmp_path / "out" / "even" / "TS_001.mrc").write_bytes(b"a real file")
    with pytest.raises(FileExistsError):
        stager.stage(star, tmp_path / "out", project_dir=tmp_path)
    assert (tmp_path / "out" / "even" / "TS_001.mrc").read_bytes() == b"a real file"


def test_stage_only_makes_the_kinds_the_star_has(tmp_path):
    only_halves = _relion_tomograms(tmp_path, full=False)
    assert set(stager.stage(only_halves, tmp_path / "a", project_dir=tmp_path)) == {"even", "odd"}
    only_full = _relion_tomograms(tmp_path, halves=False)
    assert set(stager.stage(only_full, tmp_path / "b", project_dir=tmp_path)) == {"full"}


def test_stage_alternative_full_column(tmp_path):
    star = _relion_tomograms(tmp_path)
    df = starfile.read(star, always_dict=True)["global"]
    df["rlnTomoReconstructedTomogramDenoised"] = [f"Tomograms/job018/tomograms/{n}_half1.mrc" for n in df["rlnTomoName"]]
    starfile.write({"global": df}, star, overwrite=True)
    stager.stage(star, tmp_path / "out", project_dir=tmp_path, full_column="rlnTomoReconstructedTomogramDenoised")
    assert (tmp_path / "out" / "full" / "TS_001.mrc").resolve().name == "TS_001_half1.mrc"


def test_stage_errors_are_plain(tmp_path):
    star = _relion_tomograms(tmp_path)
    df = starfile.read(star, always_dict=True)["global"].drop(columns=["rlnTomoReconstructedTomogramHalf2"])
    df = df.drop(columns=["rlnTomoReconstructedTomogram"])
    starfile.write({"global": df}, star, overwrite=True)
    with pytest.raises(ValueError, match="only one of the two half-map columns"):
        stager.columns_for(star)
    broken = _relion_tomograms(tmp_path, folder="Tomograms/job018/tomograms")
    (tmp_path / "Tomograms/job018/tomograms/TS_002.mrc").unlink()
    with pytest.raises(FileNotFoundError, match="TS_002"):
        stager.stage(broken, tmp_path / "out2", project_dir=tmp_path)
    assert stager.main(["--star", str(broken), "--out", str(tmp_path / "out3"), "--project_dir", str(tmp_path)]) == 1


def test_prepare_star_command_stages_first_then_points_isonet_at_the_staged_folders(tmp_path):
    star = _relion_tomograms(tmp_path)
    cmd = build_isonet_command("IsonetPrepareStar", {"relion_star": "Tomograms/job018/tomograms.star"},
                               "IsonetPrepareStar/job033", project_dir=tmp_path)
    stage_part, isonet_part = cmd.split(" && ")
    assert "stage_isonet_tomograms.py" in stage_part
    assert "--star Tomograms/job018/tomograms.star --out IsonetPrepareStar/job033/staged" in stage_part
    tokens = isonet_part.split()
    for kind in ("full", "even", "odd"):
        assert tokens[tokens.index(f"--{kind}") + 1] == f"IsonetPrepareStar/job033/{STAGED_DIRNAME}/{kind}"
    assert "create_average" not in cmd and "--relion_star" not in cmd


def test_prepare_star_command_passes_only_what_the_star_has(tmp_path):
    _relion_tomograms(tmp_path, full=False)
    cmd = build_isonet_command("IsonetPrepareStar", {"relion_star": "Tomograms/job018/tomograms.star"},
                               "IsonetPrepareStar/job033", project_dir=tmp_path)
    assert "--even" in cmd and "--odd" in cmd and "--full" not in cmd


def test_prepare_star_command_with_a_denoised_full_column(tmp_path):
    _relion_tomograms(tmp_path)
    cmd = build_isonet_command("IsonetPrepareStar", {"relion_star": "t.star",
                                                     "relion_tomo_column": "rlnTomoReconstructedTomogramDenoised"},
                               "IsonetPrepareStar/job033", project_dir=tmp_path)
    assert "--full_column rlnTomoReconstructedTomogramDenoised" in cmd.split(" && ")[0]


def test_prepare_star_folder_mode_is_unchanged():
    cmd = build_isonet_command("IsonetPrepareStar", {"full": "A/full"}, "IsonetPrepareStar/job001")
    assert cmd.startswith("conda run ") and "--full A/full" in cmd and "stage_isonet" not in cmd


def test_validation_accepts_a_relion_star_and_proper_folders(tmp_path):
    _relion_tomograms(tmp_path)
    assert validate_isonet_fields("IsonetPrepareStar", {"relion_star": "Tomograms/job018/tomograms.star"}, tmp_path) == []
    for k in ("full", "even", "odd"):
        (tmp_path / k).mkdir()
        (tmp_path / k / "a.mrc").write_bytes(b"")
    assert validate_isonet_fields("IsonetPrepareStar", {"full": "full", "even": "even", "odd": "odd"}, tmp_path) == []


def test_validation_rejects_the_same_folder_as_even_and_odd(tmp_path):
    """The reported case: --even and --odd both Tomograms/job018/tomograms/."""
    _relion_tomograms(tmp_path)
    problems = validate_isonet_fields("IsonetPrepareStar",
                                      {"even": "Tomograms/job018/tomograms/", "odd": "Tomograms/job018/tomograms"}, tmp_path)
    assert any("same folder" in p for p in problems)
    assert any("mixes full tomograms" in p for p in problems)


def test_validation_rejects_a_folder_mixing_full_and_half_maps(tmp_path):
    _relion_tomograms(tmp_path)
    problems = validate_isonet_fields("IsonetPrepareStar", {"full": "Tomograms/job018/tomograms"}, tmp_path)
    assert len(problems) == 1 and "RELION tomograms STAR" in problems[0]


def test_validation_folder_counts_and_pairing(tmp_path):
    for k, n in (("even", 2), ("odd", 3)):
        (tmp_path / k).mkdir()
        for i in range(n):
            (tmp_path / k / f"t{i}.mrc").write_bytes(b"")
    problems = validate_isonet_fields("IsonetPrepareStar", {"even": "even", "odd": "odd"}, tmp_path)
    assert any("different numbers of files" in p for p in problems)
    assert any("together" in p for p in validate_isonet_fields("IsonetPrepareStar", {"even": "even"}, tmp_path))


def test_validation_requires_some_input_and_not_two_kinds(tmp_path):
    _relion_tomograms(tmp_path)
    assert validate_isonet_fields("IsonetPrepareStar", {}, tmp_path)
    assert validate_isonet_fields("IsonetPrepareStar", {"relion_star": "Tomograms/job018/tomograms.star",
                                                        "full": "Tomograms"}, tmp_path)
    assert any("not found" in p for p in validate_isonet_fields("IsonetPrepareStar", {"relion_star": "nope.star"}, tmp_path))
    assert validate_isonet_fields("IsonetDeconv", {}, tmp_path) == []


def test_run_over_http_rejects_a_mixed_folder_before_creating_the_job(client):
    project = Path(main.run_manager.project_dir)
    _relion_tomograms(project)
    resp = client.post("/api/runs", json={"internal_name": "IsonetPrepareStar",
                                          "field_values": {"even": "Tomograms/job018/tomograms",
                                                           "odd": "Tomograms/job018/tomograms"}})
    assert resp.status_code == 400 and "same folder" in resp.json()["detail"]
    assert not (project / "IsonetPrepareStar").exists()


# ---------------------------------------------------------------------------
# snrfalloff: IsoNet's own default is 1 for deconv but 0 for denoise/refine.
# A default equal to the UI default is left off the command, so a UI default
# of 1.0 on Denoise/Refine showed 1.0 and ran with 0.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,real_default", [("IsonetDeconv", 1.0), ("IsonetDenoise", 0.0), ("IsonetRefine", 0.0)])
def test_snrfalloff_default_matches_isonet(name, real_default):
    option = next(o for o in ISONET_JOB_DEFINITIONS[name]["options"] if o["key"] == "snrfalloff")
    assert option["default"] == real_default
    cmd = build_isonet_command(name, {"star_file": "X/tomograms.star"}, f"{name}/job001")
    assert "--snrfalloff" not in cmd                              # equals IsoNet's default: left off
    other = 1.0 if real_default == 0.0 else 0.0
    assert f"--snrfalloff {other}" in build_isonet_command(
        name, {"star_file": "X/tomograms.star", "snrfalloff": other}, f"{name}/job001")


@pytest.mark.skipif(not (Path.home() / "IsoNet2-IsoNet-2.0.0-beta/IsoNet/bin/isonet.py").is_file(),
                    reason="needs a real IsoNet2 install")
def test_every_default_matches_the_real_isonet_signature():
    """Read isonet.py's own signatures: any option whose UI default differs
    from IsoNet's (beyond float/str spelling) would silently run differently
    from what the popup shows."""
    import ast
    src = (Path.home() / "IsoNet2-IsoNet-2.0.0-beta/IsoNet/bin/isonet.py").read_text()
    sigs = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef):
            a = node.args
            names = [x.arg for x in a.args]
            defaults = [None] * (len(names) - len(a.defaults)) + [ast.literal_eval(d) for d in a.defaults]
            sigs[node.name] = dict(zip(names, defaults))
    def same(ui, real):
        if real is None:
            return str(ui).strip() == ""
        if isinstance(real, list):                      # defocus: [10000] <-> "10000"
            return str(ui).strip() == ",".join(str(x) for x in real)
        try:
            return float(ui) == float(real)
        except (TypeError, ValueError):
            return str(ui) == str(real)
    for name, d in ISONET_JOB_DEFINITIONS.items():
        real = sigs[d["subcommand"]]
        for o in d["options"]:
            if o["key"] in real and o["key"] != "star_file" and "default" in o and not isinstance(o["default"], bool):
                assert same(o["default"], real[o["key"]]), (name, o["key"], o["default"], real[o["key"]])


def _real_mrc_tomograms(project, voxel=10.0):
    import mrcfile
    import numpy as np
    folder = project / "Tomograms" / "job018" / "tomograms"
    folder.mkdir(parents=True)
    for n, base in (("TS_001", 1.0), ("TS_002", 5.0)):
        for suffix, add in (("_half1", 0.0), ("_half2", 2.0)):
            with mrcfile.new(folder / f"{n}{suffix}.mrc", overwrite=True) as m:
                m.set_data(np.full((4, 5, 6), base + add, dtype=np.float32))
                m.voxel_size = voxel
    star = project / "Tomograms" / "job018" / "tomograms.star"
    names = ["TS_001", "TS_002"]
    starfile.write({"global": pd.DataFrame({
        "rlnTomoName": names,
        "rlnTomoReconstructedTomogramHalf1": [f"Tomograms/job018/tomograms/{n}_half1.mrc" for n in names],
        "rlnTomoReconstructedTomogramHalf2": [f"Tomograms/job018/tomograms/{n}_half2.mrc" for n in names]})},
        star, overwrite=True)
    return star


def test_stage_average_sums_the_halves_into_the_job_folder(tmp_path):
    import mrcfile
    star = _real_mrc_tomograms(tmp_path, voxel=10.0)
    made = stager.stage(star, tmp_path / "out", project_dir=tmp_path, average=True)
    assert set(made) == {"full", "even", "odd"}
    full = tmp_path / "out" / "full" / "TS_002.mrc"
    assert not full.is_symlink()
    with mrcfile.open(full) as m:
        assert float(m.data[0, 0, 0]) == 5.0 + 7.0 and m.data.dtype.name == "float32"
        assert float(m.voxel_size.x) == 10.0
    stager.stage(star, tmp_path / "out", project_dir=tmp_path, average=True)      # a re-run replaces them
    assert sorted(p.name for p in (tmp_path / "out" / "full").iterdir()) == ["TS_001.mrc", "TS_002.mrc"]


def test_stage_without_average_leaves_a_half_only_star_without_full(tmp_path):
    star = _real_mrc_tomograms(tmp_path)
    assert set(stager.stage(star, tmp_path / "out", project_dir=tmp_path)) == {"even", "odd"}


def test_prepare_star_command_average_option_goes_to_the_staging_step(tmp_path):
    _real_mrc_tomograms(tmp_path)
    star = "Tomograms/job018/tomograms.star"
    plain = build_isonet_command("IsonetPrepareStar", {"relion_star": star}, "IsonetPrepareStar/job033", project_dir=tmp_path)
    avg = build_isonet_command("IsonetPrepareStar", {"relion_star": star, "create_average": True},
                               "IsonetPrepareStar/job033", project_dir=tmp_path)
    assert "--full" not in plain and "--average" not in plain
    stage_part, isonet_part = avg.split(" && ")
    assert "--average" in stage_part
    assert "--full IsonetPrepareStar/job033/staged/full" in isonet_part and "--create_average" not in avg


# ---------------------------------------------------------------------------
# Deconv on a Prepare Star output that has no full tomograms (reported:
# FileNotFoundError: 'None' eight rows into a run).
# ---------------------------------------------------------------------------

def _prepared_star(tmp_path, tomo_names):
    path = tmp_path / "IsonetPrepareStar" / "job033" / "tomograms.star"
    path.parent.mkdir(parents=True)
    starfile.write(pd.DataFrame({"rlnIndex": range(1, len(tomo_names) + 1), "rlnTomoName": tomo_names,
                                 "rlnPixelSize": [10.0] * len(tomo_names)}), path, overwrite=True)
    return "IsonetPrepareStar/job033/tomograms.star"


def test_deconv_refuses_a_star_whose_full_tomograms_are_none(tmp_path):
    star = _prepared_star(tmp_path, ["None", "None", "None"])
    problems = validate_isonet_fields("IsonetDeconv", {"star_file": star}, tmp_path)
    assert len(problems) == 1
    assert "1, 2, 3" in problems[0] and "3 of 3" in problems[0] and "averaged full tomograms" in problems[0]


def test_deconv_only_checks_the_rows_it_will_process(tmp_path):
    star = _prepared_star(tmp_path, ["a.mrc", "None", "c.mrc"])
    for n in ("a.mrc", "c.mrc"):
        (tmp_path / n).write_bytes(b"")
    assert validate_isonet_fields("IsonetDeconv", {"star_file": star, "tomo_idx": "1,3"}, tmp_path) == []
    assert validate_isonet_fields("IsonetDeconv", {"star_file": star, "tomo_idx": "2"}, tmp_path)
    assert validate_isonet_fields("IsonetDeconv", {"star_file": star, "tomo_idx": "1-3"}, tmp_path)


def test_deconv_accepts_a_good_star_and_ignores_one_that_isnt_there_yet(tmp_path):
    star = _prepared_star(tmp_path, ["a.mrc", "b.mrc"])
    for n in ("a.mrc", "b.mrc"):
        (tmp_path / n).write_bytes(b"")
    assert validate_isonet_fields("IsonetDeconv", {"star_file": star}, tmp_path) == []
    assert validate_isonet_fields("IsonetDeconv", {"star_file": "IsonetPrepareStar/job099/tomograms.star"}, tmp_path) == []
    assert validate_isonet_fields("IsonetDeconv", {}, tmp_path) == []


def test_deconv_names_a_missing_input_column(tmp_path):
    star = _prepared_star(tmp_path, ["a.mrc"])
    problems = validate_isonet_fields("IsonetDeconv", {"star_file": star, "input_column": "rlnNope"}, tmp_path)
    assert problems and "rlnNope" in problems[0]


def test_the_same_number_written_differently_is_still_the_default():
    """The popup stored "1" for a slider whose default is 1.0, which put a
    redundant --snrfalloff 1 --deconvstrength 1 on the command."""
    cmd = build_isonet_command("IsonetDeconv", {"star_file": "X/tomograms.star", "snrfalloff": "1", "deconvstrength": 1},
                               "IsonetDeconv/job001")
    assert "--snrfalloff" not in cmd and "--deconvstrength" not in cmd
    assert "--snrfalloff 2" in build_isonet_command(
        "IsonetDeconv", {"star_file": "X/tomograms.star", "snrfalloff": "2"}, "IsonetDeconv/job001")


def test_deconv_refuses_tomogram_paths_that_do_not_exist(tmp_path):
    """The STAR looks fine but its files are gone (a deleted/cleaned Prepare
    Star job, or a STAR copied from another project)."""
    star = _prepared_star(tmp_path, ["IsonetPrepareStar/job033/staged/full/a.mrc", "b.mrc"])
    (tmp_path / "b.mrc").write_bytes(b"")
    problems = validate_isonet_fields("IsonetDeconv", {"star_file": star}, tmp_path)
    assert len(problems) == 1 and "1 of 2" in problems[0] and "staged/full/a.mrc" in problems[0]
    assert validate_isonet_fields("IsonetDeconv", {"star_file": star, "tomo_idx": "2"}, tmp_path) == []


# ---------------------------------------------------------------------------
# Make Mask's Progress tab: GET /api/runs/{id}/mask-preview (see mask_preview.py)
# ---------------------------------------------------------------------------

def _start_trivial_run(client, internal_name, field_values=None):
    """A real run of `internal_name` whose command is harmless, so there is a
    run record with a job directory to put masks in."""
    resp = client.post("/api/runs", json={"internal_name": internal_name, "command": "true",
                                          "field_values": field_values or {}})
    assert resp.status_code == 200, resp.text
    run = resp.json()
    _wait_for_status(client, run["run_id"], {"completed", "failed"})
    return run


def test_mask_preview_endpoint_draws_the_first_mask_of_a_make_mask_run(client):
    import mrcfile
    import numpy as np
    project = Path(main.run_manager.project_dir)
    run = _start_trivial_run(client, "IsonetMakeMask")
    job = Path(run["cwd"])
    assert client.get(f"/api/runs/{run['run_id']}/mask-preview").json()["available"] is False

    tomo = project / "Tomo" / "TS_1.mrc"
    tomo.parent.mkdir()
    for path, data in ((tomo, np.random.default_rng(1).normal(size=(6, 10, 12)).astype(np.float32)),
                       (job / "TS_1_mask.mrc", np.ones((6, 10, 12), dtype=np.uint16))):
        with mrcfile.new(path, overwrite=True) as m:
            m.set_data(data)
            m.voxel_size = 10.0
    old = time.time() - 30
    os.utime(job / "TS_1_mask.mrc", (old, old))
    starfile.write(pd.DataFrame({"rlnTomoName": ["Tomo/TS_1.mrc"], "rlnDeconvTomoName": ["Tomo/TS_1.mrc"],
                                 "rlnPixelSize": [10.0]}), job / "tomograms.star", overwrite=True)

    body = client.get(f"/api/runs/{run['run_id']}/mask-preview").json()
    assert body["available"] and body["mask"] == "TS_1_mask.mrc" and body["mask_percent"] == 100.0
    assert body["tomogram"].endswith("Tomo/TS_1.mrc") and body["masks_total"] == 1
    png = client.get(f"/api/runs/{run['run_id']}/files/download", params={"path": body["file"]})
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"


def test_mask_preview_endpoint_is_unsupported_for_other_jobs_and_unknown_runs(client):
    run = _start_trivial_run(client, "IsonetDeconv")
    assert client.get(f"/api/runs/{run['run_id']}/mask-preview").json() == {"supported": False, "available": False}
    assert client.get("/api/runs/nope/mask-preview").status_code == 404


# ---------------------------------------------------------------------------
# A command edited after the form drew it up is flagged on the run (reported:
# a job ran from a hand-edited command -- patch size 64 -- while the recorded
# form said 96, so cloning it reproduced a job that ran out of GPU memory).
# ---------------------------------------------------------------------------

def test_a_run_records_whether_its_command_was_edited(client):
    plain = client.post("/api/runs", json={"internal_name": "IsonetDeconv", "command": "true"}).json()
    assert plain["command_edited"] is False
    edited = client.post("/api/runs", json={"internal_name": "IsonetDeconv", "command": "true",
                                            "command_edited": True}).json()
    assert edited["command_edited"] is True
    for run in (plain, edited):
        _wait_for_status(client, run["run_id"], {"completed", "failed"}, timeout_s=20)
    history = {h["run_id"]: h for h in project_manager.load_history(Path(main.run_manager.project_dir))}
    assert history[edited["run_id"]]["command_edited"] is True
    assert history[plain["run_id"]]["command_edited"] is False
    assert client.get(f"/api/runs/{edited['run_id']}").json()["command_edited"] is True


def test_the_flag_survives_a_restart_and_an_overwrite_is_judged_afresh(client):
    run = client.post("/api/runs", json={"internal_name": "IsonetDeconv", "command": "true",
                                         "command_edited": True}).json()
    _wait_for_status(client, run["run_id"], {"completed", "failed"}, timeout_s=20)
    main.run_manager.runs.clear()                               # as after a restart: only the history remains
    assert client.get(f"/api/runs/{run['run_id']}").json()["command_edited"] is True
    again = client.post("/api/runs", json={"internal_name": "IsonetDeconv", "command": "true",
                                           "overwrite_run_id": run["run_id"], "command_edited": False}).json()
    assert again["command_edited"] is False
