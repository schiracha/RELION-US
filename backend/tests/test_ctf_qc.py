"""
Tests for ctf_qc.py — end-of-job CTF Estimation QC charts + power-spectrum
thumbnails, read from RELION's own joined CTF star files.

Fixtures mimic what RELION itself writes (verified against
src/ctffind_runner.cpp's joinCtffindResults(), both branches): SPA's
micrographs_ctf.star (block "micrographs") and tomo's power_spectra_fits.star
(one anonymous block), including the real "path/with/subdirs/name.ctf:mrc"
shape of rlnCtfImage that a class average/volume reference never has.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ctf_qc

mrcfile = pytest.importorskip("mrcfile")
starfile = pytest.importorskip("starfile")


def _write_ctf_image(path: Path, seed: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    with mrcfile.new(path, overwrite=True) as m:
        m.set_data(rng.random((48, 48)).astype(np.float32))


def _write_spa(job_dir: Path, n: int = 5):
    """job_dir must be a real <project_root>/CtfFind/job00N -- rlnCtfImage's
    value is job-relative WITH subdirectories (mirroring the micrograph's
    own layout), resolved against the project root, not the job dir
    directly (see progress._resolve_reference's third fallback)."""
    project_root = job_dir.parent.parent
    job_rel = job_dir.relative_to(project_root)
    rows = []
    for i in range(n):
        rel = f"{job_rel}/mic{i:03d}/mic{i:03d}.ctf"
        _write_ctf_image(project_root / rel, seed=i)
        rows.append({
            "rlnMicrographName": f"MotionCorr/job001/mic{i:03d}.mrc",
            "rlnCtfImage": f"{rel}:mrc",
            "rlnDefocusU": 10000.0 + i * 500,
            "rlnDefocusV": 9800.0 + i * 500,
            "rlnCtfAstigmatism": 200.0 + i * 10,
            "rlnDefocusAngle": 30.0 + i,
            "rlnCtfFigureOfMerit": 0.05 - i * 0.005,
            "rlnCtfMaxResolution": 4.0 + i * 1.5,  # worst (largest) = last
        })
    optics = pd.DataFrame([{"rlnVoltage": 300.0, "rlnOpticsGroup": 1}])
    starfile.write(
        {"optics": optics, "micrographs": pd.DataFrame(rows)},
        job_dir / "micrographs_ctf.star", overwrite=True,
    )


def _write_tomo(job_dir: Path, n: int = 4):
    project_root = job_dir.parent.parent
    job_rel = job_dir.relative_to(project_root)
    rows = []
    for i in range(n):
        rel = f"{job_rel}/tilt_series/tilt{i:03d}.ctf"
        _write_ctf_image(project_root / rel, seed=100 + i)
        rows.append({
            "rlnCtfImage": f"{rel}:mrc",
            "rlnDefocusU": 20000.0 + i * 300,
            "rlnDefocusV": 19500.0 + i * 300,
            "rlnCtfAstigmatism": 150.0,
            "rlnDefocusAngle": -10.0 + i,
            "rlnCtfFigureOfMerit": 0.02,
            "rlnCtfMaxResolution": 8.0 + i,
        })
    starfile.write({"": pd.DataFrame(rows)}, job_dir / "power_spectra_fits.star", overwrite=True)


@pytest.fixture
def job_dir(tmp_path):
    """A real <project_root>/CtfFind/job002 -- see _write_spa's docstring
    for why the two-level RELION convention matters here specifically."""
    d = tmp_path / "CtfFind" / "job002"
    d.mkdir(parents=True)
    return d


def test_supports_ctf_qc_only_ctffind():
    assert ctf_qc.supports_ctf_qc("Ctffind")
    assert not ctf_qc.supports_ctf_qc("Class2D")
    assert not ctf_qc.supports_ctf_qc("Motioncorr")


def test_not_yet_available_is_not_an_error(job_dir):
    """The job hasn't finished (or hasn't started) -- joinCtffindResults()
    only writes its output once, at the very end."""
    out = ctf_qc.read_ctf_qc(job_dir)
    assert out["available"] is False
    assert out["micrographs"] == []


def test_reads_spa_micrographs_ctf_star(job_dir):
    _write_spa(job_dir, n=5)
    out = ctf_qc.read_ctf_qc(job_dir)
    assert out["available"] is True
    assert out["count"] == 5
    first = out["micrographs"][0]
    assert first["name"] == "MotionCorr/job001/mic000.mrc"
    assert first["defocus_u"] == pytest.approx(10000.0)
    assert first["defocus_v"] == pytest.approx(9800.0)
    assert first["astigmatism"] == pytest.approx(200.0)
    assert first["fom"] == pytest.approx(0.05)
    assert first["max_resolution_A"] == pytest.approx(4.0)


def test_reads_tomo_power_spectra_fits_star_with_name_fallback(job_dir):
    """Tomo's rlnMicrographName is deliberately removed by RELION itself --
    the per-tilt-image "name" must fall back to the CTF image's own
    basename rather than come back empty/missing."""
    _write_tomo(job_dir, n=4)
    out = ctf_qc.read_ctf_qc(job_dir)
    assert out["available"] is True
    assert out["count"] == 4
    names = [m["name"] for m in out["micrographs"]]
    assert names == ["tilt000", "tilt001", "tilt002", "tilt003"]
    assert out["micrographs"][0]["defocus_u"] == pytest.approx(20000.0)


def test_spa_takes_priority_when_both_files_somehow_exist(job_dir):
    _write_spa(job_dir, n=2)
    _write_tomo(job_dir, n=9)
    out = ctf_qc.read_ctf_qc(job_dir)
    assert out["count"] == 2  # the SPA fixture's count, not tomo's


def test_thumbnail_from_ctf_image_with_nested_path_and_format_hint(job_dir):
    """rlnCtfImage's value is a job-relative path with subdirectories AND
    RELION's ":mrc" format-hint suffix -- both real, both different from
    how a class average/volume reference looks (progress.py's existing
    case), and both must resolve correctly."""
    _write_spa(job_dir, n=1)
    out = ctf_qc.read_ctf_qc(job_dir)
    png = ctf_qc.render_ctf_thumbnail(job_dir, out["micrographs"][0]["ctf_image"])
    assert png[:4] == b"\x89PNG"


def test_thumbnail_missing_image_raises_progresserror(job_dir):
    _write_spa(job_dir, n=1)
    with pytest.raises(ctf_qc.ProgressError):
        ctf_qc.render_ctf_thumbnail(job_dir, "CtfFind/job002/does/not/exist.ctf:mrc")


def test_worst_fit_first_ranking_is_derivable_from_max_resolution(job_dir):
    """Not computed server-side (see module docstring) -- just confirms the
    field the frontend sorts by is present and numeric for every row, so
    "worst N" ranking is actually possible from what's returned."""
    _write_spa(job_dir, n=5)
    out = ctf_qc.read_ctf_qc(job_dir)
    worst = sorted(out["micrographs"], key=lambda m: m["max_resolution_A"], reverse=True)
    assert worst[0]["name"] == "MotionCorr/job001/mic004.mrc"  # 4.0 + 4*1.5 = 10.0, the largest
