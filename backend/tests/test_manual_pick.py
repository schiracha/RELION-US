import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import manual_pick

mrcfile = pytest.importorskip("mrcfile")
starfile = pytest.importorskip("starfile")


# --------------------------------------------------------------------------
# SPA
# --------------------------------------------------------------------------


def _spa_project(tmp_path):
    (tmp_path / ".relion_us").mkdir()
    (tmp_path / "MotionCorr" / "job002" / "020").mkdir(parents=True)
    return tmp_path


def test_save_spa_picks_writes_coord_file_and_job_star(tmp_path):
    project = _spa_project(tmp_path)
    job_dir = project / "ManualPick" / "job005"
    mic = "MotionCorr/job002/020/mic001.mrc"
    result = manual_pick.save_spa_picks(
        project, job_dir, mic,
        [{"x": 10.0, "y": 20.0, "class": 1}, {"x": 30.0, "y": 40.0, "class": 2}],
    )
    assert result["n_picks"] == 2
    assert result["n_micrographs"] == 1
    coord_path = Path(result["coord_path"])
    assert coord_path.is_file()
    blocks = starfile.read(coord_path, always_dict=True)
    df = blocks[""]
    assert list(df["rlnCoordinateX"]) == [10.0, 30.0]
    assert list(df["rlnAnglePsi"]) == [-999.0, -999.0]
    assert list(df["rlnParticleSelectionType"]) == [1, 2]

    job_star = job_dir / "manualpick.star"
    assert job_star.is_file()
    jblocks = starfile.read(job_star, always_dict=True)
    jdf = jblocks["coordinate_files"]
    assert jdf["rlnMicrographName"].iloc[0] == mic
    assert jdf["rlnMicrographCoordinates"].iloc[0] == str(coord_path.relative_to(project))


def test_spa_picks_from_different_subdirs_dont_collide(tmp_path):
    project = _spa_project(tmp_path)
    (project / "MotionCorr" / "job002" / "021").mkdir(parents=True)
    job_dir = project / "ManualPick" / "job005"
    manual_pick.save_spa_picks(
        project, job_dir, "MotionCorr/job002/020/mic001.mrc", [{"x": 1.0, "y": 1.0}])
    manual_pick.save_spa_picks(
        project, job_dir, "MotionCorr/job002/021/mic001.mrc", [{"x": 2.0, "y": 2.0}])
    p1 = manual_pick.spa_coord_path(job_dir, project, "MotionCorr/job002/020/mic001.mrc")
    p2 = manual_pick.spa_coord_path(job_dir, project, "MotionCorr/job002/021/mic001.mrc")
    assert p1 != p2
    assert p1.is_file() and p2.is_file()


def test_save_spa_picks_empty_list_removes_coord_file_and_job_row(tmp_path):
    project = _spa_project(tmp_path)
    job_dir = project / "ManualPick" / "job005"
    mic = "MotionCorr/job002/020/mic001.mrc"
    manual_pick.save_spa_picks(project, job_dir, mic, [{"x": 1.0, "y": 1.0}])
    result = manual_pick.save_spa_picks(project, job_dir, mic, [])
    assert result["n_picks"] == 0
    assert result["n_micrographs"] == 0
    assert not Path(result["coord_path"]).exists()
    assert not (job_dir / "manualpick.star").exists()


def test_load_spa_picks_round_trips(tmp_path):
    project = _spa_project(tmp_path)
    job_dir = project / "ManualPick" / "job005"
    mic = "MotionCorr/job002/020/mic001.mrc"
    manual_pick.save_spa_picks(
        project, job_dir, mic, [{"x": 5.0, "y": 6.0, "class": 3}])
    loaded = manual_pick.load_spa_picks(project, job_dir, mic)
    assert loaded == [{"x": 5.0, "y": 6.0, "class": 3}]


def test_list_spa_micrographs_from_star(tmp_path):
    project = _spa_project(tmp_path)
    df = pd.DataFrame({"rlnMicrographName": [
        "MotionCorr/job002/020/mic001.mrc", "MotionCorr/job002/020/mic002.mrc",
    ]})
    starfile.write({"micrographs": df}, project / "micrographs.star", overwrite=True)
    names = manual_pick.list_spa_micrographs(project, "micrographs.star")
    assert names == [
        "MotionCorr/job002/020/mic001.mrc", "MotionCorr/job002/020/mic002.mrc",
    ]


def test_list_spa_micrographs_from_wildcard(tmp_path):
    project = _spa_project(tmp_path)
    for name in ("a.mrc", "b.mrc"):
        (project / "MotionCorr" / "job002" / "020" / name).write_bytes(b"")
    names = manual_pick.list_spa_micrographs(project, "MotionCorr/job002/020/*.mrc")
    assert sorted(Path(n).name for n in names) == ["a.mrc", "b.mrc"]


def test_list_spa_micrographs_missing_star_raises(tmp_path):
    project = _spa_project(tmp_path)
    with pytest.raises(manual_pick.ManualPickError, match="not found"):
        manual_pick.list_spa_micrographs(project, "nope.star")


def test_load_spa_picks_missing_file_returns_empty(tmp_path):
    project = _spa_project(tmp_path)
    job_dir = project / "ManualPick" / "job005"
    assert manual_pick.load_spa_picks(project, job_dir, "no/such/mic.mrc") == []


# --------------------------------------------------------------------------
# Tomography
# --------------------------------------------------------------------------


def _tomo_project(tmp_path, name="TS_01", nx=40, ny=50, nz=30, voxel=10.0):
    (tmp_path / ".relion_us").mkdir(exist_ok=True)
    vol = (np.random.rand(nz, ny, nx) * 100).astype(np.float32)
    with mrcfile.new(tmp_path / f"{name}.mrc", overwrite=True) as m:
        m.set_data(vol)
        m.voxel_size = voxel
    tomo_df = pd.DataFrame({
        "rlnTomoName": [name],
        "rlnTomoReconstructedTomogram": [f"{name}.mrc"],
    })
    starfile.write({"tomograms": tomo_df}, tmp_path / "tomograms.star", overwrite=True)
    return tmp_path


def test_save_tomo_picks_writes_annotation_and_particles(tmp_path):
    project = _tomo_project(tmp_path)
    job_dir = project / "Picks" / "job006"
    result = manual_pick.save_tomo_picks(
        project, job_dir, "TS_01",
        [{"x": 20.0, "y": 25.0, "z": 15.0, "class": 1}],  # dead center voxel
        "tomograms.star",
    )
    assert result["n_particles"] == 1
    ann_path = Path(result["annotation_path"])
    assert ann_path.is_file()
    ann_blocks = starfile.read(ann_path, always_dict=True)
    adf = ann_blocks[""]
    assert adf["rlnTomoName"].iloc[0] == "TS_01"
    assert adf["rlnCoordinateX"].iloc[0] == 20.0

    particles_path = job_dir / "particles.star"
    assert particles_path.is_file()
    pblocks = starfile.read(particles_path, always_dict=True)
    pdf = pblocks["particles"]
    # dead-center voxel (nx/2, ny/2, nz/2) -> centered Angstrom coords ~ 0
    assert pdf["rlnCenteredCoordinateXAngst"].iloc[0] == pytest.approx(0.0)
    assert pdf["rlnCenteredCoordinateYAngst"].iloc[0] == pytest.approx(0.0)
    assert pdf["rlnCenteredCoordinateZAngst"].iloc[0] == pytest.approx(0.0)

    optset_path = job_dir / "optimisation_set.star"
    assert optset_path.is_file()
    oblocks = starfile.read(optset_path, always_dict=True)
    odf = oblocks["optimisation_set"]
    # Project-root-relative, matching rlnTomoTomogramsFile's own convention
    # below -- a bare "particles.star" is unresolvable from where RELION
    # jobs actually run (the project root, not job_dir). Confirmed for
    # real: TomoSubtomo failed immediately with "MetaDataTable::read: File
    # particles.star does not exist" before this fix.
    assert odf["rlnTomoParticlesFile"].iloc[0] == "Picks/job006/particles.star"
    assert odf["rlnTomoTomogramsFile"].iloc[0] == "tomograms.star"


def test_tomo_particles_star_round_trips_through_viz_load_picks(tmp_path):
    """The Angstrom conversion here must be the exact inverse of viz.py's
    load_picks (voxel -> Angst here, Angst -> voxel there) -- otherwise a
    saved pick would visibly move when the job's own output is reloaded."""
    import viz

    project = _tomo_project(tmp_path)
    job_dir = project / "Picks" / "job006"
    manual_pick.save_tomo_picks(
        project, job_dir, "TS_01",
        [{"x": 12.0, "y": 33.0, "z": 7.0, "class": 1}],
        "tomograms.star",
    )
    vinfo = viz.volume_info(project, "TS_01.mrc")
    loaded = viz.load_picks(
        project, str(job_dir / "particles.star"), tomo_name="TS_01", volume=vinfo)
    assert loaded["matched"] is True
    assert loaded["picks"][0]["x"] == pytest.approx(12.0)
    assert loaded["picks"][0]["y"] == pytest.approx(33.0)
    assert loaded["picks"][0]["z"] == pytest.approx(7.0)


def test_save_tomo_picks_combines_multiple_tomograms(tmp_path):
    project = _tomo_project(tmp_path, name="TS_01")
    _tomo_project(project, name="TS_02")  # second tomogram, same tomograms.star needed
    tomo_df = pd.DataFrame({
        "rlnTomoName": ["TS_01", "TS_02"],
        "rlnTomoReconstructedTomogram": ["TS_01.mrc", "TS_02.mrc"],
    })
    starfile.write({"tomograms": tomo_df}, project / "tomograms.star", overwrite=True)

    job_dir = project / "Picks" / "job006"
    manual_pick.save_tomo_picks(
        project, job_dir, "TS_01", [{"x": 1.0, "y": 1.0, "z": 1.0}], "tomograms.star")
    result = manual_pick.save_tomo_picks(
        project, job_dir, "TS_02", [{"x": 2.0, "y": 2.0, "z": 2.0}], "tomograms.star")
    assert result["n_particles"] == 2
    pdf = starfile.read(job_dir / "particles.star", always_dict=True)["particles"]
    assert sorted(pdf["rlnTomoName"]) == ["TS_01", "TS_02"]


def test_save_tomo_picks_empty_removes_job_level_files(tmp_path):
    project = _tomo_project(tmp_path)
    job_dir = project / "Picks" / "job006"
    manual_pick.save_tomo_picks(
        project, job_dir, "TS_01", [{"x": 1.0, "y": 1.0, "z": 1.0}], "tomograms.star")
    result = manual_pick.save_tomo_picks(project, job_dir, "TS_01", [], "tomograms.star")
    assert result["n_particles"] == 0
    assert not (job_dir / "particles.star").exists()
    assert not (job_dir / "optimisation_set.star").exists()


def test_load_tomo_picks_round_trips(tmp_path):
    project = _tomo_project(tmp_path)
    job_dir = project / "Picks" / "job006"
    manual_pick.save_tomo_picks(
        project, job_dir, "TS_01", [{"x": 9.0, "y": 8.0, "z": 7.0}], "tomograms.star")
    loaded = manual_pick.load_tomo_picks(project, job_dir, "TS_01")
    assert loaded == [{"x": 9.0, "y": 8.0, "z": 7.0, "class": 1}]


# --------------------------------------------------------------------------
# clear_spa_picks / clear_tomo_picks -- what Overwrite calls (via
# custom_jobs.run_manual_pick/run_tomo_manual_pick) to genuinely start
# clean, as opposed to Continue (job_runner.resume_run), which touches
# nothing on disk.
# --------------------------------------------------------------------------


def test_clear_spa_picks_removes_job_star_and_coord_files(tmp_path):
    project = _spa_project(tmp_path)
    job_dir = project / "ManualPick" / "job005"
    manual_pick.save_spa_picks(
        project, job_dir, "MotionCorr/job002/020/mic001.mrc", [{"x": 1.0, "y": 1.0}])
    manual_pick.save_spa_picks(
        project, job_dir, "MotionCorr/job002/021/mic001.mrc", [{"x": 2.0, "y": 2.0}])
    assert manual_pick.clear_spa_picks(job_dir) == 3  # job-level star + 2 coord files
    assert not (job_dir / "manualpick.star").exists()
    assert list(job_dir.glob("*_manualpick.star")) == []


def test_clear_spa_picks_on_a_fresh_job_dir_is_a_noop(tmp_path):
    job_dir = tmp_path / "ManualPick" / "job001"
    job_dir.mkdir(parents=True)
    assert manual_pick.clear_spa_picks(job_dir) == 0


def test_clear_tomo_picks_removes_everything(tmp_path):
    project = _tomo_project(tmp_path)
    job_dir = project / "Picks" / "job006"
    manual_pick.save_tomo_picks(
        project, job_dir, "TS_01", [{"x": 1.0, "y": 1.0, "z": 1.0}], "tomograms.star")
    assert manual_pick.clear_tomo_picks(job_dir) == 3  # annotation + particles + optimisation_set
    assert not (job_dir / "particles.star").exists()
    assert not (job_dir / "optimisation_set.star").exists()
    assert list((job_dir / "annotations").glob("*_particles.star")) == []


def test_clear_tomo_picks_on_a_fresh_job_dir_is_a_noop(tmp_path):
    job_dir = tmp_path / "Picks" / "job001"
    job_dir.mkdir(parents=True)
    assert manual_pick.clear_tomo_picks(job_dir) == 0
