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
from typing import Any

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
# Second block in each per-tomogram annotation file recording the geometry of
# the volume the picks were made on (voxels), so picks stay correct if the
# tomogram is later re-reconstructed at a different size/binning -- see
# _recentre_picks. Absent in annotation files written before this existed.
TOMO_GEOMETRY_BLOCK = "pick_volume"
_GEOM_COLS = ("rlnImageSizeX", "rlnImageSizeY", "rlnImageSizeZ", "rlnImagePixelSize")
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


def _read_annotation(ann_path: Path):
    """(picks DataFrame or None, recorded geometry dict or None)."""
    import starfile

    blocks = starfile.read(ann_path, always_dict=True)
    df = blocks.get("") if "" in blocks else next(
        (b for name, b in blocks.items() if name != TOMO_GEOMETRY_BLOCK), None)
    geom = None
    gdf = blocks.get(TOMO_GEOMETRY_BLOCK)
    if gdf is not None and not gdf.empty and all(c in gdf.columns for c in _GEOM_COLS):
        row = gdf.iloc[0]
        geom = {"nx": float(row["rlnImageSizeX"]), "ny": float(row["rlnImageSizeY"]),
                "nz": float(row["rlnImageSizeZ"]), "voxel_size": float(row["rlnImagePixelSize"])}
    return df, geom


def _volume_for(project_dir: Path, tomo_name: str, tomograms_star_path: str):
    """Header geometry of this tomogram's CURRENT volume, or None if it isn't
    in the star / can't be read (callers fall back to older behaviour)."""
    try:
        lookup = {
            t["name"]: t["mrc_path"]
            for t in viz._tomograms_from_star(project_dir, viz._safe(project_dir, tomograms_star_path))
        }
        mrc_path = lookup.get(tomo_name)
        if not mrc_path:
            return None
        info = viz.volume_info(project_dir, mrc_path)
    except (viz.VizError, OSError, ValueError, KeyError):
        return None
    return {"nx": float(info["nx"]), "ny": float(info["ny"]), "nz": float(info["nz"]),
            "voxel_size": float(info["voxel_size"] or 1.0)}


def _geometry_differs(a: dict, b: dict) -> bool:
    if any(abs(a[k] - b[k]) > 0.5 for k in ("nx", "ny", "nz")):
        return True
    return abs(a["voxel_size"] - b["voxel_size"]) > 1e-3 * max(a["voxel_size"], b["voxel_size"], 1e-9)


def _recentre_picks(xs, ys, zs, src: dict, dst: dict):
    """Voxel coords on volume `src` -> voxel coords on volume `dst`, holding
    the physical centre fixed (RELION anchors every reconstruction at N/2):
    v' = (v - N_src/2) * (voxel_src / voxel_dst) + N_dst/2 per axis. A pure
    shift when only the thickness changed, never a separate squeeze."""
    ratio = src["voxel_size"] / dst["voxel_size"]
    return (
        (xs - src["nx"] / 2.0) * ratio + dst["nx"] / 2.0,
        (ys - src["ny"] / 2.0) * ratio + dst["ny"] / 2.0,
        (zs - src["nz"] / 2.0) * ratio + dst["nz"] / 2.0,
    )


def _count_outside(xs, ys, zs, vol: dict) -> int:
    import numpy as np

    xs, ys, zs = (np.asarray(a, dtype=float) for a in (xs, ys, zs))
    inside = ((xs >= 0) & (xs < vol["nx"]) & (ys >= 0) & (ys < vol["ny"])
              & (zs >= 0) & (zs < vol["nz"]))
    return int((~inside).sum())


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

    n_outside = 0
    if picks:
        df = pd.DataFrame({
            "rlnTomoName": [tomo_name] * len(picks),
            "rlnCoordinateX": [float(p["x"]) for p in picks],
            "rlnCoordinateY": [float(p["y"]) for p in picks],
            "rlnCoordinateZ": [float(p["z"]) for p in picks],
        })
        blocks = {"": df}
        vol = _volume_for(project_dir, tomo_name, tomograms_star_path)
        if vol is not None:
            blocks[TOMO_GEOMETRY_BLOCK] = pd.DataFrame({
                "rlnImageSizeX": [vol["nx"]], "rlnImageSizeY": [vol["ny"]],
                "rlnImageSizeZ": [vol["nz"]], "rlnImagePixelSize": [vol["voxel_size"]],
            })
            n_outside = _count_outside(df["rlnCoordinateX"], df["rlnCoordinateY"], df["rlnCoordinateZ"], vol)
        starfile.write(blocks, ann_path, overwrite=True)
    else:
        ann_path.unlink(missing_ok=True)

    n_particles = _rebuild_tomo_job_star(project_dir, job_dir, tomograms_star_path)
    return {"annotation_path": str(ann_path), "n_picks": len(picks), "n_particles": n_particles,
            "n_outside_volume": n_outside}


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
            df, recorded = _read_annotation(ann_file)
            if df is None or df.empty or "rlnTomoName" not in df.columns:
                continue
            tomo_name = str(df["rlnTomoName"].iloc[0])
            if recorded is not None:
                # Convert against the volume these picks were MADE on, not
                # whatever the tomogram is now: centred Angstrom is
                # independent of the reconstruction size, so this keeps a
                # re-reconstructed tomogram (new thickness/binning) from
                # silently displacing every earlier pick.
                vinfo = recorded
            else:
                mrc_path = tomo_lookup.get(tomo_name)
                if not mrc_path:
                    # Legacy annotation with no recorded geometry, and this
                    # tomogram isn't in the CURRENT input tomograms.star
                    # (e.g. removed upstream since these picks were made) --
                    # can't convert to Angstrom without its voxel size/dims.
                    # Skip it rather than guess; the raw annotation file is
                    # untouched either way.
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
            # Bare filename here (no job_dir prefix) previously left this
            # unresolvable from where every RELION job actually runs (the
            # PROJECT ROOT, not job_dir) -- confirmed for real: TomoSubtomo
            # failed immediately with "MetaDataTable::read: File
            # particles.star does not exist". rlnTomoTomogramsFile already
            # got this right below; this just matches it.
            "rlnTomoParticlesFile": [_project_relative(project_dir, str(particles_path))],
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


def load_tomo_picks_info(project_dir: Path, job_dir: Path, tomo_name: str,
                         tomograms_star_path: str | None = None) -> dict:
    """Picks already saved for this tomogram in THIS job, plus whether they
    had to be moved onto a re-reconstructed volume.

    If the annotation recorded the volume it was picked on and the tomogram
    in `tomograms_star_path` is now a different size/voxel size, the picks
    are re-centred onto the current volume (see _recentre_picks) and a
    `notice` says so. Picks that land outside the new volume are KEPT (so
    re-saving can never silently lose one) and counted in `n_outside`."""
    import numpy as np

    ann_path = tomo_annotation_path(Path(job_dir), tomo_name)
    result = {"picks": [], "rereferenced": False, "n_outside": 0, "notice": ""}
    if not ann_path.is_file():
        return result
    df, recorded = _read_annotation(ann_path)
    if df is None or "rlnCoordinateX" not in df.columns:
        return result
    xs = df["rlnCoordinateX"].to_numpy(dtype=float)
    ys = df["rlnCoordinateY"].to_numpy(dtype=float)
    zs = df["rlnCoordinateZ"].to_numpy(dtype=float)

    current = _volume_for(project_dir, tomo_name, tomograms_star_path) if (recorded and tomograms_star_path) else None
    if recorded and current and _geometry_differs(recorded, current):
        xs, ys, zs = _recentre_picks(xs, ys, zs, recorded, current)
        result["rereferenced"] = True
        result["n_outside"] = _count_outside(xs, ys, zs, current)
        dims = lambda g: "%d x %d x %d" % (round(g["nx"]), round(g["ny"]), round(g["nz"]))
        result["notice"] = (
            f"These picks were made on a {dims(recorded)} voxel volume; the tomogram is now "
            f"{dims(current)}. They were moved onto it (centre held fixed)."
            + (f" {result['n_outside']} now lie outside the volume." if result["n_outside"] else "")
            + " Save to keep the moved positions."
        )
    result["picks"] = [
        {"x": float(x), "y": float(y), "z": float(z), "class": 1}
        for x, y, z in zip(xs, ys, zs)
    ]
    return result


def load_tomo_picks(project_dir: Path, job_dir: Path, tomo_name: str,
                    tomograms_star_path: str | None = None) -> list[dict]:
    """Just the picks from load_tomo_picks_info -- same purpose as
    load_spa_picks above."""
    return load_tomo_picks_info(project_dir, job_dir, tomo_name, tomograms_star_path)["picks"]


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
