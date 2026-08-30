"""
Unit tests for converters/imod_bridge.py.

.xf / .tlt tests run everywhere (pure Python, plain text formats).
.mod tests are skipped automatically if IMOD's point2model/model2point
aren't on PATH. On this dev machine conftest.py's
_add_local_imod_to_path_if_needed adds the real local IMOD 5.1.12 install
to PATH automatically, so those tests actually run here rather than skip;
on a sandbox with no IMOD install at all they still skip as before.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converters.imod_bridge import (
    XfRecord,
    coordinates_to_model,
    model_to_coordinates,
    read_tlt,
    read_xf,
    write_tlt,
    write_xf,
)

IMOD_AVAILABLE = shutil.which("point2model") is not None and shutil.which("model2point") is not None


def test_xf_roundtrip(tmp_path):
    records = [
        XfRecord(1.0, 0.0, 0.0, 1.0, 0.5, -0.5),
        XfRecord(0.999, 0.01, -0.01, 0.999, 1.2, 0.3),
    ]
    path = tmp_path / "test.xf"
    write_xf(records, path)
    loaded = read_xf(path)
    assert len(loaded) == 2
    assert loaded[0].a11 == pytest.approx(1.0)
    assert loaded[1].dx == pytest.approx(1.2)


def test_tlt_roundtrip(tmp_path):
    angles = [-60.0, -57.0, -54.0, 0.0, 3.0, 60.0]
    path = tmp_path / "test.tlt"
    write_tlt(angles, path)
    loaded = read_tlt(path)
    assert loaded == pytest.approx(angles)


def test_read_xf_rejects_malformed_line(tmp_path):
    path = tmp_path / "bad.xf"
    path.write_text("1.0 0.0 0.0 1.0 0.5\n")  # only 5 fields
    with pytest.raises(ValueError, match="expected 6 fields"):
        read_xf(path)


def test_read_tlt_rejects_non_numeric(tmp_path):
    path = tmp_path / "bad.tlt"
    path.write_text("-60.0\nnot_a_number\n")
    with pytest.raises(ValueError, match="not a number"):
        read_tlt(path)


def test_missing_binary_raises_clear_error(tmp_path, monkeypatch):
    # Force "not found" regardless of the sandbox's actual PATH, to test the
    # error-message path deterministically.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    df = pd.DataFrame(
        {
            "rlnTomoName": ["TS_01"],
            "rlnCoordinateX": [1.0],
            "rlnCoordinateY": [2.0],
            "rlnCoordinateZ": [3.0],
        }
    )
    with pytest.raises(RuntimeError, match="point2model"):
        coordinates_to_model(df, tmp_path / "out.mod", tomo_name="TS_01")


def test_coordinates_to_model_passes_image_flag_when_given(tmp_path, monkeypatch):
    """man point2model (real IMOD 5.1.12, verified 2026-08-30) recommends
    -image when the .mod feeds downstream IMOD tools; tomo_mrc_path should
    add it to the constructed command. Captures the real argv via a
    monkeypatched subprocess.run rather than requiring a real, valid .mrc
    file on disk (point2model would otherwise need one to succeed with
    -image), matching test_missing_binary_raises_clear_error's own style."""
    captured = {}

    def fake_run(cmd, capture_output, text):
        captured["cmd"] = cmd
        (tmp_path / "out.mod").write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    df = pd.DataFrame({
        "rlnTomoName": ["TS_01"],
        "rlnCoordinateX": [1.0],
        "rlnCoordinateY": [2.0],
        "rlnCoordinateZ": [3.0],
    })
    mrc = tmp_path / "TS_01.mrc"
    coordinates_to_model(df, tmp_path / "out.mod", tomo_name="TS_01", tomo_mrc_path=mrc)

    assert "-image" in captured["cmd"]
    assert str(mrc) == captured["cmd"][captured["cmd"].index("-image") + 1]


def test_coordinates_to_model_omits_image_flag_by_default(tmp_path, monkeypatch):
    """Without tomo_mrc_path, the command must be unchanged from before this
    parameter existed — no -image flag at all."""
    captured = {}

    def fake_run(cmd, capture_output, text):
        captured["cmd"] = cmd
        (tmp_path / "out.mod").write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    df = pd.DataFrame({
        "rlnTomoName": ["TS_01"],
        "rlnCoordinateX": [1.0],
        "rlnCoordinateY": [2.0],
        "rlnCoordinateZ": [3.0],
    })
    coordinates_to_model(df, tmp_path / "out.mod", tomo_name="TS_01")

    assert "-image" not in captured["cmd"]


@pytest.mark.skipif(not IMOD_AVAILABLE, reason="IMOD point2model/model2point not on PATH")
def test_coordinates_to_model_and_back(tmp_path):
    df = pd.DataFrame(
        {
            "rlnTomoName": ["TS_01", "TS_01"],
            "rlnCoordinateX": [100.0, 150.0],
            "rlnCoordinateY": [110.0, 160.0],
            "rlnCoordinateZ": [50.0, 55.0],
        }
    )
    mod_path = tmp_path / "picks.mod"
    coordinates_to_model(df, mod_path, tomo_name="TS_01")
    assert mod_path.exists()

    back = model_to_coordinates(mod_path, tomo_name="TS_01")
    assert len(back) == 2
    assert sorted(back["rlnCoordinateX"]) == pytest.approx([100.0, 150.0])
