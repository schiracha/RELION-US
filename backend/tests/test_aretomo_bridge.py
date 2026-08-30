"""
Tests for aretomo_bridge.py's .aln -> .xf/.tlt conversion.

The ROT/TX/TY -> .xf formula under test here was verified 2026-08-30
against a real AreTomo2 source checkout (ImodUtil/CSaveXF.cpp,
MrcUtil/CSaveAlnFile.cpp) term-for-term — see aretomo_bridge.py's own
module docstring for the full citation. These tests still only check
self-consistency against a hand-built SAMPLE_ALN (no real .aln/tilt-series
data was available locally to build a true I/O ground-truth fixture — see
the module docstring's note on the local AreTomo2 binary's broken CUDA
runtime), but the formula itself is no longer just community-sourced.
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converters import aretomo_bridge as ab

SAMPLE_ALN = """# AreTomo Alignment / Priims bprmMn
# RawSize = 4096 4096 4
# NumPatches = 0
# DarkFrame =    0    1    -60.00
# SEC     ROT      GMAG      TX       TY     SMEAN   SFIT   SCALE   BASE    TILT
    0    85.0000  1.00000   10.000   0.000   1.0    1.0    1.0    0.0   -30.00
    1    85.0000  1.00000    0.000   5.000   1.0    1.0    1.0    0.0     0.00
    2    85.0000  1.00000   -3.000   2.000   1.0    1.0    1.0    0.0    30.00
# Local Alignment
    0 0 1.0 2.0 0.1 0.2 1.0
"""


def _write(tmp_path):
    p = tmp_path / "TS.aln"
    p.write_text(SAMPLE_ALN)
    return p


def test_read_aln_parses_global_header_and_dark(tmp_path):
    data = ab.read_aln(_write(tmp_path))
    assert list(data.df.columns) == list(ab.ALN_GLOBAL_COLUMNS)
    assert len(data.df) == 3               # local rows are NOT counted
    assert data.raw_size == (4096, 4096, 4)
    assert data.num_patches == 0
    assert len(data.dark_frames) == 1
    assert data.dark_frames[0].sec0 == 0
    assert data.dark_frames[0].angle == pytest.approx(-60.0)


def test_tilt_angles_in_sec_order(tmp_path):
    assert ab.aln_tilt_angles(_write(tmp_path)) == [-30.0, 0.0, 30.0]


def test_xf_mapping_matches_df_to_xf_formula(tmp_path):
    """Verify against the teamtomo/alnfile df_to_xf formula: theta = -ROT,
    shift negated and rotated into the transformed frame."""
    recs = ab.aln_to_xf_records(_write(tmp_path))
    assert len(recs) == 3
    rot, tx, ty = 85.0, 10.0, 0.0
    theta = math.radians(-rot)
    c, s = math.cos(theta), math.sin(theta)
    r0 = recs[0]
    assert r0.a11 == pytest.approx(c)
    assert r0.a12 == pytest.approx(-s)
    assert r0.a21 == pytest.approx(s)
    assert r0.a22 == pytest.approx(c)
    assert r0.dx == pytest.approx(c * (-tx) + (-s) * (-ty))
    assert r0.dy == pytest.approx(s * (-tx) + c * (-ty))


def test_aln_to_imod_writes_xf_and_tlt(tmp_path):
    p = _write(tmp_path)
    summary = ab.aln_to_imod(p, tmp_path / "aligned.xf")
    assert summary["n_images"] == 3
    assert summary["n_dark_excluded"] == 1
    assert summary["dark_indices_original"] == [0]
    xf = (tmp_path / "aligned.xf").read_text().strip().splitlines()
    tlt = (tmp_path / "aligned.tlt").read_text().split()
    assert len(xf) == 3
    assert tlt == ["-30.00", "0.00", "30.00"]


def test_read_aln_rejects_file_with_no_global_rows(tmp_path):
    p = tmp_path / "empty.aln"
    p.write_text("# RawSize = 100 100 1\n# Local Alignment\n0 0 1 2 3 4 1\n")
    with pytest.raises(ValueError, match="no global alignment rows"):
        ab.read_aln(p)
