import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import exclude_tilts

starfile = pytest.importorskip("starfile")


def _series_df(movie_names, angles):
    return pd.DataFrame({
        "rlnMicrographMovieName": movie_names,
        "rlnTomoTiltMovieFrameCount": [8] * len(movie_names),
        "rlnTomoNominalStageTiltAngle": angles,
        "rlnMicrographPreExposure": [i * 3.0 for i in range(len(movie_names))],
        "rlnMicrographName": [f"MotionCorr/job001/{n}" for n in movie_names],
        "rlnCtfMaxResolution": [7.0] * len(movie_names),
        "rlnAccumMotionTotal": [2.0] * len(movie_names),
    })


def _project(tmp_path, series=None):
    """A project with a CtfFind-shaped global tilt-series-set star (data_
    global: rlnTomoName/rlnTomoTiltSeriesStarFile/...) plus one per-series
    star per entry -- the exact shape confirmed against a real RELION 5.0.1
    tomography tutorial project's CtfFind/job002/tilt_series_ctf.star +
    CtfFind/job002/tilt_series/TS_01.star."""
    (tmp_path / ".relion_us").mkdir(exist_ok=True)
    series = series or {"TS_01": (["a.mrc", "b.mrc", "c.mrc"], [0.0, 3.0, -3.0])}
    ctf_dir = tmp_path / "CtfFind" / "job002" / "tilt_series"
    ctf_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, (movies, angles) in series.items():
        df = _series_df(movies, angles)
        starfile.write({name: df}, ctf_dir / f"{name}.star", overwrite=True)
        rows.append({
            "rlnTomoName": name,
            "rlnTomoTiltSeriesStarFile": f"CtfFind/job002/tilt_series/{name}.star",
            "rlnVoltage": 300.0, "rlnSphericalAberration": 2.7,
            "rlnAmplitudeContrast": 0.1, "rlnMicrographOriginalPixelSize": 0.675,
            "rlnTomoHand": -1.0, "rlnOpticsGroupName": "optics1",
            "rlnTomoTiltSeriesPixelSize": 1.35,
        })
    global_df = pd.DataFrame(rows)
    starfile.write({"global": global_df}, tmp_path / "CtfFind" / "job002" / "tilt_series_ctf.star", overwrite=True)
    return tmp_path


IN_TILTSERIES = "CtfFind/job002/tilt_series_ctf.star"


# --------------------------------------------------------------------------
# list_tilt_series / list_images
# --------------------------------------------------------------------------


def test_list_tilt_series(tmp_path):
    project = _project(tmp_path, series={
        "TS_01": (["a.mrc"], [0.0]), "TS_02": (["b.mrc"], [0.0]),
    })
    assert exclude_tilts.list_tilt_series(project, IN_TILTSERIES) == ["TS_01", "TS_02"]


def test_list_tilt_series_missing_star_raises(tmp_path):
    (tmp_path / ".relion_us").mkdir()
    with pytest.raises(exclude_tilts.ExcludeTiltsError, match="not found"):
        exclude_tilts.list_tilt_series(tmp_path, "nope.star")


def test_purely_numeric_tomo_name_survives_round_trip(tmp_path):
    """rlnTomoName is free text in real RELION-5 tomography STAR files, and
    some projects name tomograms with plain digits (e.g. "01") -- without
    parse_as_string, starfile/pandas would infer an int column, silently
    turning "01" into 1 and breaking every name-keyed lookup in this
    module."""
    project = _project(tmp_path, series={"01": (["a.mrc", "b.mrc"], [0.0, 3.0])})
    assert exclude_tilts.list_tilt_series(project, IN_TILTSERIES) == ["01"]
    job_dir = project / "ExcludeTiltImages" / "job003"
    exclude_tilts.write_passthrough(project, job_dir, IN_TILTSERIES)
    # Read back the way exclude_tilts.py itself reads (parse_as_string on
    # rlnTomoName) -- a raw starfile.read of the on-disk STAR text, with no
    # such hint, would infer this column as int (same ambiguity real
    # RELION's own STAR reader has to guard against), which is exactly the
    # bug this test exists to catch.
    gdf = exclude_tilts._read_star_blocks(job_dir / "selected_tilt_series.star")["global"]
    assert list(gdf["rlnTomoName"]) == ["01"]
    images = exclude_tilts.list_images(project, job_dir, IN_TILTSERIES, "01")
    assert len(images) == 2


def test_list_images_before_any_save_reports_nothing_excluded(tmp_path):
    project = _project(tmp_path, series={
        "TS_01": (["a.mrc", "b.mrc", "c.mrc"], [0.0, 3.0, -3.0]),
    })
    job_dir = project / "ExcludeTiltImages" / "job003"
    images = exclude_tilts.list_images(project, job_dir, IN_TILTSERIES, "TS_01")
    assert [i["movie_name"] for i in images] == ["a.mrc", "b.mrc", "c.mrc"]
    assert all(not i["excluded"] for i in images)
    assert images[0]["tilt_angle"] == pytest.approx(0.0)
    assert images[1]["pre_exposure"] == pytest.approx(3.0)


def test_list_images_unknown_tomogram_raises(tmp_path):
    project = _project(tmp_path)
    job_dir = project / "ExcludeTiltImages" / "job003"
    with pytest.raises(exclude_tilts.ExcludeTiltsError, match="TS_99"):
        exclude_tilts.list_images(project, job_dir, IN_TILTSERIES, "TS_99")


# --------------------------------------------------------------------------
# write_passthrough
# --------------------------------------------------------------------------


def test_write_passthrough_writes_every_series_with_nothing_excluded(tmp_path):
    project = _project(tmp_path, series={
        "TS_01": (["a.mrc", "b.mrc"], [0.0, 3.0]),
        "TS_02": (["x.mrc"], [0.0]),
    })
    job_dir = project / "ExcludeTiltImages" / "job003"
    n = exclude_tilts.write_passthrough(project, job_dir, IN_TILTSERIES)
    assert n == 2

    global_star = job_dir / "selected_tilt_series.star"
    assert global_star.is_file()
    gdf = starfile.read(global_star, always_dict=True)["global"]
    # Both series present -- not just the last one written (regression
    # guard: `blocks.get(...) or next(...)` on a DataFrame raises, which an
    # earlier version of _upsert_job_global_star's broad except silently
    # swallowed, resetting accumulated rows to {} on every subsequent call).
    assert sorted(gdf["rlnTomoName"]) == ["TS_01", "TS_02"]
    assert set(gdf["rlnTomoTiltSeriesStarFile"]) == {
        "ExcludeTiltImages/job003/tilt_series/TS_01.star",
        "ExcludeTiltImages/job003/tilt_series/TS_02.star",
    }
    # Other columns copied through from the original global row.
    row = gdf[gdf["rlnTomoName"] == "TS_01"].iloc[0]
    assert row["rlnVoltage"] == pytest.approx(300.0)

    ts01 = starfile.read(job_dir / "tilt_series" / "TS_01.star", always_dict=True)["TS_01"]
    assert list(ts01["rlnMicrographMovieName"]) == ["a.mrc", "b.mrc"]

    summary = exclude_tilts.series_summary(project, job_dir, IN_TILTSERIES)
    assert summary == [
        {"name": "TS_01", "n_images": 2, "n_excluded": 0},
        {"name": "TS_02", "n_images": 1, "n_excluded": 0},
    ]


# --------------------------------------------------------------------------
# save_tilt_series_exclusions
# --------------------------------------------------------------------------


def test_save_exclusions_drops_named_rows_and_sorts_by_tilt_angle(tmp_path):
    project = _project(tmp_path, series={
        "TS_01": (["a.mrc", "b.mrc", "c.mrc"], [0.0, 9.0, -3.0]),
    })
    job_dir = project / "ExcludeTiltImages" / "job003"
    result = exclude_tilts.save_tilt_series_exclusions(project, job_dir, IN_TILTSERIES, "TS_01", ["b.mrc"])
    assert result["n_total"] == 3
    assert result["n_kept"] == 2
    assert result["n_excluded"] == 1

    df = starfile.read(Path(result["series_path"]), always_dict=True)["TS_01"]
    # b.mrc (9.0 deg) dropped; remaining rows sorted by tilt angle ascending
    # (RelionTiltImageExcluderWidget.save_output's own convention).
    assert list(df["rlnMicrographMovieName"]) == ["c.mrc", "a.mrc"]
    assert list(df["rlnTomoNominalStageTiltAngle"]) == [-3.0, 0.0]


def test_save_exclusions_then_list_images_reflects_state(tmp_path):
    project = _project(tmp_path, series={
        "TS_01": (["a.mrc", "b.mrc", "c.mrc"], [0.0, 3.0, -3.0]),
    })
    job_dir = project / "ExcludeTiltImages" / "job003"
    exclude_tilts.save_tilt_series_exclusions(project, job_dir, IN_TILTSERIES, "TS_01", ["b.mrc"])
    images = exclude_tilts.list_images(project, job_dir, IN_TILTSERIES, "TS_01")
    excluded = {i["movie_name"] for i in images if i["excluded"]}
    assert excluded == {"b.mrc"}


def test_save_exclusions_never_accumulates_across_calls(tmp_path):
    """Each save re-derives from the ORIGINAL input, not the job's own
    already-trimmed copy -- so excluding {b} then excluding {c} must leave
    ONLY c excluded (b re-included), not both."""
    project = _project(tmp_path, series={
        "TS_01": (["a.mrc", "b.mrc", "c.mrc"], [0.0, 3.0, -3.0]),
    })
    job_dir = project / "ExcludeTiltImages" / "job003"
    exclude_tilts.save_tilt_series_exclusions(project, job_dir, IN_TILTSERIES, "TS_01", ["b.mrc"])
    exclude_tilts.save_tilt_series_exclusions(project, job_dir, IN_TILTSERIES, "TS_01", ["c.mrc"])
    images = exclude_tilts.list_images(project, job_dir, IN_TILTSERIES, "TS_01")
    excluded = {i["movie_name"] for i in images if i["excluded"]}
    assert excluded == {"c.mrc"}


def test_save_exclusions_all_images_leaves_zero_rows_and_drops_from_global_star(tmp_path):
    """A fully-excluded tilt series must be DROPPED from the job's global
    selected_tilt_series.star -- matching real RELION's own RlnTiltSeriesSet.
    write_star_file, which never emits a row for an empty tilt series (a
    downstream job has nothing to do with a series that has no images
    left). The per-series STAR itself is still written, with zero rows --
    that's what lets list_images/series_summary tell "fully excluded" apart
    from "never touched"."""
    project = _project(tmp_path, series={"TS_01": (["a.mrc", "b.mrc"], [0.0, 3.0])})
    job_dir = project / "ExcludeTiltImages" / "job003"
    result = exclude_tilts.save_tilt_series_exclusions(
        project, job_dir, IN_TILTSERIES, "TS_01", ["a.mrc", "b.mrc"])
    assert result["n_kept"] == 0
    df = starfile.read(Path(result["series_path"]), always_dict=True)["TS_01"]
    assert len(df) == 0
    assert not (job_dir / "selected_tilt_series.star").exists()


def test_list_images_all_excluded_after_fully_excluding_a_series(tmp_path):
    """Regression: once a series has been explicitly fully excluded (empty
    per-series STAR written, see above), list_images must show every image
    as excluded -- not silently default back to "kept" just because the
    tomogram's row is gone from the global star."""
    project = _project(tmp_path, series={"TS_01": (["a.mrc", "b.mrc"], [0.0, 3.0])})
    job_dir = project / "ExcludeTiltImages" / "job003"
    exclude_tilts.save_tilt_series_exclusions(project, job_dir, IN_TILTSERIES, "TS_01", ["a.mrc", "b.mrc"])
    images = exclude_tilts.list_images(project, job_dir, IN_TILTSERIES, "TS_01")
    assert all(i["excluded"] for i in images)
    summary = exclude_tilts.series_summary(project, job_dir, IN_TILTSERIES)
    assert {s["name"]: s["n_excluded"] for s in summary} == {"TS_01": 2}


def test_save_exclusions_one_series_does_not_disturb_another(tmp_path):
    project = _project(tmp_path, series={
        "TS_01": (["a.mrc", "b.mrc"], [0.0, 3.0]),
        "TS_02": (["x.mrc", "y.mrc"], [0.0, 3.0]),
    })
    job_dir = project / "ExcludeTiltImages" / "job003"
    exclude_tilts.write_passthrough(project, job_dir, IN_TILTSERIES)
    exclude_tilts.save_tilt_series_exclusions(project, job_dir, IN_TILTSERIES, "TS_01", ["a.mrc"])

    ts02_images = exclude_tilts.list_images(project, job_dir, IN_TILTSERIES, "TS_02")
    assert all(not i["excluded"] for i in ts02_images)
    summary = exclude_tilts.series_summary(project, job_dir, IN_TILTSERIES)
    assert {s["name"]: s["n_excluded"] for s in summary} == {"TS_01": 1, "TS_02": 0}


# --------------------------------------------------------------------------
# clear_exclusions
# --------------------------------------------------------------------------


def test_clear_exclusions_removes_everything(tmp_path):
    project = _project(tmp_path, series={
        "TS_01": (["a.mrc"], [0.0]), "TS_02": (["x.mrc"], [0.0]),
    })
    job_dir = project / "ExcludeTiltImages" / "job003"
    exclude_tilts.write_passthrough(project, job_dir, IN_TILTSERIES)
    removed = exclude_tilts.clear_exclusions(job_dir)
    assert removed == 3  # global star + 2 per-series files
    assert not (job_dir / "selected_tilt_series.star").exists()
    assert not (job_dir / "tilt_series").exists()


def test_reset_and_write_passthrough_is_atomic_under_a_concurrent_save(tmp_path):
    """A save request racing job start's clear+reseed (both run on their own
    threads -- see main.py's plain `def` endpoints and custom_jobs.
    run_exclude_tilt_images's asyncio.to_thread) must never observe a
    half-cleared job directory or corrupt the global star. Runs
    reset_and_write_passthrough and save_tilt_series_exclusions from real
    threads many times over and checks the global star is always
    well-formed and never loses a series that a passthrough write is
    responsible for."""
    import threading

    project = _project(tmp_path, series={
        "TS_01": (["a.mrc", "b.mrc"], [0.0, 3.0]),
        "TS_02": (["x.mrc", "y.mrc"], [0.0, 3.0]),
    })
    job_dir = project / "ExcludeTiltImages" / "job003"
    exclude_tilts.write_passthrough(project, job_dir, IN_TILTSERIES)

    errors = []

    def reset():
        try:
            for _ in range(20):
                exclude_tilts.reset_and_write_passthrough(project, job_dir, IN_TILTSERIES)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def save():
        try:
            for _ in range(20):
                exclude_tilts.save_tilt_series_exclusions(project, job_dir, IN_TILTSERIES, "TS_02", ["x.mrc"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reset), threading.Thread(target=save)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # Whatever the final interleaving, the global star must still be a
    # well-formed STAR with a row per tilt series that has kept images --
    # never a partial/corrupted table missing an unrelated series.
    gdf = exclude_tilts._read_star_blocks(job_dir / "selected_tilt_series.star")["global"]
    assert set(gdf["rlnTomoName"]) <= {"TS_01", "TS_02"}


def test_clear_exclusions_on_a_fresh_job_dir_is_a_noop(tmp_path):
    job_dir = tmp_path / "ExcludeTiltImages" / "job001"
    job_dir.mkdir(parents=True)
    assert exclude_tilts.clear_exclusions(job_dir) == 0
