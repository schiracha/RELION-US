"""
Session-wide test fixtures.
"""
import os
import shutil

import pytest


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
