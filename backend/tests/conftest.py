"""
Session-wide test fixtures.
"""
import os
import shutil

import pytest


def _add_local_imod_to_path_if_needed():
    """Module-level (not a fixture) on purpose: test_imod_bridge.py computes
    `IMOD_AVAILABLE = shutil.which(...)` at IMPORT time (used to decide
    whether to @pytest.mark.skipif its one real-binary test), and pytest
    imports every test module during collection, before any fixture -- even
    an autouse one -- ever runs. A fixture would run too late to change
    that already-evaluated constant.

    This dev machine has a real IMOD 5.1.12 install at /usr/local/IMOD,
    confirmed 2026-08-30, but it's not on PATH by default -- so
    model2point/point2model have been silently unavailable (and their one
    real-binary test silently skipped) every run here. Only touch PATH if
    the binaries aren't already resolvable AND a candidate install path
    genuinely exists, so this never overrides a developer's own newer IMOD
    elsewhere on PATH and is a no-op on any machine without IMOD at all.

    Candidate path: RELION_US_TEST_IMOD_BIN if set, else /usr/local/IMOD/bin
    -- matching this repo's existing precedent for "optional real tool
    installed at some path on this dev machine" (see
    verify_draft_flags_against_relion.py's RELION_BIN_DIR/RELION_SRC_DIR),
    rather than hardcoding only this one machine's path with no override.
    """
    if shutil.which("model2point") and shutil.which("point2model"):
        return
    imod_bin = os.environ.get("RELION_US_TEST_IMOD_BIN", "/usr/local/IMOD/bin")
    if not (os.path.isfile(os.path.join(imod_bin, "model2point"))
            and os.path.isfile(os.path.join(imod_bin, "point2model"))):
        return
    os.environ["PATH"] = imod_bin + os.pathsep + os.environ.get("PATH", "")


_add_local_imod_to_path_if_needed()


@pytest.fixture(autouse=True)
def _no_real_relion_pipeliner_on_path(monkeypatch):
    """Strip any REAL `relion_pipeliner` from PATH for every backend test.

    project_manager.pipeline_sync_setting defaults to True now (see its own
    module comment) -- this machine has a real RELION install
    (/usr/local/relion/bin), so without this, any test that creates a
    project via tmp_path + project_manager.init_new_project would silently
    start invoking the REAL relion_pipeliner mid-test: real subprocess
    calls, real job numbering from RELION's own counter (not this app's
    _next_job_number), real default_pipeline.star writes -- none of which
    the test asked for or accounts for. Confirmed to matter for real: this
    is exactly what caused test_job_runner.py's job-numbering tests to
    start failing (job000 instead of job001) the moment the sync default
    flipped, purely because this dev box happens to have RELION installed.

    A test that wants pipeline sync to actually do something
    (test_pipeline_bridge.py's own `project` fixture) prepends its OWN stub
    binary's directory onto PATH afterward, which `shutil.which` finds
    first regardless of this fixture -- PATH search order still works
    normally, this only ever removes the real one.
    """
    real = shutil.which("relion_pipeliner")
    if real is None:
        return
    real_dir = os.path.dirname(real)
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p != real_dir]
    monkeypatch.setenv("PATH", os.pathsep.join(parts))
