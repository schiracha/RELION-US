"""
manual_pick.py — writes RELION-compatible coordinate STAR output for the
browser-based manual-picking jobs (Manualpick / TomoManualPick), which
replace relion_manualpick's own picking canvas (a desktop FLTK GUI, unusable
headless -- see job_catalog.py's CUSTOM_JOBS docstring) with picks made in
viz.py's in-browser viewer.

Every filename/column choice here is copied directly from real RELION
source (confirmed against RELION 5.0.1, checked out at the path this app's
other modules reference -- src/displayer.cpp/manualpicker.cpp/
pipeline_jobs.cpp for SPA; tomography_python_programs/pick/particles.py and
get_particle_poses/particles.py for tomo), not invented -- so a job this
writes is the same shape a real relion_manualpick/relion_python_tomo_pick
run would have produced, and stays readable by Extract/TomoSubtomo and by
RELION's own GUI.

One deliberate simplification from real RELION: per-micrograph coordinate
files here are flat inside the job directory (SANITIZED_relative_mic_path
_manualpick.star) rather than mirroring the micrograph's own subdirectory
structure the way manualpicker.cpp does. Extract reads the coordinate file
path straight out of the job-level list (see SPA_ constants below) rather
than re-deriving it from convention, so this doesn't affect compatibility --
it only changes what the filename itself looks like on disk. Flattening
still needs the sanitized full relative path (not just the basename) to
stay collision-free: two micrographs named the same but living in different
input subdirectories are a real scenario, not a hypothetical one.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import pandas as pd

import viz

SPA_COORD_FILENAME_SUFFIX = "_manualpick.star"
SPA_JOB_STAR_NAME = "manualpick.star"
SPA_JOB_TABLE_NAME = "coordinate_files"
# EMDL_MICROGRAPH_NAME / EMDL_MICROGRAPH_COORDINATES (metadata_label.h) --
# the exact 2-column shape manualpickerGuiWindow::writeOutputStarfiles()
# writes, and the one relion_preprocess --coord_list (Extract) reads.
SPA_MIC_NAME_COL = "rlnMicrographName"
SPA_MIC_COORDS_COL = "rlnMicrographCoordinates"

TOMO_ANNOTATIONS_DIRNAME = "annotations"
TOMO_PARTICLES_STAR_NAME = "particles.star"
TOMO_OPTSET_STAR_NAME = "optimisation_set.star"


class ManualPickError(Exception):
    """Raised for a bad request (unreadable path, escapes project, etc.);
    the API turns this into a 400."""


def _sanitize_relpath(rel: str) -> str:
    """A relative path, made safe as a flat filename fragment: path
    separators become underscores, kept collision-free across input
    subdirectories (see module docstring)."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", rel.strip("/"))


def _mic_relpath(project_dir: Path, mic_path: str) -> str:
    p = Path(mic_path)
    if p.is_absolute():
        try:
            return str(p.relative_to(project_dir))
        except ValueError:
            return p.name
    return mic_path


# --------------------------------------------------------------------------
# SPA (2D micrographs)
# --------------------------------------------------------------------------


def list_spa_micrographs(project_dir: Path, fn_in: str) -> list[str]:
    """Every micrograph this job's `fn_in` names, project-relative -- either
    a STAR file (any block with an rlnMicrographName column, matching what
    Import/MotionCorr/CtfFind all produce) or a plain unix-style wildcard
    over MRC files (real RELION's own "OR: unix wildcard" alternative,
    initialiseManualpickJob's fn_in docstring)."""
    import starfile

    p = viz._safe(project_dir, fn_in)
    if p.suffix.lower() == ".star":
        if not p.is_file():
            raise ManualPickError(f"micrographs STAR not found: {fn_in}")
        blocks = starfile.read(p, always_dict=True)
        for df in blocks.values():
            if hasattr(df, "columns") and "rlnMicrographName" in df.columns:
                return [str(v) for v in df["rlnMicrographName"].tolist()]
        raise ManualPickError(f"{p.name}: no rlnMicrographName column found")
    # A wildcard pattern -- glob it relative to the project directory, the
    # same base real RELION resolves paths against.
    import glob as _glob

    matches = sorted(_glob.glob(str(project_dir / fn_in)))
    if not matches:
        raise ManualPickError(f"no micrographs match: {fn_in}")
    return [str(Path(m).relative_to(project_dir)) for m in matches]


def spa_coord_path(job_dir: Path, project_dir: Path, mic_path: str) -> Path:
    """Where this micrograph's own pick coordinates live (see module
    docstring for why this is flat rather than subdirectory-mirroring)."""
    rel = _mic_relpath(project_dir, mic_path)
    return job_dir / (_sanitize_relpath(rel) + SPA_COORD_FILENAME_SUFFIX)


def save_spa_picks(
    project_dir: Path, job_dir: Path, mic_path: str, picks: list[dict[str, Any]]
) -> dict:
    """Write one micrograph's picks (replacing whatever was there before --
    the browser holds the full, current set for the micrograph currently
    open, exactly like relion_manualpick's own picker canvas) and update the
    job-level coordinate_files list (manualpick.star) to match.

    `picks`: [{"x": float, "y": float, "class": int}], pixel coordinates in
    the micrograph's own grid (viz.py's voxel-coordinate convention, z
    simply absent -- a micrograph has no depth axis).

    Returns {"coord_path": str, "n_picks": int, "n_micrographs": int} --
    n_micrographs is the new size of the job-level list, for a status line.
    """
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    coord_path = spa_coord_path(job_dir, project_dir, mic_path)
    coord_path.parent.mkdir(parents=True, exist_ok=True)

    if picks:
        df = pd.DataFrame({
            "rlnCoordinateX": [float(p["x"]) for p in picks],
            "rlnCoordinateY": [float(p["y"]) for p in picks],
            # RELION's own picker always writes these two (displayer.cpp) --
            # dummy values for a pick that was never autopicked/refined, so
            # a manually-picked coordinate file has the same columns an
            # autopicked one does and nothing downstream needs to special-
            # case where it came from.
            "rlnAnglePsi": [-999.0] * len(picks),
            "rlnAutopickFigureOfMerit": [0.0] * len(picks),
            # The "colour" class RELION's picker assigns via hotkeys 1-6
            # (rlnParticleSelectionType) -- viz.py's pick state already
            # carries a "class" int (default 1) for the same purpose, so it
            # round-trips here instead of being silently dropped.
            "rlnParticleSelectionType": [int(p.get("class", 1)) for p in picks],
        })
        import starfile
        starfile.write({"": df}, coord_path, overwrite=True)
    else:
        # No picks left for this micrograph (the user deleted them all) --
        # remove the file rather than leave an empty/stale one for Extract
        # to trip over, and drop it from the job-level list below.
        coord_path.unlink(missing_ok=True)

    n_micrographs = _rewrite_spa_job_star(project_dir, job_dir, mic_path, coord_path, picks)
    return {"coord_path": str(coord_path), "n_picks": len(picks), "n_micrographs": n_micrographs}


def _rewrite_spa_job_star(
    project_dir: Path, job_dir: Path, mic_path: str, coord_path: Path, picks: list
) -> int:
    """Upsert this micrograph's row in <job_dir>/manualpick.star (add/update
    if it has picks, remove if it doesn't) and rewrite the file. Small
    enough (one row per micrograph, not per particle) to just read-modify-
    write rather than maintain incrementally."""
    import starfile

    job_star = job_dir / SPA_JOB_STAR_NAME
    rows: dict[str, str] = {}
    if job_star.is_file():
        try:
            blocks = starfile.read(job_star, always_dict=True)
            existing = blocks.get(SPA_JOB_TABLE_NAME)
            if existing is not None and SPA_MIC_NAME_COL in existing.columns:
                rows = dict(zip(existing[SPA_MIC_NAME_COL], existing[SPA_MIC_COORDS_COL]))
        except Exception:  # noqa: BLE001
            rows = {}

    mic_rel = _mic_relpath(project_dir, mic_path)
    coord_rel = str(coord_path.relative_to(project_dir))
    if picks:
        rows[mic_rel] = coord_rel
    else:
        rows.pop(mic_rel, None)

    if rows:
        df = pd.DataFrame({
            SPA_MIC_NAME_COL: list(rows.keys()),
            SPA_MIC_COORDS_COL: list(rows.values()),
        })
        starfile.write({SPA_JOB_TABLE_NAME: df}, job_star, overwrite=True)
    else:
        job_star.unlink(missing_ok=True)
    return len(rows)


def load_spa_picks(project_dir: Path, job_dir: Path, mic_path: str) -> list[dict]:
    """Picks already saved for this micrograph in THIS job (so reopening a
    picking session, or switching back to a micrograph already visited,
    shows what's there instead of starting blank)."""
    import starfile

    coord_path = spa_coord_path(Path(job_dir), project_dir, mic_path)
    if not coord_path.is_file():
        return []
    blocks = starfile.read(coord_path, always_dict=True)
    df = blocks.get("") if "" in blocks else next(iter(blocks.values()), None)
    if df is None or "rlnCoordinateX" not in df.columns:
        return []
    classes = df["rlnParticleSelectionType"] if "rlnParticleSelectionType" in df.columns else None
    return [
        {
            "x": float(row["rlnCoordinateX"]), "y": float(row["rlnCoordinateY"]),
            "class": int(classes.iloc[i]) if classes is not None else 1,
        }
        for i, (_, row) in enumerate(df.iterrows())
    ]


def clear_spa_picks(job_dir: Path) -> int:
    """Delete every pick this job has saved -- the job-level list
    (manualpick.star) and every per-micrograph coordinate file it points
    at. Called at the start of an Overwrite (see custom_jobs.run_manual_
    pick): real RELION's own "Overwrite" job action re-runs into the SAME
    directory (gui_mainwindow.cpp's cb_toggle_overwrite_continue), which
    for a batch job means its outputs get regenerated from scratch anyway;
    for a picking job, which has no batch step to regenerate anything, the
    equivalent is clearing the slate explicitly. A fresh (never-run) job's
    directory has nothing to clear, so this is a safe no-op there -- no
    separate "is this actually an overwrite" flag needed. Returns how many
    files were removed, for the run's own summary line.

    "Continue" (job_runner.JobRunManager.resume_run) is the other, non
    -destructive way back into a picking session -- it does NOT call this.
    """
    job_dir = Path(job_dir)
    removed = 0
    job_star = job_dir / SPA_JOB_STAR_NAME
    if job_star.is_file():
        job_star.unlink()
        removed += 1
    for coord_file in job_dir.glob(f"*{SPA_COORD_FILENAME_SUFFIX}"):
        coord_file.unlink()
        removed += 1
    return removed


# --------------------------------------------------------------------------
# Tomography (3D tomograms)
# --------------------------------------------------------------------------


def tomo_annotation_path(job_dir: Path, tomo_name: str) -> Path:
    safe = _sanitize_relpath(tomo_name)
    return Path(job_dir) / TOMO_ANNOTATIONS_DIRNAME / f"{safe}_particles.star"


def save_tomo_picks(
    project_dir: Path,
    job_dir: Path,
    tomo_name: str,
    picks: list[dict[str, Any]],
    tomograms_star_path: str,
) -> dict:
    """Write one tomogram's picks (voxel coordinates, viz.py's own
    convention -- see load_picks) and rebuild the job-level particles.star +
    optimisation_set.star from every tomogram's annotation file, the same
    way relion_python_tomo_get_particle_poses combines them.

    `picks`: [{"x": float, "y": float, "z": float, "class": int}], voxel
    coordinates in the tomogram MRC the user loaded (whatever binning that
    file is at -- its own voxel_size, read fresh per tomogram below, is
    what makes the Angstrom conversion correct regardless).

    Raises ManualPickError if `tomo_name` isn't in tomograms_star_path (a
    stale/mismatched input) or its MRC can't be read.
    """
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    ann_path = tomo_annotation_path(job_dir, tomo_name)
    ann_path.parent.mkdir(parents=True, exist_ok=True)

    import starfile

    if picks:
        df = pd.DataFrame({
            "rlnTomoName": [tomo_name] * len(picks),
            "rlnCoordinateX": [float(p["x"]) for p in picks],
            "rlnCoordinateY": [float(p["y"]) for p in picks],
            "rlnCoordinateZ": [float(p["z"]) for p in picks],
        })
        starfile.write({"": df}, ann_path, overwrite=True)
    else:
        ann_path.unlink(missing_ok=True)

    n_particles = _rebuild_tomo_job_star(project_dir, job_dir, tomograms_star_path)
    return {"annotation_path": str(ann_path), "n_picks": len(picks), "n_particles": n_particles}


def _rebuild_tomo_job_star(project_dir: Path, job_dir: Path, tomograms_star_path: str) -> int:
    """Recombine every annotations/*_particles.star (voxel coords) into the
    job-level particles.star (centered Angstrom coords, RELION-5's
    tomography convention) + optimisation_set.star, exactly mirroring
    relion_python_tomo_get_particle_poses -- see viz.py's load_picks for the
    inverse (Angstrom -> voxel) of the conversion used here."""
    import starfile

    job_dir = Path(job_dir)
    ann_dir = job_dir / TOMO_ANNOTATIONS_DIRNAME
    tomo_lookup = {
        t["name"]: t["mrc_path"]
        for t in viz._tomograms_from_star(project_dir, viz._safe(project_dir, tomograms_star_path))
    }

    names, xs, ys, zs = [], [], [], []
    if ann_dir.is_dir():
        for ann_file in sorted(ann_dir.glob("*_particles.star")):
            blocks = starfile.read(ann_file, always_dict=True)
            df = blocks.get("") if "" in blocks else next(iter(blocks.values()), None)
            if df is None or df.empty or "rlnTomoName" not in df.columns:
                continue
            tomo_name = str(df["rlnTomoName"].iloc[0])
            mrc_path = tomo_lookup.get(tomo_name)
            if not mrc_path:
                # This tomogram's own MRC isn't in the CURRENT input
                # tomograms.star (e.g. removed upstream since these picks
                # were made) -- can't convert to Angstrom without its voxel
                # size/dims. Skip it from the combined file rather than
                # guess; the raw annotation file is untouched either way.
                continue
            try:
                vinfo = viz.volume_info(project_dir, mrc_path)
            except viz.VizError:
                continue
            vs = vinfo["voxel_size"] or 1.0
            names.extend([tomo_name] * len(df))
            xs.extend((df["rlnCoordinateX"].to_numpy(dtype=float) - vinfo["nx"] / 2.0) * vs)
            ys.extend((df["rlnCoordinateY"].to_numpy(dtype=float) - vinfo["ny"] / 2.0) * vs)
            zs.extend((df["rlnCoordinateZ"].to_numpy(dtype=float) - vinfo["nz"] / 2.0) * vs)

    particles_path = job_dir / TOMO_PARTICLES_STAR_NAME
    optset_path = job_dir / TOMO_OPTSET_STAR_NAME
    if names:
        particles_df = pd.DataFrame({
            "rlnTomoName": names,
            "rlnCenteredCoordinateXAngst": xs,
            "rlnCenteredCoordinateYAngst": ys,
            "rlnCenteredCoordinateZAngst": zs,
        })
        starfile.write({"particles": particles_df}, particles_path, overwrite=True)
        optset_df = pd.DataFrame({
            "rlnTomoParticlesFile": [TOMO_PARTICLES_STAR_NAME],
            "rlnTomoTomogramsFile": [_project_relative(project_dir, tomograms_star_path)],
        })
        starfile.write({"optimisation_set": optset_df}, optset_path, overwrite=True)
    else:
        particles_path.unlink(missing_ok=True)
        optset_path.unlink(missing_ok=True)
    return len(names)


def _project_relative(project_dir: Path, path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        return path
    try:
        return str(p.relative_to(project_dir))
    except ValueError:
        return path


def load_tomo_picks(project_dir: Path, job_dir: Path, tomo_name: str) -> list[dict]:
    """Picks already saved for this tomogram in THIS job -- same purpose as
    load_spa_picks above."""
    import starfile

    ann_path = tomo_annotation_path(Path(job_dir), tomo_name)
    if not ann_path.is_file():
        return []
    blocks = starfile.read(ann_path, always_dict=True)
    df = blocks.get("") if "" in blocks else next(iter(blocks.values()), None)
    if df is None or "rlnCoordinateX" not in df.columns:
        return []
    return [
        {"x": float(row["rlnCoordinateX"]), "y": float(row["rlnCoordinateY"]),
         "z": float(row["rlnCoordinateZ"]), "class": 1}
        for _, row in df.iterrows()
    ]


def clear_tomo_picks(job_dir: Path) -> int:
    """Tomography counterpart of clear_spa_picks above -- every per-tomogram
    annotation file plus the combined particles.star/optimisation_set.star.
    Same "Overwrite means start clean" reasoning; see that function's own
    docstring."""
    job_dir = Path(job_dir)
    removed = 0
    for name in (TOMO_PARTICLES_STAR_NAME, TOMO_OPTSET_STAR_NAME):
        p = job_dir / name
        if p.is_file():
            p.unlink()
            removed += 1
    ann_dir = job_dir / TOMO_ANNOTATIONS_DIRNAME
    if ann_dir.is_dir():
        for ann_file in ann_dir.glob("*_particles.star"):
            ann_file.unlink()
            removed += 1
    return removed
