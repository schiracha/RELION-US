"""
Filter Picks (pick_filter.py + the PickFilter custom job): choose which picks to
keep by a numeric column of a particles STAR file; the selection is RELION's
select on value.
"""
import os
import shutil
import sys
import time
from pathlib import Path

import mrcfile
import numpy as np
import pandas as pd
import pytest
import starfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
import pick_filter as pf
import project_manager
import viz

fastapi_testclient = pytest.importorskip("fastapi.testclient")
# Found before conftest's autouse fixture removes RELION's directory from PATH (it keeps the
# real relion_pipeliner out of every test); the one test that compares with real RELION puts it back.
REAL_STAR_HANDLER = shutil.which("relion_star_handler")
HAVE_STAR_HANDLER = REAL_STAR_HANDLER is not None


def _tomo_picks(scores_a=(0.2, 0.5, 0.8, 1.0), scores_b=(0.1, 0.9), with_score=True):
    rows = []
    for name, scores in (("TS_001", scores_a), ("TS_002", scores_b)):
        for i, s in enumerate(scores):
            rows.append({"rlnTomoName": name, "rlnCoordinateX": 10.0 + i, "rlnCoordinateY": 20.0 + i,
                         "rlnCoordinateZ": 5.0 + 10 * i, "rlnClassNumber": 1, "rlnOpticsGroup": 1,
                         "rlnRandomSubset": 1 + i % 2, "rlnImageName": f"{name}_{i}.mrcs",
                         "rlnMaxValueProbDistribution": 0.1 * (i + 1), "rlnAutopickFigureOfMerit": s})
    df = pd.DataFrame(rows)
    return df if with_score else df.drop(columns=["rlnAutopickFigureOfMerit"])


def _write_star(project, name, blocks):
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    starfile.write(blocks, path, overwrite=True)
    return name


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    project_manager.init_new_project(p)
    return p


@pytest.fixture(autouse=True)
def _fresh_cache():
    pf._cache.clear()
    yield
    pf._cache.clear()


# ---- the columns on offer -------------------------------------------------

def test_numeric_columns_are_offered_score_like_first_and_ids_and_text_are_not(project):
    star = _write_star(project, "p.star", {"particles": _tomo_picks()})
    t = pf.load_table(project, star)
    names = [c["name"] for c in t.numeric_columns()]
    assert names[0] == "rlnAutopickFigureOfMerit" and t.default_column() == "rlnAutopickFigureOfMerit"
    assert names.index("rlnMaxValueProbDistribution") == 1               # next score-like column
    assert {"rlnCoordinateX", "rlnCoordinateY", "rlnCoordinateZ"} <= set(names)
    for excluded in ("rlnTomoName", "rlnImageName", "rlnOpticsGroup", "rlnRandomSubset", "rlnClassNumber"):
        assert excluded not in names, excluded               # text, ids, and a constant column
    groups = {c["name"]: c["group"] for c in t.numeric_columns()}
    assert groups["rlnAutopickFigureOfMerit"] == "Scores and quality" and groups["rlnCoordinateZ"] == "Position"


def test_without_a_score_the_default_is_the_next_most_useful_column(project):
    star = _write_star(project, "p.star", {"particles": _tomo_picks(with_score=False)})
    assert pf.load_table(project, star).default_column() == "rlnMaxValueProbDistribution"
    bare = _tomo_picks(with_score=False).drop(columns=["rlnMaxValueProbDistribution"])
    star2 = _write_star(project, "q.star", {"particles": bare})
    assert pf.load_table(project, star2).default_column() == "rlnCoordinateX"


def test_column_statistics_and_integer_detection(project):
    df = _tomo_picks()
    df["rlnNrOfSignificantSamples"] = [1, 2, 3, 4, 5, 6]
    t = pf.load_table(project, _write_star(project, "p.star", {"particles": df}))
    col = {c["name"]: c for c in t.numeric_columns()}
    assert col["rlnNrOfSignificantSamples"]["integer"] is True and col["rlnNrOfSignificantSamples"]["max"] == 6
    assert col["rlnAutopickFigureOfMerit"]["integer"] is False
    assert col["rlnAutopickFigureOfMerit"]["n_unique"] == 6 or col["rlnAutopickFigureOfMerit"]["n_unique"] == 5


def test_an_spa_file_with_optics_and_micrograph_names(project):
    particles = pd.DataFrame({
        "rlnMicrographName": ["mics/a.mrc"] * 3 + ["mics/b.mrc"] * 2,
        "rlnCoordinateX": [1.0, 2, 3, 4, 5], "rlnCoordinateY": [5.0, 4, 3, 2, 1],
        "rlnAutopickFigureOfMerit": [0.3, 0.6, 0.9, 0.2, 0.7], "rlnDefocusU": [1e4, 1.1e4, 1.2e4, 2e4, 2.1e4],
        "rlnImageName": [f"{i}@x.mrcs" for i in range(5)], "rlnOpticsGroup": 1})
    optics = pd.DataFrame({"rlnOpticsGroup": [1], "rlnImagePixelSize": [1.06], "rlnVoltage": [300.0]})
    t = pf.load_table(project, _write_star(project, "p.star", {"optics": optics, "particles": particles}))
    s = pf.summary(t)
    assert s["kind"] == "spa" and s["image_column"] == "rlnMicrographName" and s["n_images"] == 2
    assert s["n_rows"] == 5 and s["block"] == "particles"
    assert "rlnImagePixelSize" not in [c["name"] for c in s["columns"]]          # the optics table is not picks


def test_a_single_unnamed_table_of_older_star_files_is_found(project):
    path = project / "old.star"
    path.write_text("\ndata_\n\nloop_\n_rlnCoordinateX #1\n_rlnCoordinateY #2\n_rlnAutopickFigureOfMerit #3\n"
                    "1.0 2.0 0.5\n3.0 4.0 0.9\n")
    t = pf.load_table(project, "old.star")
    assert t.n_rows == 2 and t.default_column() == "rlnAutopickFigureOfMerit" and t.image_column is None


# ---- histogram and counts ---------------------------------------------------

def test_histogram_counts_every_value_and_few_distinct_values_get_few_bins(project):
    t = pf.load_table(project, _write_star(project, "p.star", {"particles": _tomo_picks()}))
    h = pf.histogram(t, "rlnAutopickFigureOfMerit", 20)
    assert sum(h["counts"]) == 6 and len(h["edges"]) == len(h["counts"]) + 1 and h["n_missing"] == 0
    assert len(h["counts"]) == 6                        # six distinct values: no more than six bins
    assert h["min"] == 0.1 and h["max"] == 1.0
    df = _tomo_picks()
    df.loc[0, "rlnAutopickFigureOfMerit"] = np.nan
    t2 = pf.load_table(project, _write_star(project, "q.star", {"particles": df}))
    h2 = pf.histogram(t2, "rlnAutopickFigureOfMerit", 20)
    assert h2["n_missing"] == 1 and sum(h2["counts"]) == 5


def test_a_pick_exactly_on_a_bound_follows_relions_32_bit_bounds(project):
    """relion_star_handler reads --minval/--maxval as 32-bit floats: 0.9 as a maximum is really
    0.89999998, so a pick scoring exactly 0.9 is left out, while 0.5 and 0.75 (exact in binary) are kept.
    The counts must say what RELION will write."""
    df = pd.DataFrame({"rlnTomoName": "A", "rlnAutopickFigureOfMerit": [0.3, 0.5, 0.7, 0.75, 0.9, 1.0]})
    t = pf.load_table(project, _write_star(project, "b.star", {"particles": df}))
    kept = lambda lo, hi: pf.counts(t, "rlnAutopickFigureOfMerit", lo, hi)["kept"]   # noqa: E731
    assert kept(0.5, 0.9) == 3                      # 0.5, 0.7, 0.75; the 0.9 pick is on the (32-bit) bound and out
    assert kept(0.5, 0.90000001) == 4               # a hair above: now it is in
    assert kept(0.3, 1.0) == 5                      # a minimum of 0.3 is really 0.30000001, so 0.3 is out
    assert kept(0.5, 0.75) == 3 and kept(0.75, 0.75) == 1                     # exact in float32: inclusive


def test_counts_are_inclusive_exclude_missing_values_and_split_per_image(project):
    df = _tomo_picks()
    df.loc[0, "rlnAutopickFigureOfMerit"] = np.nan
    t = pf.load_table(project, _write_star(project, "p.star", {"particles": df}))
    c = pf.counts(t, "rlnAutopickFigureOfMerit", 0.5, 0.75)
    assert (c["kept"], c["total"]) == (1, 6)           # only 0.5; 0.8 and 0.9 and 1.0 are above, 0.1 below, NaN never
    c = pf.counts(t, "rlnAutopickFigureOfMerit", 0.5, 1.0)
    assert (c["kept"], c["total"]) == (4, 6)           # 0.5, 0.8, 1.0, 0.9: both ends inclusive
    images = {i["name"]: (i["kept"], i["total"]) for i in c["images"]}
    assert images == {"TS_001": (3, 4), "TS_002": (1, 2)}
    assert pf.counts(t, "rlnAutopickFigureOfMerit", 0.0, 1.0)["kept"] == 5         # all but the missing one
    with pytest.raises(pf.PickFilterError, match="above the maximum"):
        pf.counts(t, "rlnAutopickFigureOfMerit", 0.9, 0.5)
    with pytest.raises(pf.PickFilterError, match="not a numeric column"):
        pf.counts(t, "rlnImageName", 0, 1)


# ---- the preview ---------------------------------------------------------------

def _tomogram(project, name, shape=(20, 40, 50), voxel=10.0):
    path = project / "Tomograms" / f"{name}.mrc"
    path.parent.mkdir(parents=True, exist_ok=True)
    with mrcfile.new(path, overwrite=True) as m:
        m.set_data(np.random.default_rng(0).normal(size=shape).astype(np.float32))
        m.voxel_size = voxel
    return f"Tomograms/{name}.mrc"


def _tomograms_star(project, names):
    return _write_star(project, "Tomograms/tomograms.star", {"": pd.DataFrame({
        "rlnTomoName": names, "rlnTomoReconstructedTomogram": [_tomogram(project, n) for n in names]})})


def test_a_tomograms_picks_come_with_their_values_and_the_volume_to_draw_on(project):
    star = _write_star(project, "p.star", {"particles": _tomo_picks()})
    preview = _tomograms_star(project, ["TS_001", "TS_002"])
    t = pf.load_table(project, star)
    r = pf.image_picks(project, t, "rlnAutopickFigureOfMerit", "TS_001", preview)
    assert r["path"] == "Tomograms/TS_001.mrc" and r["volume"]["nx"] == 50 and r["volume"]["voxel_size"] == 10.0
    assert r["n"] == 4 and r["x"] == [10.0, 11.0, 12.0, 13.0] and r["z"] == [5.0, 15.0, 25.0, 35.0]
    assert r["value"] == [0.2, 0.5, 0.8, 1.0] and r["message"] == ""
    assert pf.image_picks(project, t, "rlnAutopickFigureOfMerit", "TS_002", preview)["n"] == 2
    with pytest.raises(pf.PickFilterError, match="no picks"):
        pf.image_picks(project, t, "rlnAutopickFigureOfMerit", "TS_999", preview)


def test_tomogram_picks_without_a_tomograms_star_still_list_but_say_why_nothing_is_drawn(project):
    t = pf.load_table(project, _write_star(project, "p.star", {"particles": _tomo_picks()}))
    r = pf.image_picks(project, t, "rlnAutopickFigureOfMerit", "TS_001", "")
    assert r["path"] is None and r["n"] == 4 and "tomograms STAR" in r["message"]


def test_centred_angstrom_picks_are_placed_on_the_voxel_grid(project):
    df = _tomo_picks().drop(columns=["rlnCoordinateX", "rlnCoordinateY", "rlnCoordinateZ"])
    df["rlnCenteredCoordinateXAngst"] = [0.0, 50.0, -50.0, 10.0, 0.0, 0.0]
    df["rlnCenteredCoordinateYAngst"] = 0.0
    df["rlnCenteredCoordinateZAngst"] = 0.0
    t = pf.load_table(project, _write_star(project, "c.star", {"particles": df}))
    r = pf.image_picks(project, t, "rlnAutopickFigureOfMerit", "TS_001", _tomograms_star(project, ["TS_001", "TS_002"]))
    # 50 voxels wide at 10 A/voxel: centre 25; 50 A = 5 voxels
    assert r["x"] == [25.0, 30.0, 20.0, 26.0] and r["y"] == [20.0] * 4 and r["z"] == [10.0] * 4
    r2 = pf.image_picks(project, t, "rlnAutopickFigureOfMerit", "TS_001", "")
    assert r2["x"] == [] and "centred Angstroms" in r2["message"]


def test_spa_picks_draw_on_the_micrograph_the_file_names(project):
    mic = project / "mics" / "a.mrc"
    mic.parent.mkdir()
    with mrcfile.new(mic) as m:
        m.set_data(np.zeros((30, 40), dtype=np.float32))
        m.voxel_size = 1.0
    df = pd.DataFrame({"rlnMicrographName": ["mics/a.mrc"] * 2, "rlnCoordinateX": [5.0, 6.0],
                       "rlnCoordinateY": [7.0, 8.0], "rlnAutopickFigureOfMerit": [0.4, 0.9]})
    t = pf.load_table(project, _write_star(project, "s.star", {"particles": df}))
    r = pf.image_picks(project, t, "rlnAutopickFigureOfMerit", "mics/a.mrc")
    assert r["path"] == "mics/a.mrc" and r["volume"]["nz"] == 1 and r["x"] == [5.0, 6.0] and r["y"] == [7.0, 8.0]
    assert r["z"] == []


# ---- writing the selection ---------------------------------------------------------

def _without_star_handler(monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda name: None)


def test_the_selection_keeps_the_rows_in_range_and_every_other_block(project, monkeypatch):
    _without_star_handler(monkeypatch)
    particles = _tomo_picks()
    optics = pd.DataFrame({"rlnOpticsGroup": [1], "rlnImagePixelSize": [1.5]})
    star = _write_star(project, "p.star", {"optics": optics, "particles": particles})
    job = project / "Select" / "job001"
    r = pf.apply_filter(project, star, job, "rlnAutopickFigureOfMerit", 0.5, 1.0)
    assert (r["kept"], r["total"]) == (4, 6) and "python" in r["engine"]
    out = starfile.read(job / "particles.star", always_dict=True)
    assert list(out) == ["optics", "particles"] and len(out["optics"]) == 1
    assert sorted(out["particles"]["rlnAutopickFigureOfMerit"]) == [0.5, 0.8, 0.9, 1.0]
    assert "rlnAutopickFigureOfMerit between 0.5 and 1" in (job / "note.txt").read_text()


def test_reapplying_replaces_the_output_and_the_source_is_never_modified(project, monkeypatch):
    _without_star_handler(monkeypatch)
    star = _write_star(project, "p.star", {"particles": _tomo_picks()})
    before = (project / star).read_bytes()
    job = project / "Select" / "job001"
    pf.apply_filter(project, star, job, "rlnAutopickFigureOfMerit", 0.0, 1.0)
    assert len(starfile.read(job / "particles.star")) == 6
    pf.apply_filter(project, star, job, "rlnAutopickFigureOfMerit", 0.95, 1.0)
    assert len(starfile.read(job / "particles.star")) == 1
    assert (project / star).read_bytes() == before


def test_the_command_is_relions_own_select_on_value(project):
    cmd = pf.select_command(Path("Picks/job1/particles.star"), Path("Select/job2/particles.star"),
                            "rlnAutopickFigureOfMerit", 0.8, 1.0)
    assert cmd == ["relion_star_handler", "--i", "Picks/job1/particles.star", "--o", "Select/job2/particles.star",
                   "--select", "rlnAutopickFigureOfMerit", "--minval", "0.8", "--maxval", "1"]


@pytest.mark.skipif(not HAVE_STAR_HANDLER, reason="needs a real RELION install")
def test_the_python_fallback_keeps_exactly_the_rows_real_relion_does(project, monkeypatch):
    monkeypatch.setenv("PATH", f"{Path(REAL_STAR_HANDLER).parent}{os.pathsep}{os.environ['PATH']}")
    df = _tomo_picks().drop(columns=["rlnImageName"])
    star = _write_star(project, "p.star", {"particles": df})
    for lo, hi, expected in ((0.5, 1.0, 4), (0.5, 0.9, 2), (0.2, 1.0, 4), (0.5, 0.75, 1)):   # incl. 32-bit boundaries
        real = pf.apply_filter(project, star, project / "Select" / "job001", "rlnAutopickFigureOfMerit", lo, hi)
        assert real["engine"] == "relion_star_handler" and "relion_star_handler --i p.star --o Select/job001" in real["command"]
        a = starfile.read(project / "Select/job001/particles.star")
        # the python engine, and the counts shown before writing, agree with what RELION wrote
        assert pf.counts(pf.load_table(project, star), "rlnAutopickFigureOfMerit", lo, hi)["kept"] == len(a) == expected
        assert real["kept"] == expected
    _without_star_handler(monkeypatch)
    mine = pf.apply_filter(project, star, project / "Select" / "job002", "rlnAutopickFigureOfMerit", 0.2, 1.0)
    assert mine["kept"] == 4 and "python" in mine["engine"]                    # same rows as real RELION kept above
    pf.apply_filter(project, star, project / "Select" / "job003", "rlnAutopickFigureOfMerit", 0.5, 0.75)
    b = starfile.read(project / "Select/job003/particles.star")                # and the same table, for the last range
    assert len(b) == 1
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True), check_dtype=False, rtol=1e-5)


def test_a_failing_relion_star_handler_is_reported_plainly(project, monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "relion_star_handler"
    shim.write_text("#!/bin/sh\necho 'ERROR: something went wrong' >&2\nexit 1\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    star = _write_star(project, "p.star", {"particles": _tomo_picks()})
    with pytest.raises(pf.PickFilterError, match="something went wrong"):
        pf.apply_filter(project, star, project / "Select" / "job001", "rlnAutopickFigureOfMerit", 0.5, 0.9)


# ---- reading input safely -------------------------------------------------------------

def test_the_picks_file_must_be_a_readable_star_file_inside_the_project(project, tmp_path):
    with pytest.raises(pf.PickFilterError, match="no particles STAR"):
        pf.load_table(project, "")
    with pytest.raises(pf.PickFilterError, match="not found"):
        pf.load_table(project, "nope.star")
    with pytest.raises(viz.VizError, match="outside the project"):
        pf.load_table(project, "/etc/passwd")
    (project / "junk.star").write_text("this is not a star file\n")
    with pytest.raises(pf.PickFilterError):
        pf.load_table(project, "junk.star")


def test_a_changed_file_is_read_again(project):
    star = _write_star(project, "p.star", {"particles": _tomo_picks()})
    assert pf.load_table(project, star).n_rows == 6
    time.sleep(0.01)
    _write_star(project, "p.star", {"particles": _tomo_picks().iloc[:2]})
    assert pf.load_table(project, star).n_rows == 2


def test_a_fresh_sessions_range_keeps_every_pick(project):
    """A blank bound has no value, and RELION's -9999. / 9999. defaults do not cover every column; a
    new job must still keep everything. A bound the user typed is used exactly as typed."""
    t = pf.load_table(project, _write_star(project, "p.star", {"particles": _tomo_picks()}))
    lo, hi = pf.parse_range("rlnAutopickFigureOfMerit", "", None, t)
    assert lo < 0.1 and hi > 1.0 and pf.counts(t, "rlnAutopickFigureOfMerit", lo, hi)["kept"] == 6
    assert pf.parse_range("rlnAutopickFigureOfMerit", "-9999.", "9999.", t) == (-9999.0, 9999.0)   # covers: as is
    assert pf.parse_range("rlnAutopickFigureOfMerit", "0.5", " 0.9 ", t) == (0.5, 0.9)
    wide = _tomo_picks()
    wide["rlnDefocusU"] = [10000.0, 12000, 15000, 18000, 20000, 25000]
    t2 = pf.load_table(project, _write_star(project, "w.star", {"particles": wide}))
    lo, hi = pf.parse_range("rlnDefocusU", "-9999.", "9999.", t2)             # defaults that cut the data
    assert hi > 25000 and pf.counts(t2, "rlnDefocusU", lo, hi)["kept"] == 6
    assert pf.parse_range("rlnDefocusU", "-9999.", "20000", t2)[1] == 20000.0   # typed: kept
    with pytest.raises(pf.PickFilterError, match="not a number"):
        pf.parse_range("rlnAutopickFigureOfMerit", "abc", "", t)


def test_job_star_updates_keep_the_files_own_style(project):
    """RELION rewrites job.star after registering a job -- bare values, aligned columns -- and the
    update must not put quotes into a file RELION wrote."""
    job = project / "Select" / "job001"
    job.mkdir(parents=True)
    relion_style = ("\ndata_joboptions_values\n\nloop_ \n_rlnJobOptionVariable #1 \n_rlnJobOptionValue #2 \n"
                    "do_select_values        Yes \n   fn_data Picks/job040/particles.star \n"
                    "select_label rlnCtfMaxResolution \nselect_maxval      9999. \nselect_minval     -9999. \n")
    (job / "job.star").write_text(relion_style)
    pf.record_selection(job, None, "rlnAutopickFigureOfMerit", 0.9, 1.009)
    text = (job / "job.star").read_text()
    assert "select_label rlnAutopickFigureOfMerit \n" in text and "select_minval     0.9 \n" in text
    assert "select_maxval      1.009 \n" in text and '"' not in text
    assert "do_select_values        Yes \n" in text and "   fn_data Picks/job040/particles.star \n" in text


def test_the_job_remembers_the_selection_in_its_values_and_its_job_star(project):
    job = project / "Select" / "job001"
    job.mkdir(parents=True)
    (job / "job.star").write_text(
        '\ndata_joboptions_values\n\nloop_\n_rlnJobOptionVariable #1\n_rlnJobOptionValue #2\n'
        'fn_data "p.star"\nselect_label "rlnX"\nselect_minval "-9999."\nselect_maxval "9999."\ndo_select_values "Yes"\n')
    values = {"fn_data": "p.star"}
    sel = pf.record_selection(job, values, "rlnAutopickFigureOfMerit", 0.75, 1.0)
    assert sel == {"select_label": "rlnAutopickFigureOfMerit", "select_minval": "0.75", "select_maxval": "1"}
    assert values["select_minval"] == "0.75" and values["fn_data"] == "p.star"
    text = (job / "job.star").read_text()
    assert 'select_label "rlnAutopickFigureOfMerit"' in text and 'select_minval "0.75"' in text
    assert 'fn_data "p.star"' in text and 'do_select_values "Yes"' in text           # the rest is untouched
    assert pf.record_selection(project / "nothing", None, "c", 0, 1)["select_label"] == "c"   # no job.star: fine


# ---- the job and its endpoints -----------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(pf.shutil, "which", lambda name: None)             # the python engine: no RELION needed
    project = tmp_path / "project"
    project_manager.init_new_project(project)
    main.run_manager.set_project_dir(project)
    # entered as a context manager so one event loop lives across requests: a picker-style job is a
    # fire-and-forget task, which a per-request loop would cancel (see test_main_endpoints' picker_client)
    with fastapi_testclient.TestClient(main.app) as http:
        yield http, project


def _start(http, **fields):
    resp = http.post("/api/runs", json={"internal_name": "PickFilter", "field_values": fields})
    assert resp.status_code == 200, resp.text
    run = resp.json()
    for _ in range(100):
        got = http.get(f"/api/runs/{run['run_id']}").json()
        if got["stdout_lines"] or got["status"] in ("failed",):
            break
        time.sleep(0.05)
    return run, got


def test_starting_the_job_writes_a_pass_through_and_stays_open(client):
    http, project = client
    star = _write_star(project, "Picks/job001/particles.star", {"particles": _tomo_picks()})
    run, state = _start(http, fn_data=star)
    assert state["status"] == "running"
    out = starfile.read(Path(run["cwd"]) / "particles.star")
    assert len(out) == 6                                         # RELION's default range keeps every pick
    assert "Read 6 picks" in "\n".join(state["stdout_lines"]) and "kept 6" in "\n".join(state["stdout_lines"])
    assert state["field_values"]["select_label"] == "rlnAutopickFigureOfMerit"
    assert Path(run["cwd"]).parent.name == "Select"              # numbered as the Select job it is


def test_a_label_the_file_does_not_have_falls_back_to_a_usable_column(client):
    http, project = client
    star = _write_star(project, "p.star", {"particles": _tomo_picks(with_score=False)})
    run, state = _start(http, fn_data=star, select_label="rlnCtfMaxResolution")
    assert state["status"] == "running"
    assert state["field_values"]["select_label"] == "rlnMaxValueProbDistribution"
    assert "rlnCtfMaxResolution is not a numeric column" in "\n".join(state["stdout_lines"])


def test_starting_without_a_usable_file_fails_with_the_reason(client):
    http, project = client
    empty = http.post("/api/runs", json={"internal_name": "PickFilter", "field_values": {}}).json()
    for _ in range(100):
        got = http.get(f"/api/runs/{empty['run_id']}").json()
        if got["status"] == "failed":
            break
        time.sleep(0.05)
    assert got["status"] == "failed" and "required" in " ".join(got["stderr_lines"] + got["stdout_lines"])
    run, state = _start(http, fn_data="nope.star")
    assert state["status"] == "failed" and "not found" in " ".join(state["stderr_lines"] + state["stdout_lines"])


def test_the_tuner_endpoints_serve_columns_histogram_counts_and_picks(client):
    http, project = client
    star = _write_star(project, "p.star", {"particles": _tomo_picks()})
    preview = _tomograms_star(project, ["TS_001", "TS_002"])
    run, _ = _start(http, fn_data=star, fn_preview=preview)
    base = f"/api/pick-filter/{run['run_id']}"
    info = http.get(f"{base}/info").json()
    assert info["default"] == "rlnAutopickFigureOfMerit" and info["n_rows"] == 6 and info["kind"] == "tomo"
    assert info["has_preview_source"] is True and info["current"]["column"] == "rlnAutopickFigureOfMerit"
    assert http.get(f"{base}/histogram", params={"column": "rlnAutopickFigureOfMerit", "bins": 10}).json()["counts"]
    counts = http.get(f"{base}/counts", params={"column": "rlnAutopickFigureOfMerit", "lo": 0.5, "hi": 1}).json()
    assert counts["kept"] == 4 and {i["name"] for i in counts["images"]} == {"TS_001", "TS_002"}
    image = http.get(f"{base}/image", params={"column": "rlnAutopickFigureOfMerit", "image": "TS_001"}).json()
    assert image["path"] == "Tomograms/TS_001.mrc" and image["n"] == 4
    assert http.get(f"{base}/counts", params={"column": "rlnImageName", "lo": 0, "hi": 1}).status_code == 400
    assert http.get(f"{base}/image", params={"column": "rlnAutopickFigureOfMerit", "image": "x"}).status_code == 400


def test_without_a_preview_star_the_tuner_is_told_tomograms_cannot_be_drawn(client):
    http, project = client
    run, _ = _start(http, fn_data=_write_star(project, "p.star", {"particles": _tomo_picks()}))
    assert http.get(f"/api/pick-filter/{run['run_id']}/info").json()["has_preview_source"] is False


def test_applying_rewrites_the_output_and_the_job_remembers_it(client):
    http, project = client
    star = _write_star(project, "p.star", {"particles": _tomo_picks()})
    run, _ = _start(http, fn_data=star)
    resp = http.post(f"/api/pick-filter/{run['run_id']}/apply",
                     json={"column": "rlnAutopickFigureOfMerit", "lo": 0.75, "hi": 1.0})
    assert resp.status_code == 200, resp.text
    assert resp.json()["kept"] == 3 and resp.json()["selection"]["select_minval"] == "0.75"
    assert len(starfile.read(Path(run["cwd"]) / "particles.star")) == 3
    fv = http.get(f"/api/runs/{run['run_id']}").json()["field_values"]
    assert (fv["select_label"], fv["select_minval"], fv["select_maxval"]) == ("rlnAutopickFigureOfMerit", "0.75", "1")
    assert fv["fn_data"] == star                                  # the rest of the form is untouched
    info = http.get(f"/api/pick-filter/{run['run_id']}/info").json()
    assert info["current"]["min"] == "0.75"                        # a reopened tuner starts from it


def test_applying_is_refused_for_a_bad_column_and_after_done(client):
    http, project = client
    run, _ = _start(http, fn_data=_write_star(project, "p.star", {"particles": _tomo_picks()}))
    url = f"/api/pick-filter/{run['run_id']}/apply"
    assert http.post(url, json={"column": "rlnImageName", "lo": 0, "hi": 1}).status_code == 400
    assert http.post(url, json={"column": "rlnAutopickFigureOfMerit", "lo": 1, "hi": 0}).status_code == 400
    done = http.patch(f"/api/runs/{run['run_id']}", json={"status": "completed"})
    assert done.status_code == 200
    late = http.post(url, json={"column": "rlnAutopickFigureOfMerit", "lo": 0.5, "hi": 1})
    assert late.status_code == 409 and "Continue" in late.json()["detail"]
    assert http.post(f"/api/runs/{run['run_id']}/resume").status_code == 200       # Continue reopens it
    assert http.post(url, json={"column": "rlnAutopickFigureOfMerit", "lo": 0.5, "hi": 1}).status_code == 200


def test_the_endpoints_refuse_other_jobs_and_unknown_runs(client):
    http, project = client
    other = http.post("/api/runs", json={"internal_name": "IsonetDeconv", "command": "true"}).json()
    assert http.get(f"/api/pick-filter/{other['run_id']}/info").status_code == 400
    assert http.get("/api/pick-filter/nope/info").status_code == 404
    assert http.post("/api/pick-filter/nope/apply", json={"column": "c", "lo": 0, "hi": 1}).status_code == 404


def test_overwrite_starts_the_session_afresh(client):
    http, project = client
    star = _write_star(project, "p.star", {"particles": _tomo_picks()})
    run, _ = _start(http, fn_data=star)
    http.post(f"/api/pick-filter/{run['run_id']}/apply", json={"column": "rlnAutopickFigureOfMerit", "lo": 0.95, "hi": 1})
    http.patch(f"/api/runs/{run['run_id']}", json={"status": "completed"})
    again = http.post("/api/runs", json={"internal_name": "PickFilter", "overwrite_run_id": run["run_id"],
                                         "field_values": {"fn_data": star}})
    assert again.status_code == 200, again.text
    for _ in range(100):
        if len(starfile.read(Path(run["cwd"]) / "particles.star")) == 6:
            break
        time.sleep(0.05)
    assert len(starfile.read(Path(run["cwd"]) / "particles.star")) == 6         # back to keeping everything


def test_other_sets_and_dirnames_know_the_job():
    import job_catalog
    assert job_catalog.job_dirname("PickFilter") == "Select"
    assert job_catalog.CUSTOM_JOBS["PickFilter"]["label_new"] == "relion.select.onvalue"
    assert job_catalog.pipeline_type("PickFilter") == "shared"           # for single-particle and tomography
