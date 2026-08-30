import sys
from pathlib import Path

import pandas as pd
import pytest
import starfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converters.star_io import MissingColumnError
from converters.warp_bridge import (
    angstrom_origin_to_pixel_origin,
    diff_columns,
    harmonize_particle_star,
    load_warp_star,
    remap_tomogram_paths,
)

# A real WarpTools "RELION 3.0 single STAR file" tomography particle export,
# verified 2026-08-30 against the official WarpTools docs:
# warpem.github.io/warp/reference/warptools/tomogram_particle_files/
# (loop_ block quoted verbatim there). Unlike the warpX/warpY-style
# fixtures below (deliberately-invented placeholder names used only to
# exercise the generic rename mechanism), this is what a real Warp/M
# particle export actually looks like -- already-native rln* columns,
# no mapping needed, confirming DEFAULT_COLUMN_MAP's emptiness is correct
# for this common case rather than an unfinished gap.
REAL_WARPTOOLS_PARTICLE_COLUMNS = {
    "rlnCoordinateX": [443.701994],
    "rlnCoordinateY": [214.586768],
    "rlnCoordinateZ": [203.739618],
    "rlnAngleRot": [54.077932],
    "rlnAngleTilt": [-29.726951],
    "rlnAnglePsi": [92.706063],
    "rlnMicrographName": ["TS_01.tomostar"],
}


def test_real_warptools_particle_export_needs_no_mapping():
    """The common case, confirmed against WarpTools' own docs: a real
    particle export already carries native rln* columns. diff_columns
    must report nothing missing (via the rlnMicrographName alternate for
    the tomogram-identity column -- this format predates rlnTomoName)."""
    df = pd.DataFrame(REAL_WARPTOOLS_PARTICLE_COLUMNS)
    result = diff_columns(df)
    assert result["missing_from_source"] == []
    assert "rlnMicrographName" in result["matched"]


def test_real_warptools_particle_export_harmonizes_with_empty_map():
    """harmonize_particle_star with the (still-empty, correctly so)
    DEFAULT_COLUMN_MAP must succeed on a real export by renaming the
    rlnMicrographName alternate to rlnTomoName, not by needing a
    wrp*->rln* mapping that real Warp particle exports don't use."""
    df = pd.DataFrame(REAL_WARPTOOLS_PARTICLE_COLUMNS)
    out = harmonize_particle_star(df)
    assert "rlnTomoName" in out.columns
    assert out.loc[0, "rlnTomoName"] == "TS_01.tomostar"
    assert "rlnMicrographName" not in out.columns  # renamed, not duplicated


def test_diff_columns_rlnMicrographName_alternate_not_reported_as_extra():
    """Before the alternate-column fix, rlnMicrographName would be listed
    in BOTH missing_from_source (rlnTomoName absent) and extra_in_source
    (an unrecognized column) -- confusing for a column that's actually the
    real tomogram identity field for this format."""
    df = pd.DataFrame({
        "rlnMicrographName": ["TS_01.tomostar"],
        "rlnCoordinateX": [1.0], "rlnCoordinateY": [2.0], "rlnCoordinateZ": [3.0],
    })
    result = diff_columns(df)
    assert "rlnMicrographName" not in result["extra_in_source"]
    assert "rlnTomoName" not in result["missing_from_source"]


def test_harmonize_raises_when_neither_tomo_name_nor_alternate_present():
    """The alternate-column fallback must not silently swallow a genuinely
    missing tomogram-identity column -- only rlnMicrographName specifically
    satisfies it, nothing else does."""
    df = pd.DataFrame({"rlnCoordinateX": [1.0], "rlnCoordinateY": [2.0], "rlnCoordinateZ": [3.0]})
    with pytest.raises(MissingColumnError, match="rlnTomoName"):
        harmonize_particle_star(df)


def test_angstrom_origin_to_pixel_origin():
    """Community-sourced Warp/RELION>=3.1 (Angstrom) -> M (pixel) origin
    conversion: pixel = angstrom / pixel_size_angst."""
    df = pd.DataFrame({
        "rlnTomoName": ["TS_01"],
        "rlnOriginXAngst": [10.0],
        "rlnOriginYAngst": [-5.0],
    })
    out = angstrom_origin_to_pixel_origin(df, pixel_size_angst=2.0)
    assert out.loc[0, "rlnOriginX"] == pytest.approx(5.0)
    assert out.loc[0, "rlnOriginY"] == pytest.approx(-2.5)
    assert "rlnOriginXAngst" not in out.columns
    assert "rlnOriginYAngst" not in out.columns


def test_angstrom_origin_to_pixel_origin_raises_when_neither_present():
    df = pd.DataFrame({"rlnTomoName": ["TS_01"]})
    with pytest.raises(KeyError):
        angstrom_origin_to_pixel_origin(df, pixel_size_angst=2.0)


def test_angstrom_origin_to_pixel_origin_rejects_non_positive_pixel_size():
    """Found in code review: an unguarded division let pixel_size_angst=0
    raise a confusing ZeroDivisionError instead of a clear, actionable
    error, matching this module's usual style (e.g. harmonize_particle_star's
    MissingColumnError)."""
    df = pd.DataFrame({"rlnTomoName": ["TS_01"], "rlnOriginXAngst": [10.0]})
    for bad in (0.0, -1.5):
        with pytest.raises(ValueError, match="pixel_size_angst must be"):
            angstrom_origin_to_pixel_origin(df, pixel_size_angst=bad)


def test_alternate_column_not_collapsed_when_independently_required():
    """Found in code review: if a caller's required_columns lists BOTH
    rlnTomoName and rlnMicrographName as independently required (not the
    common case today, but a real edge case the alternate-column mechanism
    must not mishandle), rlnMicrographName must NOT be silently renamed
    away to satisfy rlnTomoName -- that would destroy the only copy of a
    column the caller separately asked for, then report it as missing
    despite being present in the input. _resolve_tomogram_name_alternate's
    guard (an alternate doesn't count if it's itself in required_columns)
    exists specifically for this."""
    required = ("rlnTomoName", "rlnMicrographName", "rlnCoordinateX", "rlnCoordinateY", "rlnCoordinateZ")
    df = pd.DataFrame({
        "rlnMicrographName": ["a.mrc"],
        "rlnCoordinateX": [1.0], "rlnCoordinateY": [2.0], "rlnCoordinateZ": [3.0],
    })
    # Correctly reported as missing (rlnTomoName genuinely absent, and
    # rlnMicrographName can't double as satisfying both requirements).
    result = diff_columns(df, reference_columns=required)
    assert "rlnTomoName" in result["missing_from_source"]
    assert "rlnMicrographName" in result["matched"]

    with pytest.raises(MissingColumnError, match="rlnTomoName"):
        harmonize_particle_star(df, required_columns=required)


def test_diff_columns_all_matched():
    df = pd.DataFrame(
        {
            "rlnTomoName": ["TS_01"],
            "rlnCoordinateX": [1.0],
            "rlnCoordinateY": [2.0],
            "rlnCoordinateZ": [3.0],
        }
    )
    result = diff_columns(df)
    assert result["missing_from_source"] == []
    assert set(result["matched"]) == {
        "rlnTomoName",
        "rlnCoordinateX",
        "rlnCoordinateY",
        "rlnCoordinateZ",
    }


# NOTE: "warpX"/"warpY"/"warpTomoName" below are deliberately-invented
# placeholder names, NOT real Warp column names (Warp's real wrp*-prefixed
# columns only appear in .tomostar per-tilt-series metadata, never in
# particle exports -- see REAL_WARPTOOLS_PARTICLE_COLUMNS above and the
# module docstring). These tests exist to exercise diff_columns'/
# harmonize_particle_star's generic rename MECHANISM with an explicit
# column_map, independent of what any real tool actually calls its columns.


def test_diff_columns_reports_missing():
    df = pd.DataFrame({"warpX": [1.0], "warpY": [2.0]})
    result = diff_columns(df)
    assert "rlnCoordinateX" in result["missing_from_source"]
    assert "warpX" in result["extra_in_source"]


def test_harmonize_with_explicit_map():
    df = pd.DataFrame(
        {
            "warpTomoName": ["TS_01"],
            "warpX": [1.0],
            "warpY": [2.0],
            "warpZ": [3.0],
        }
    )
    mapping = {
        "warpTomoName": "rlnTomoName",
        "warpX": "rlnCoordinateX",
        "warpY": "rlnCoordinateY",
        "warpZ": "rlnCoordinateZ",
    }
    out = harmonize_particle_star(df, column_map=mapping)
    assert list(out.columns[:1]) == ["rlnTomoName"] or "rlnTomoName" in out.columns
    assert out.loc[0, "rlnCoordinateX"] == 1.0


def test_harmonize_raises_when_still_missing_after_map():
    df = pd.DataFrame({"warpX": [1.0]})
    with pytest.raises(MissingColumnError):
        harmonize_particle_star(df, column_map={"warpX": "rlnCoordinateX"})


def test_remap_tomogram_paths_only_touches_matching_prefix():
    df = pd.DataFrame(
        {
            "rlnTomoName": [
                "/scratch/warp_project/TS_01",
                "/other/path/TS_02",
            ]
        }
    )
    out = remap_tomogram_paths(
        df, old_prefix="/scratch/warp_project", new_prefix="/home/user/relion_project"
    )
    assert out.loc[0, "rlnTomoName"] == "/home/user/relion_project/TS_01"
    assert out.loc[1, "rlnTomoName"] == "/other/path/TS_02"  # untouched


def test_load_warp_star_roundtrip(tmp_path):
    df = pd.DataFrame({"rlnTomoName": ["TS_01"], "wrpDose": [3.0]})
    path = tmp_path / "TS_01.tomostar"
    starfile.write({"tomostar": df}, path)
    loaded = load_warp_star(path, block="tomostar")
    assert loaded.loc[0, "wrpDose"] == 3.0
