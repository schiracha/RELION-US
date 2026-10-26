"""
Make Mask's Progress-tab picture (mask_preview.py): the first finished mask,
over the tomogram it was made from, drawn once and read back after.
"""
import json
import os
import sys
import time
from pathlib import Path

import mrcfile
import numpy as np
import pandas as pd
import pytest
import starfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mask_preview as mp
from isonet_jobs import ISONET_JOB_DEFINITIONS

DEFINITION = ISONET_JOB_DEFINITIONS["IsonetMakeMask"]
SHAPE = (8, 16, 20)


def _write_mrc(path, data, voxel=10.0):
    with mrcfile.new(path, overwrite=True) as m:
        m.set_data(data)
        m.voxel_size = voxel


def _age(path, seconds=10):
    t = time.time() - seconds
    os.utime(path, (t, t))


def _job(tmp_path, names=("TS_1", "TS_2", "TS_3")):
    """A Make Mask job directory: its STAR copy listing the deconvolved tomograms
    (which live in another job's directory, as in a real project)."""
    deconv = tmp_path / "IsonetDeconv" / "job001"
    deconv.mkdir(parents=True)
    job = tmp_path / "IsonetMakeMask" / "job002"
    job.mkdir(parents=True)
    rng = np.random.default_rng(0)
    for n in names:
        _write_mrc(deconv / f"{n}.mrc", rng.normal(size=SHAPE).astype(np.float32))
    starfile.write(pd.DataFrame({
        "rlnIndex": range(1, len(names) + 1), "rlnTomoName": [f"x/{n}.mrc" for n in names],
        "rlnDeconvTomoName": [f"IsonetDeconv/job001/{n}.mrc" for n in names],
        "rlnPixelSize": [10.0] * len(names)}), job / "tomograms.star", overwrite=True)
    return job


def _mask(job, name, fraction_ones=0.25, age=10):
    data = np.zeros(SHAPE, dtype=np.uint16)
    data[:, : int(SHAPE[1] * fraction_ones), :] = 1
    p = job / f"{name}_mask.mrc"
    _write_mrc(p, data)
    _age(p, age)
    return p


def test_nothing_is_available_before_a_mask_exists(tmp_path):
    job = _job(tmp_path)
    out = mp.mask_preview(job, tmp_path, {}, DEFINITION)
    assert out["supported"] and not out["available"]
    assert out["masks_done"] == 0 and out["masks_total"] == 3
    assert not (job / mp.PREVIEW_PNG).exists()


def test_the_first_finished_mask_is_drawn_over_its_tomogram(tmp_path):
    job = _job(tmp_path)
    _mask(job, "TS_2", 0.25)
    out = mp.mask_preview(job, tmp_path, {}, DEFINITION)
    assert out["available"] and out["mask"] == "TS_2_mask.mrc"
    assert out["file"] == mp.PREVIEW_PNG and out["masks_done"] == 1
    assert out["tomogram"].endswith("IsonetDeconv/job001/TS_2.mrc") and out["note"] is None
    assert out["shape"] == list(SHAPE) and out["voxel_size"] == 10.0
    assert 20 < out["mask_percent"] < 30                  # a quarter of the rows, sampled
    png = (job / mp.PREVIEW_PNG).read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    im = Image.open(job / mp.PREVIEW_PNG)
    assert im.width == mp.PANEL_WIDTH and im.height > im.width * SHAPE[1] / SHAPE[2]   # XY panel + XZ strip


def test_the_picture_shows_the_mask_tint_and_outline(tmp_path):
    job = _job(tmp_path)
    _mask(job, "TS_1", 0.5)
    mp.mask_preview(job, tmp_path, {}, DEFINITION)
    from PIL import Image
    a = np.asarray(Image.open(job / mp.PREVIEW_PNG).convert("RGB")).astype(int)
    edge = np.all(a == mp.EDGE, axis=-1)
    tinted = (a[..., 1] > a[..., 0] + 40) & (a[..., 1] > a[..., 2] + 25)
    assert edge.any() and tinted.any()


def test_it_is_drawn_once_and_later_masks_do_not_replace_it(tmp_path):
    job = _job(tmp_path)
    _mask(job, "TS_1", 0.25, age=30)
    first = mp.mask_preview(job, tmp_path, {}, DEFINITION)
    stamp = (job / mp.PREVIEW_PNG).stat().st_mtime_ns
    _mask(job, "TS_2", 0.75, age=10)
    again = mp.mask_preview(job, tmp_path, {}, DEFINITION)
    assert (job / mp.PREVIEW_PNG).stat().st_mtime_ns == stamp
    assert again["mask"] == first["mask"] == "TS_1_mask.mrc"
    assert again["masks_done"] == 2 and again["mask_percent"] == first["mask_percent"]


def test_first_means_first_made_not_first_by_name(tmp_path):
    job = _job(tmp_path)
    _mask(job, "TS_3", age=60)
    _mask(job, "TS_1", age=20)
    assert [p.name for p in mp.finished_masks(job)] == ["TS_3_mask.mrc", "TS_1_mask.mrc"]


def test_a_mask_still_being_written_is_not_used(tmp_path):
    job = _job(tmp_path)
    good = _mask(job, "TS_1")
    partial = job / "TS_2_mask.mrc"
    partial.write_bytes(good.read_bytes()[: good.stat().st_size // 2])      # header + half the data
    _age(partial)
    assert [p.name for p in mp.finished_masks(job)] == ["TS_1_mask.mrc"]
    fresh = _mask(job, "TS_3", age=0)                                       # complete but touched just now
    assert [p.name for p in mp.finished_masks(job)] == ["TS_1_mask.mrc"]
    _age(fresh)
    assert len(mp.finished_masks(job)) == 2


def test_a_missing_or_different_tomogram_still_gives_a_picture_with_a_note(tmp_path):
    job = _job(tmp_path)
    _mask(job, "TS_1")
    (tmp_path / "IsonetDeconv" / "job001" / "TS_1.mrc").unlink()
    out = mp.mask_preview(job, tmp_path, {}, DEFINITION)
    assert out["available"] and "not found" in out["note"] and out["tomogram"] is None

    job2 = _job(tmp_path / "b")
    _mask(job2, "TS_1")
    _write_mrc(tmp_path / "b" / "IsonetDeconv" / "job001" / "TS_1.mrc", np.zeros((4, 5, 6), dtype=np.float32))
    out2 = mp.mask_preview(job2, tmp_path / "b", {}, DEFINITION)
    assert out2["available"] and "differ in size" in out2["note"]


def test_a_mask_with_no_star_row_is_still_drawn(tmp_path):
    job = _job(tmp_path)
    _mask(job, "Other_tomo")
    out = mp.mask_preview(job, tmp_path, {}, DEFINITION)
    assert out["available"] and out["tomogram"] is None


def test_settings_come_from_the_run_with_defaults_filling_the_rest(tmp_path):
    job = _job(tmp_path)
    out = mp.mask_preview(job, tmp_path, {"density_percentage": "35", "z_crop": 0.4}, DEFINITION)
    assert out["settings"] == ["patch size 4", "density 35%", "std 50%", "z-crop 0.4"]


def test_total_follows_the_tomogram_selection(tmp_path):
    job = _job(tmp_path)
    assert mp.mask_preview(job, tmp_path, {"tomo_idx": "1,3"}, DEFINITION)["masks_total"] == 2
    assert mp.mask_preview(job, tmp_path, {"tomo_idx": "2"}, DEFINITION)["masks_total"] == 1


def test_a_failure_drawing_is_reported_not_raised(tmp_path):
    job = _job(tmp_path)
    bad = _mask(job, "TS_1")
    with mrcfile.new(bad, overwrite=True) as m:                  # a 2D "mask": not drawable
        m.set_data(np.zeros((5, 5), dtype=np.uint16))
    _age(bad)
    out = mp.mask_preview(job, tmp_path, {}, DEFINITION)
    assert not out["available"] and "TS_1_mask.mrc" in out["error"]
    assert not (job / mp.PREVIEW_PNG).exists()                   # so it will try again next poll


def test_overwrite_does_not_keep_the_picture():
    """Overwrite empties the job directory except its bookkeeping files (see
    test_job_waiting's overwrite tests), so the first mask of the new run is
    drawn fresh rather than the old one shown."""
    from job_runner import JobRunManager
    assert mp.PREVIEW_PNG not in JobRunManager.OVERWRITE_KEEP
    assert mp.PREVIEW_JSON not in JobRunManager.OVERWRITE_KEEP
