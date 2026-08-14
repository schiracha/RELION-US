import sys
from pathlib import Path

import pandas as pd
import pytest
import starfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converters.star_io import MissingColumnError
from converters.warp_bridge import (
    diff_columns,
    harmonize_particle_star,
    load_warp_star,
    remap_tomogram_paths,
)


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
