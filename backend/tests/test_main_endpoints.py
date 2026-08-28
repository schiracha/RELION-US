"""
HTTP-level tests for main.py's FastAPI routes (issue #39).

Before this file, every route in main.py was exercised only indirectly --
unit tests hit JobRunManager/project_manager/etc. directly, and the
Playwright suites at the repo root drive a real browser -- so a change that
broke an endpoint's status code, response shape, or request wiring (a typo
in a route decorator, a wrong status code, a field renamed on one side of
the fetch() call but not the other) could pass every existing test. This
file uses FastAPI's TestClient to call the real ASGI app in-process, no
mocks, covering the run lifecycle and the error-path status codes a browser
suite tends to never exercise (they mostly click through the happy path).

This is the first TestClient usage in the repo (see the module docstring
below for why `main.run_manager` -- a module-level singleton -- needs a
dedicated per-test wiring trick, not just `tmp_path` on its own).
"""
import asyncio
import sys
import time
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
import project_manager


# ---------------------------------------------------------------------------
# A RELION-native project, built by hand the same way
# test_relion_project_adoption.py's own `relion_project` fixture does --
# reused here (not imported) so this file's 409-on-a-RELION-run coverage
# doesn't depend on that file's fixture staying shaped the same way. Schema
# verified against RELION's own source, per that file's module docstring:
#   * default_pipeline.star -> PipeLine::write(), src/pipeliner.cpp
#   * status labels         -> procstatus_type2label, src/pipeline_jobs.h
# ---------------------------------------------------------------------------
RELION_PIPELINE_STAR = """
# version 30001

data_pipeline_general

_rlnPipeLineJobCounter                      2


# version 30001

data_pipeline_processes

loop_
_rlnPipeLineProcessName #1
_rlnPipeLineProcessAlias #2
_rlnPipeLineProcessTypeLabel #3
_rlnPipeLineProcessStatusLabel #4
Import/job001/           None            relion.import.movies     Succeeded
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient wired to a fresh, empty tmp_path project.

    Isolates every test from this dev machine's real
    ~/.config/relion_us -- which, on this box, has password protection
    turned ON (a real auth.json with "enabled": true) -- by redirecting
    XDG_CONFIG_HOME into tmp_path (project_manager.config_root() honours
    it; see its own docstring). Without this, main.py's auth_gate
    middleware would 401 every /api/ request a plain unauthenticated
    TestClient makes, since it reads that config fresh on every request.

    main.run_manager is a MODULE-LEVEL SINGLETON, built once at import
    time from the process's cwd (main.py:204, `run_manager =
    JobRunManager(PROJECT_DIR)`). Since `main` is only ever imported once
    per test process, per-test isolation can't come from cwd or env vars
    read at import time -- instead this calls set_project_dir() to repoint
    the already-constructed singleton at a fresh tmp_path project, exactly
    the mechanism POST /api/project/switch uses at runtime (main.py:868-869)
    to move the whole app to a different project without restarting it.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    project_dir = tmp_path / "project"
    project_manager.init_new_project(project_dir)
    main.run_manager.set_project_dir(project_dir)
    return TestClient(main.app)


@pytest.fixture
def relion_native_run_id(client):
    """Hand-writes a RELION-native project (pipeline file + job directory,
    no run ever started through this app) into the client's own project
    directory, then returns the run_id the Command Center assigns it
    ("relion:job001" -- see JobRunManager.is_relion_run). Used by the 409
    tests below: _reject_relion_run is exactly the gate that keeps this
    app from touching a job it never ran and has no safe way to describe
    consistently with RELION's own pipeline record."""
    project_dir = main.run_manager.project_dir
    (project_dir / "default_pipeline.star").write_text(RELION_PIPELINE_STAR)
    (project_dir / "Import" / "job001").mkdir(parents=True)
    return "relion:job001"


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


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


def test_start_list_get_a_run(client):
    """POST /api/runs -> the run shows up in both GET /api/runs (the
    Command Center list) and GET /api/runs/{id} (its own detail view),
    with the exact command this app was asked to run -- the same
    field/command shape test_job_runner.py's own start_subprocess_job
    tests use directly against JobRunManager, just now over real HTTP."""
    resp = client.post("/api/runs", json={
        "internal_name": "Import",
        "command": "echo hello",
        "subdir": "Import/job001",
    })
    assert resp.status_code == 200
    run = resp.json()
    assert run["internal_name"] == "Import"
    assert run["command"] == "echo hello"
    run_id = run["run_id"]

    listed = client.get("/api/runs")
    assert listed.status_code == 200
    assert any(r["run_id"] == run_id for r in listed.json())

    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["run_id"] == run_id
    # GET .../{id} carries stdout/stderr lines the summary-only list
    # endpoint doesn't -- this is the field the Command Center's log
    # panel actually reads.
    assert "stdout_lines" in detail.json()

    _wait_for_status(client, run_id, {"completed", "failed"})


def test_abort_a_running_job(client):
    """POST .../abort against a job that's actually still running --
    unlike the 409 error-path test below, this exercises the success
    branch: a still-alive `sleep` gets killed and the run's own status
    flips to "aborted".

    This deliberately does NOT use the synchronous `client` fixture's
    TestClient for the actual calls (only for its project-directory setup):
    that TestClient drives each request through its own event-loop portal,
    which -- confirmed while writing this test -- forces the background
    `asyncio.create_task` started by POST /api/runs (job_runner.py's
    `_run_subprocess`) to run to full completion (all 30 real seconds of
    `sleep 30`) before the FIRST synchronous call even returns, so a
    second synchronous call always finds it already terminal, never
    "running". Driving both calls from one `asyncio.run()` on a single
    persistent event loop (the same pattern test_job_runner.py's own tests
    use directly against JobRunManager, see its module docstring) lets the
    launched task actually run concurrently with the test, the way it does
    for a real client talking to a real running server.
    """
    async def go():
        transport = httpx2.ASGITransport(app=main.app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            resp = await ac.post("/api/runs", json={
                "internal_name": "Import",
                "command": "sleep 30",
                "subdir": "Import/job001",
            })
            run_id = resp.json()["run_id"]
            # Wait for the launcher to actually spawn the process -- there's
            # a real window, by design, between start_subprocess_job()
            # returning and run.proc existing (see abort_run's own PENDING
            # comment) -- so this exercises the "kill a live process" branch
            # specifically, not the earlier "cancel before spawn" one.
            for _ in range(150):
                run = main.run_manager.runs.get(run_id)
                if run is not None and run.proc is not None:
                    break
                await asyncio.sleep(0.02)
            assert run is not None and run.proc is not None, "process never spawned"

            abort_resp = await ac.post(f"/api/runs/{run_id}/abort")

            for _ in range(150):
                run = main.run_manager.runs.get(run_id)
                if run is not None and run.status == "aborted":
                    break
                await asyncio.sleep(0.02)
            return abort_resp, run

    abort_resp, run = asyncio.run(go())
    assert abort_resp.status_code == 200
    assert abort_resp.json() == {"ok": True}
    assert run.status == "aborted"


def test_delete_a_finished_run(client):
    """DELETE /api/runs/{id} removes a finished run from the Command
    Center list -- confirmed here by its own GET going 404 afterward, not
    just by the delete response's own "ok" flag."""
    resp = client.post("/api/runs", json={
        "internal_name": "Import",
        "command": "echo hello",
        "subdir": "Import/job001",
    })
    run_id = resp.json()["run_id"]
    _wait_for_status(client, run_id, {"completed", "failed"})

    delete_resp = client.delete(f"/api/runs/{run_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json()["ok"] is True

    assert client.get(f"/api/runs/{run_id}").status_code == 404


# ---------------------------------------------------------------------------
# Error-path status codes
# ---------------------------------------------------------------------------


def test_unknown_run_id_404s_on_get(client):
    resp = client.get("/api/runs/no-such-run")
    assert resp.status_code == 404


def test_unknown_run_id_409s_on_abort(client):
    """abort_run's own ValueError-free "not found" path returns False,
    which main.py maps to 409 (not 404) -- see abort_run's own comment
    "Run is not currently running (or doesn't exist)": an unknown id and
    an already-finished run are deliberately indistinguishable here,
    since neither is something the Abort button can act on."""
    resp = client.post("/api/runs/no-such-run/abort")
    assert resp.status_code == 409
    assert "not currently running" in resp.json()["detail"]


def test_unknown_run_id_404s_on_delete(client):
    resp = client.delete("/api/runs/no-such-run")
    assert resp.status_code == 404


def test_abort_an_already_finished_run_returns_409(client):
    """The exact message match matters here: this is the one case where an
    unknown run_id and an already-finished run share a status code and
    detail string on purpose (see abort_run's own docstring reasoning,
    tested separately above for the unknown-id case)."""
    resp = client.post("/api/runs", json={
        "internal_name": "Import",
        "command": "echo hello",
        "subdir": "Import/job001",
    })
    run_id = resp.json()["run_id"]
    _wait_for_status(client, run_id, {"completed", "failed"})

    abort_resp = client.post(f"/api/runs/{run_id}/abort")
    assert abort_resp.status_code == 409
    assert abort_resp.json()["detail"] == "Run is not currently running (or doesn't exist)"


def test_patch_with_no_fields_is_400(client):
    """update_run validates everything before writing anything -- an empty
    PATCH body (no alias, note, or status) is the one input that can't be
    partially applied, so it's rejected outright rather than silently
    succeeding as a no-op."""
    resp = client.post("/api/runs", json={
        "internal_name": "Import",
        "command": "echo hello",
        "subdir": "Import/job001",
    })
    run_id = resp.json()["run_id"]

    patch_resp = client.patch(f"/api/runs/{run_id}", json={})
    assert patch_resp.status_code == 400
    assert "Nothing to update" in patch_resp.json()["detail"]


def test_patch_with_invalid_status_is_400(client):
    resp = client.post("/api/runs", json={
        "internal_name": "Import",
        "command": "echo hello",
        "subdir": "Import/job001",
    })
    run_id = resp.json()["run_id"]

    patch_resp = client.patch(f"/api/runs/{run_id}", json={"status": "not-a-real-status"})
    assert patch_resp.status_code == 400
    assert "status must be one of" in patch_resp.json()["detail"]


def test_patch_status_on_a_relion_native_run_is_409(client, relion_native_run_id):
    """_reject_relion_run's whole reason to exist: this app has no safe way
    to make RELION's own pipeline record agree with a manually-forced
    "completed"/"failed" on a job RELION itself ran, so status edits on a
    "relion:jobNNN" id are blocked outright (unlike alias/note, which are
    this app's own local metadata -- see update_run's own docstring)."""
    resp = client.patch(f"/api/runs/{relion_native_run_id}", json={"status": "completed"})
    assert resp.status_code == 409
    assert "run in RELION itself" in resp.json()["detail"]


def test_abort_a_relion_native_run_is_409(client, relion_native_run_id):
    resp = client.post(f"/api/runs/{relion_native_run_id}/abort")
    assert resp.status_code == 409
    assert "run in RELION itself" in resp.json()["detail"]


def test_delete_a_relion_native_run_is_409(client, relion_native_run_id):
    resp = client.delete(f"/api/runs/{relion_native_run_id}")
    assert resp.status_code == 409
    assert "run in RELION itself" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Simple read-only endpoints
# ---------------------------------------------------------------------------


def test_get_project_reports_the_current_project_dir(client):
    """The Project panel's own source of truth: whatever set_project_dir
    last pointed the singleton at (this fixture's tmp_path project), not
    the module's original startup PROJECT_DIR."""
    resp = client.get("/api/project")
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == str(main.run_manager.project_dir)
    assert body["is_relion_project"] is True
    assert body["history"] == []


def test_get_settings_returns_a_settings_dict(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    assert isinstance(resp.json()["settings"], dict)
