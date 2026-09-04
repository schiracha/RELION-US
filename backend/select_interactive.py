"""
select_interactive.py — writes RELION-compatible particles.star /
class_averages.star for the browser-based class selector, which replaces
the Select job's interactive branch: real RELION shells out to `` `which
relion_display` `` with `--gui`, a desktop Qt window (see job_catalog.py's
_select_program_override docstring), the same class of problem manual_pick.py
and exclude_tilts.py already solved for Manualpick/TomoExcludeTiltImages.

Data-level shape confirmed by reading real RELION source directly (not
guessed) -- src/pipeline_jobs.cpp ~2938-2995 (getCommandsSelectJob's
interactive branch) and src/displayer.cpp (~1222-1301
makeStarFileSelectedParticles/saveSelectedParticles, ~1510-1580 saveSelected,
~2946-2996 model.star/data.star resolution):

  * fn_model is a _optimiser.star (or, back-compat, a _model.star) from a
    prior Class2D/Class3D run. relion_display resolves the optimiser's own
    optimiser_general.rlnModelStarFile to the real model.star, reads its
    model_classes table, and separately loads the sibling _data.star (same
    filename prefix) for each particle's rlnClassNumber.
  * Each class becomes one thumbnail; selection is a plain binary toggle
    (displayer.h: SELECTED 1 / NOTSELECTED 0), not persisted anywhere by
    real RELION either -- there's no sidecar "selection state" file, only
    the FINAL saved particles.star/class_averages.star. This module matches
    that: nothing is written until the user explicitly saves (see
    run_select_interactive -- unlike exclude_tilts.write_passthrough, there
    is no safe non-destructive default to write at job start here, since an
    empty selection is not usable downstream output the way "keep every
    tilt image" is).
  * On save, every particle in _data.star whose rlnClassNumber matches a
    selected class is copied into particles.star, via ObservationModel::save
    -- which preserves the optics block verbatim and only filters the data
    table. This module does the equivalent: read every block of _data.star,
    filter only the table that actually carries rlnClassNumber, write every
    block back unchanged otherwise.
  * class_averages.star is written ONLY when fn_model's path contains
    "Class2D/" (pipeline_jobs.cpp's own condition, line ~2977) -- a genuine
    RELION quirk (Class3D's interactive selection has no separate "class
    averages" output), preserved here rather than "fixed", to match real
    RELION exactly. It's a PLAIN MDout.write() with no optics block, block
    name "model_classes" (inherited from how it was read) -- this module
    matches that too.

The other two interactive input variants, fn_mic and fn_data (plain
micrographs/particles, no class concept -- see _select_mode below), and
the do_recenter/do_regroup options, are also confirmed from source and
implemented -- see _list_plain_items/save_plain_selection,
_recenter_classes, and _regroup_particles respectively for the exact real
behavior each reproduces.
"""
from __future__ import annotations

import asyncio
import threading
from math import floor, log10
from pathlib import Path

import numpy as np

import progress
import viz

OPTIMISER_SUFFIX = "_optimiser.star"
# Order matters: the half-set names must be checked before the plain
# "_model.star" they also end with. resolve_model_star() always converts an
# _optimiser.star to its _model.star before calling data_star_path(), but
# this list still includes OPTIMISER_SUFFIX (matching real RELION's own
# fn_data resolution, pipeline_jobs.cpp ~2689-2698, which accepts either
# shape directly) so the function stays correct if ever called standalone.
MODEL_STAR_SUFFIXES = ("_half1_model.star", "_half2_model.star", "_model.star", OPTIMISER_SUFFIX)
DATA_STAR_SUFFIX = "_data.star"
CLASS_NUMBER_COL = "rlnClassNumber"
MODEL_CLASSES_BLOCK = "model_classes"
OPTIMISER_GENERAL_BLOCK = "optimiser_general"
MODEL_STAR_FILE_COL = "rlnModelStarFile"

PARTICLES_OUT_NAME = "particles.star"
CLASS_AVERAGES_OUT_NAME = "class_averages.star"
CLASS_AVERAGES_STACK_NAME = "class_averages.mrcs"
MICROGRAPHS_OUT_NAME = "micrographs.star"
REFERENCE_IMAGE_COL = "rlnReferenceImage"

# fn_mic/fn_data both display one row per item with no class indirection --
# real RELION picks the display column from whichever of these is present
# (displayer.cpp's own is_mic/is_data-agnostic column probing). Order
# matters: a micrographs.star could in principle carry both an image name
# and a movie name; rlnMicrographName is the one relion_display shows.
MIC_NAME_COL = "rlnMicrographName"
MOVIE_NAME_COL = "rlnMicrographMovieName"
IMAGE_NAME_COL = "rlnImageName"

# Real RELION's own model_groups columns (displayer.cpp::
# regroupSelectedParticles, ~1307-1401) -- see _regroup_particles.
GROUP_NUMBER_COL = "rlnGroupNumber"
GROUP_NAME_COL = "rlnGroupName"
GROUP_NR_PARTICLES_COL = "rlnGroupNrParticles"
GROUP_SCALE_CORRECTION_COL = "rlnGroupScaleCorrection"
OPTICS_GROUP_COL = "rlnOpticsGroup"
MODEL_GROUPS_BLOCK = "model_groups"


class SelectInteractiveError(Exception):
    """Raised for a bad request (unreadable path, not a Class2D/Class3D
    source, missing data STAR, etc.); the API turns this into a 400."""


# Per-job_dir lock guarding save_selection/clear_selection -- same reasoning
# as exclude_tilts.py's _JOB_LOCKS: a save request and an Overwrite's clear
# could otherwise race on the same two output files from separate threads
# (FastAPI's threadpool for the plain `def` endpoints).
_JOB_LOCKS: dict[str, threading.Lock] = {}
_JOB_LOCKS_GUARD = threading.Lock()


def _job_lock(job_dir: Path) -> threading.Lock:
    key = str(Path(job_dir).resolve())
    with _JOB_LOCKS_GUARD:
        lock = _JOB_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _JOB_LOCKS[key] = lock
        return lock


def resolve_model_star(project_dir: Path, fn_model: str) -> Path:
    """fn_model -> the real model.star to read model_classes from. An
    _optimiser.star is resolved via its own optimiser_general.
    rlnModelStarFile (real relion_display's own indirection, displayer.cpp
    ~2946-2951); a _model.star is used directly (back-compat, same file,
    ~2954-2960)."""
    import starfile

    if not fn_model:
        raise SelectInteractiveError(
            "Select classes from job (fn_model) is required for interactive class selection."
        )
    p = viz._safe(project_dir, fn_model)
    if not p.is_file():
        raise SelectInteractiveError(f"model/optimiser STAR not found: {fn_model}")
    if not p.name.endswith(OPTIMISER_SUFFIX):
        return p
    blocks = starfile.read(p, always_dict=True)
    general = blocks.get(OPTIMISER_GENERAL_BLOCK)
    if general is None:
        raise SelectInteractiveError(f"{p.name}: no {OPTIMISER_GENERAL_BLOCK} block found")
    # optimiser_general is a STAR "list" block (single row of `_key value`
    # pairs, no loop_) -- starfile returns that as a plain dict, not a
    # DataFrame (same shape progress._parse_model_star_cached already
    # documents for model_general).
    row = general if isinstance(general, dict) else (general.iloc[0].to_dict() if len(general) else {})
    model_rel = row.get(MODEL_STAR_FILE_COL)
    if not model_rel:
        raise SelectInteractiveError(f"{p.name}: no {MODEL_STAR_FILE_COL} recorded")
    model_path = viz._safe(project_dir, str(model_rel))
    if not model_path.is_file():
        raise SelectInteractiveError(f"model STAR not found: {model_rel}")
    return model_path


def is_class2d_source(fn_model: str) -> bool:
    """Real RELION's own condition (pipeline_jobs.cpp ~2977) for whether the
    interactive branch offers a class_averages.star output at all -- a
    Class3D source writes particles.star only, matching real behavior."""
    return "Class2D/" in (fn_model or "")


def data_star_path(model_star_path: Path) -> Path:
    """The sibling _data.star for a model/optimiser star (same filename
    prefix -- pipeline_jobs.cpp/displayer.cpp ~2689-2698, ~2836-2848)."""
    name = model_star_path.name
    for suffix in MODEL_STAR_SUFFIXES:
        if name.endswith(suffix):
            prefix = name[: -len(suffix)]
            return model_star_path.with_name(prefix + DATA_STAR_SUFFIX)
    raise SelectInteractiveError(f"{name}: not a recognized _model.star filename")


def _read_star_blocks(path: Path) -> dict:
    import starfile

    return starfile.read(path, always_dict=True)


def _find_particles_block(blocks: dict):
    """The particles table in a _data.star's blocks -- whichever block
    carries rlnClassNumber (real RELION names it "particles", but this
    stays name-agnostic since only the column matters here, the same way
    exclude_tilts.py's block lookups don't hard-code a block name)."""
    for df in blocks.values():
        if hasattr(df, "columns") and CLASS_NUMBER_COL in df.columns:
            return df
    return None


def list_classes(project_dir: Path, fn_model: str) -> list[dict]:
    """Every class in fn_model's model.star, with a per-class particle count
    cross-referenced from the sibling _data.star. Reuses
    progress.classes_for_model_star directly for the class list (index,
    reference image, distribution, resolution, accuracy) -- no new
    model.star parsing needed here."""
    model_path = resolve_model_star(project_dir, fn_model)
    try:
        classes = progress.classes_for_model_star(model_path)
    except progress.ProgressError as exc:
        raise SelectInteractiveError(str(exc)) from exc

    data_path = data_star_path(model_path)
    counts: dict[int, int] = {}
    if data_path.is_file():
        df = _find_particles_block(_read_star_blocks(data_path))
        if df is not None:
            counts = df[CLASS_NUMBER_COL].astype(int).value_counts().to_dict()

    out = []
    for c in classes:
        out.append({**c, "class_number": c["index"], "nr_particles": int(counts.get(c["index"], 0))})
    return out


def _select_mode(field_values: dict) -> tuple:
    """Which of the interactive branch's three input variants applies, and
    its source path -- real RELION checks fn_model, then fn_mic, then
    fn_data, first non-empty wins (pipeline_jobs.cpp ~2938-2986); this
    mirrors that precedence exactly, matching job_catalog.
    select_is_interactive's own field set. Raises if none are set (the
    caller -- run_select_interactive -- is what actually produces the
    user-facing message for that case)."""
    fn_model = (field_values or {}).get("fn_model", "")
    if fn_model:
        return "classes", fn_model
    fn_mic = (field_values or {}).get("fn_mic", "")
    if fn_mic:
        return "micrographs", fn_mic
    fn_data = (field_values or {}).get("fn_data", "")
    if fn_data:
        return "particles", fn_data
    raise SelectInteractiveError(
        "One of 'Select classes from job' / 'Select from micrographs' / "
        "'Select from particles' is required for interactive selection."
    )


def _split_stack_ref(reference: str) -> tuple:
    """"N@path" (a stack slot, e.g. a particle image) -> (path, 0-based
    index); a plain path (e.g. a micrograph, always a single 2D image) ->
    (path, None). Same "N@path" convention progress.py's _STACK_REF_RE
    parses for class references, but these paths are already project-root-
    relative (no job-dir indirection needed, unlike a bare class
    reference -- see thumbnail_source's own docstring), so this stays a
    plain string split rather than reusing that job-dir-aware resolver."""
    if "@" in reference:
        idx_str, path = reference.split("@", 1)
        try:
            return path, int(idx_str) - 1
        except ValueError:
            return reference, None
    return reference, None


def _plain_star_path(project_dir: Path, source: str) -> Path:
    p = viz._safe(project_dir, source)
    if not p.is_file():
        raise SelectInteractiveError(f"STAR file not found: {source}")
    return p


def _find_items_block(blocks: dict, mode: str):
    """The one block in a plain fn_mic/fn_data STAR that actually lists
    items -- whichever carries the column this mode displays by (real
    RELION probes for whichever image-name column is present, same
    reasoning as _find_particles_block above)."""
    name_col = IMAGE_NAME_COL if mode == "particles" else MIC_NAME_COL
    fallback_col = None if mode == "particles" else MOVIE_NAME_COL
    for df in blocks.values():
        if not hasattr(df, "columns"):
            continue
        if name_col in df.columns or (fallback_col and fallback_col in df.columns):
            return df, (name_col if name_col in df.columns else fallback_col)
    return None, None


def list_items(project_dir: Path, field_values: dict) -> dict:
    """Umbrella entry point covering all three interactive input variants.
    Classes keep their existing rich per-class shape (list_classes, under
    "classes"); micrographs/particles are a flat "items" list -- one row
    of the input STAR each, no class join."""
    mode, source = _select_mode(field_values)
    if mode == "classes":
        return {
            "mode": "classes",
            "classes": list_classes(project_dir, source),
            "class_averages_will_be_written": is_class2d_source(source),
        }
    p = _plain_star_path(project_dir, source)
    blocks = _read_star_blocks(p)
    df, name_col = _find_items_block(blocks, mode)
    if df is None:
        raise SelectInteractiveError(
            f"{p.name}: no {IMAGE_NAME_COL if mode == 'particles' else MIC_NAME_COL} column found"
        )
    df = df.reset_index(drop=True)
    items = []
    for i, row in df.iterrows():
        reference = str(row[name_col])
        items.append({
            "row_index": int(i),
            "reference": reference,
            "label": reference.split("@", 1)[-1].rsplit("/", 1)[-1],
        })
    return {"mode": mode, "items": items}


def render_thumbnail(project_dir: Path, field_values: dict, reference: str) -> bytes:
    """One item's thumbnail PNG, dispatched by mode -- classes reuse
    progress.render_class_thumbnail (unchanged); micrographs/particles
    hand their already-project-relative reference straight to
    viz.render_slice_png (a plain 2D micrograph is a 1-slice "volume" per
    viz._as_3d, so axis="z" index=0 renders it directly; a particle's
    "N@stack.mrcs" is split into the stack path + 0-based slot first)."""
    mode, source = _select_mode(field_values)
    if mode == "classes":
        job_dir = thumbnail_source(project_dir, source)
        try:
            return progress.render_class_thumbnail(job_dir, reference)
        except progress.ProgressError as exc:
            raise SelectInteractiveError(str(exc)) from exc
    path_part, index = _split_stack_ref(reference)
    try:
        return viz.render_slice_png(project_dir, path_part, "z", index or 0, max_dim=progress.THUMBNAIL_MAX_PX)
    except viz.VizError as exc:
        raise SelectInteractiveError(str(exc)) from exc


def save_plain_selection(project_dir: Path, job_dir: Path, source: str, mode: str, selected_row_indices: list) -> dict:
    """The fn_mic/fn_data save: keep the selected rows of the ONE input
    STAR, every other block (optics) preserved verbatim, matching real
    RELION's ObservationModel::save behavior the same way save_selection
    does for the classes case -- ALWAYS re-derived from the original input,
    never a previous save, so re-saving with a different selection never
    accumulates."""
    import starfile

    p = _plain_star_path(project_dir, source)
    selected = {int(v) for v in (selected_row_indices or [])}
    job_dir = Path(job_dir)

    with _job_lock(job_dir):
        blocks = _read_star_blocks(p)
        df, _ = _find_items_block(blocks, mode)
        if df is None:
            raise SelectInteractiveError(f"{p.name}: no item column found")
        target_name = next(name for name, block_df in blocks.items() if block_df is df)
        df = df.reset_index(drop=True)
        filtered = df[df.index.isin(selected)].reset_index(drop=True)

        out_blocks = dict(blocks)
        out_blocks[target_name] = filtered

        out_name = PARTICLES_OUT_NAME if mode == "particles" else MICROGRAPHS_OUT_NAME
        job_dir.mkdir(parents=True, exist_ok=True)
        starfile.write(out_blocks, job_dir / out_name, overwrite=True)

    return {"n_items_selected": len(selected), "n_written": int(len(filtered))}


def thumbnail_source(project_dir: Path, fn_model: str) -> Path:
    """The directory progress.render_class_thumbnail should resolve a
    class's rlnReferenceImage relative to -- the ORIGINAL Class2D/Class3D
    job's own directory (model.star's parent), not this Select job's own
    directory (a class average sits directly in the source job's dir with
    no path component, per progress._resolve_reference's own docstring)."""
    return resolve_model_star(project_dir, fn_model).parent


def _recenter_classes(project_dir: Path, job_dir: Path, model_path: Path, sel_df):
    """do_recenter (displayer.cpp::saveSelected, ~1531-1544): translate
    each selected class average to its own center of mass, write the
    recentered images to a NEW class_averages.mrcs stack in this job's own
    directory, and point rlnReferenceImage at the new stack+slot instead
    of the original. Center of mass matches RELION's own
    MultidimArray::centerOfMass exactly: the mass-weighted centroid over
    pixels with a POSITIVE value only, everything else contributing zero
    mass. The sub-pixel wrap-around translation itself uses
    scipy.ndimage.shift(mode="wrap", order=1) as a close, well-tested
    stand-in for RELION's own B-spline-interpolated applyGeometry -- not
    bit-identical, but this app treats image PROCESSING as faithful-in-
    -behavior rather than byte-exact (unlike STAR-file I/O, which stays
    byte-exact throughout this module), the same distinction viz.py and
    progress.py already draw for their own rendering."""
    import mrcfile
    from scipy import ndimage

    job_dir = Path(job_dir)
    recentered = []
    for _, row in sel_df.iterrows():
        ref = str(row[REFERENCE_IMAGE_COL])
        path_part, index = _split_stack_ref(ref)
        p = viz._safe(project_dir, path_part)
        if not p.is_file():
            # A class reference sits directly in the source job's own dir
            # with no path component (progress._resolve_reference's own
            # first fallback) -- try that before giving up.
            p = model_path.parent / Path(path_part).name
        if not p.is_file():
            raise SelectInteractiveError(f"class reference image not found: {ref}")
        with mrcfile.open(p, mode="r", permissive=True) as mrc:
            data = np.asarray(mrc.data, dtype=np.float64)
        img = data[index] if index is not None else data
        mass_img = np.where(img > 0, img, 0.0)
        total_mass = float(mass_img.sum())
        if total_mass > 0:
            com = np.array(ndimage.center_of_mass(mass_img))
            center = np.array(img.shape) / 2.0
            img = ndimage.shift(img, center - com, mode="wrap", order=1)
        recentered.append(img.astype(np.float32))

    stack_path = job_dir / CLASS_AVERAGES_STACK_NAME
    with mrcfile.new(stack_path, overwrite=True) as mrc:
        mrc.set_data(np.stack(recentered, axis=0))

    out_df = sel_df.reset_index(drop=True).copy()
    out_df[REFERENCE_IMAGE_COL] = [f"{i + 1:06d}@{CLASS_AVERAGES_STACK_NAME}" for i in range(len(out_df))]
    return out_df


def _regroup_particles(project_dir: Path, model_path: Path, selected_df, nr_groups: int):
    """do_regroup (displayer.cpp::regroupSelectedParticles, ~1307-1401):
    regroup the SELECTED particles (already filtered by class) into
    roughly nr_groups groups, using the source model.star's own
    model_groups table (rlnGroupNumber/rlnGroupScaleCorrection/
    rlnGroupNrParticles/rlnGroupName), sorted by refined intensity-scale
    correction, bucketed by each group's own optics group so groups from
    different optics groups never merge. Real RELION's own hard minimum
    (average group size >= 10) is preserved verbatim, same message.
    Returns `selected_df` with rlnGroupNumber replaced by a freshly
    assigned rlnGroupName -- matches real RELION's own
    MDdata.deactivateLabel(EMDL_MLMODEL_GROUP_NO) ("no longer valid")."""
    if GROUP_NUMBER_COL not in selected_df.columns:
        raise SelectInteractiveError(
            f"selected particles have no {GROUP_NUMBER_COL} column to regroup by"
        )
    model_blocks = _read_star_blocks(model_path)
    groups_df = model_blocks.get(MODEL_GROUPS_BLOCK)
    if groups_df is None:
        raise SelectInteractiveError(
            f"{model_path.name}: no {MODEL_GROUPS_BLOCK} block found -- "
            f"re-grouping only works for a real refinement's model.star"
        )
    groups_df = groups_df.reset_index(drop=True).copy()
    groups_df[OPTICS_GROUP_COL] = -1
    groups_df[GROUP_NR_PARTICLES_COL] = 0
    group_no_to_row = {int(v): i for i, v in enumerate(groups_df[GROUP_NUMBER_COL])}

    optics_by_group: dict = {}
    counts_by_group: dict = {}
    max_optics_group_id = -1
    has_optics_col = OPTICS_GROUP_COL in selected_df.columns
    for _, prow in selected_df.iterrows():
        group_id = int(prow[GROUP_NUMBER_COL])
        part_optics = int(prow[OPTICS_GROUP_COL]) if has_optics_col else -1
        if group_id not in optics_by_group:
            optics_by_group[group_id] = part_optics
            max_optics_group_id = max(max_optics_group_id, part_optics)
        counts_by_group[group_id] = counts_by_group.get(group_id, 0) + 1

    for group_id, optics_id in optics_by_group.items():
        row_i = group_no_to_row.get(group_id)
        if row_i is not None:
            groups_df.at[row_i, OPTICS_GROUP_COL] = optics_id
    for group_id, count in counts_by_group.items():
        row_i = group_no_to_row.get(group_id)
        if row_i is not None:
            groups_df.at[row_i, GROUP_NR_PARTICLES_COL] = count

    groups_df = groups_df.sort_values(GROUP_SCALE_CORRECTION_COL, kind="stable").reset_index(drop=True)

    n_selected = len(selected_df)
    average_group_size = n_selected // nr_groups
    if average_group_size < 10:
        raise SelectInteractiveError("Each group should have at least 10 particles")

    fill_chars = int(floor(log10(nr_groups))) + 1
    new_group_names: dict = {}
    new_group_id = 0
    for optics_group_id in range(1, max_optics_group_id + 1):
        nr_parts_in_new_group = 0
        new_group_id += 1
        for _, grow in groups_df.iterrows():
            if int(grow[OPTICS_GROUP_COL]) != optics_group_id:
                continue
            group_id = int(grow[GROUP_NUMBER_COL])
            nr_parts_in_new_group += int(grow[GROUP_NR_PARTICLES_COL])
            if nr_parts_in_new_group > average_group_size:
                new_group_id += 1
                nr_parts_in_new_group = 0
            new_group_names[group_id] = f"group_{new_group_id:0{fill_chars}d}"

    out = selected_df.reset_index(drop=True).copy()
    out[GROUP_NAME_COL] = out[GROUP_NUMBER_COL].astype(int).map(new_group_names)
    if out[GROUP_NAME_COL].isna().any():
        raise SelectInteractiveError("Failed in regrouping: a selected particle references an unmapped group")
    return out.drop(columns=[GROUP_NUMBER_COL])


def save_selection(
    project_dir: Path, job_dir: Path, fn_model: str, selected_class_numbers: list,
    *, do_recenter: bool = False, do_regroup: bool = False, nr_groups: int = 0,
) -> dict:
    """Write particles.star (every selected class's particles, every other
    _data.star block preserved verbatim -- matches ObservationModel::save's
    real optics-preserving behavior) and, for a Class2D source only,
    class_averages.star (selected model_classes rows, no optics block --
    matches the real plain MDout.write). ALWAYS re-derives from the
    original _data.star/model.star (never from a previous save), so
    re-running this with a different selection never accumulates -- same
    no-accumulation guarantee as exclude_tilts.save_tilt_series_exclusions.

    do_recenter/do_regroup default to False (this function's original,
    still-tested behavior when called without them) -- see
    _recenter_classes/_regroup_particles for what each does when set."""
    import starfile

    model_path = resolve_model_star(project_dir, fn_model)
    data_path = data_star_path(model_path)
    if not data_path.is_file():
        raise SelectInteractiveError(f"particle data STAR not found: {data_path.name}")

    selected = {int(v) for v in (selected_class_numbers or [])}
    job_dir = Path(job_dir)

    with _job_lock(job_dir):
        data_blocks = _read_star_blocks(data_path)
        particles_block_name = None
        for name, df in data_blocks.items():
            if hasattr(df, "columns") and CLASS_NUMBER_COL in df.columns:
                particles_block_name = name
                break
        if particles_block_name is None:
            raise SelectInteractiveError(f"{data_path.name}: no {CLASS_NUMBER_COL} column found")

        out_blocks = dict(data_blocks)
        filtered = data_blocks[particles_block_name]
        filtered = filtered[filtered[CLASS_NUMBER_COL].astype(int).isin(selected)].reset_index(drop=True)

        if do_regroup and nr_groups > 0:
            filtered = _regroup_particles(project_dir, model_path, filtered, int(nr_groups))

        out_blocks[particles_block_name] = filtered

        job_dir.mkdir(parents=True, exist_ok=True)
        starfile.write(out_blocks, job_dir / PARTICLES_OUT_NAME, overwrite=True)

        class_averages_written = False
        if is_class2d_source(fn_model):
            model_blocks = _read_star_blocks(model_path)
            classes_df = model_blocks.get(MODEL_CLASSES_BLOCK)
            if classes_df is not None:
                rows = classes_df.reset_index(drop=True)
                # RELION's class number is 1-based row position within
                # model_classes (progress.classes_for_model_star's own
                # "index" field uses the same convention).
                mask = [(i + 1) in selected for i in range(len(rows))]
                sel_df = rows[mask].reset_index(drop=True)
                if do_recenter and len(sel_df):
                    sel_df = _recenter_classes(project_dir, job_dir, model_path, sel_df)
                starfile.write(
                    {MODEL_CLASSES_BLOCK: sel_df}, job_dir / CLASS_AVERAGES_OUT_NAME, overwrite=True,
                )
                class_averages_written = True

    return {
        "n_classes_selected": len(selected),
        "n_particles": int(len(filtered)),
        "class_averages_written": class_averages_written,
    }


def save(project_dir: Path, job_dir: Path, field_values: dict, selection: list) -> dict:
    """Mode-agnostic umbrella save, used by the /api/select/{run_id}/save
    endpoint -- dispatches on _select_mode the same way list_items does,
    reading do_recenter/do_regroup/nr_groups from field_values for the
    classes case (real RELION options on this same job, already recorded
    in the run's own field_values the same way fn_model is)."""
    mode, source = _select_mode(field_values)
    if mode == "classes":
        try:
            nr_groups = int(field_values.get("nr_groups") or 0)
        except (TypeError, ValueError):
            nr_groups = 0
        return save_selection(
            project_dir, job_dir, source, selection,
            do_recenter=bool(field_values.get("do_recenter")),
            do_regroup=bool(field_values.get("do_regroup")),
            nr_groups=nr_groups,
        )
    return save_plain_selection(project_dir, job_dir, source, mode, selection)


def clear_selection(job_dir: Path) -> int:
    """Delete this job's own previously-saved output (particles.star,
    class_averages.star + its do_recenter .mrcs stack, or micrographs.star,
    whichever mode was last saved) -- called at the start of an Overwrite,
    mirroring exclude_tilts.clear_exclusions (a fresh session needs a clean
    slate; a never-run job's directory has nothing to clear, so this is a
    safe no-op there too)."""
    job_dir = Path(job_dir)
    removed = 0
    with _job_lock(job_dir):
        for out_name in (
            PARTICLES_OUT_NAME, CLASS_AVERAGES_OUT_NAME, CLASS_AVERAGES_STACK_NAME, MICROGRAPHS_OUT_NAME,
        ):
            p = job_dir / out_name
            if p.is_file():
                p.unlink()
                removed += 1
    return removed


def _run_select_interactive_sync(project_dir: Path, values: dict, job_dir: Path) -> str:
    try:
        mode, source = _select_mode(values)
    except SelectInteractiveError as exc:
        raise ValueError(str(exc)) from exc

    removed = clear_selection(job_dir)
    cleared_note = f"Cleared a previous selection ({removed} file(s)) from an earlier session.\n" if removed else ""

    if mode == "classes":
        data_path = data_star_path(resolve_model_star(project_dir, source))
        if not data_path.is_file():
            raise ValueError(f"Expected particle data STAR not found: {data_path.name}")
        classes = list_classes(project_dir, source)
        class_note = (
            "2D class averages" if is_class2d_source(source)
            else "3D classes (Class3D source -- no separate class_averages.star on save)"
        )
        return (
            f"{cleared_note}"
            f"Found {len(classes)} {class_note} in {source}.\n"
            f"Use the Picker button above to open the class selector and choose "
            f"which classes to keep -- nothing is saved until you save a "
            f"selection there."
        )

    items = list_items(project_dir, values)["items"]
    noun = "particles" if mode == "particles" else "micrographs"
    return (
        f"{cleared_note}"
        f"Found {len(items)} {noun} in {source}.\n"
        f"Use the Picker button above to open the selector and choose which "
        f"{noun} to keep -- nothing is saved until you save a selection there."
    )


async def run_select_interactive(project_dir: Path, values: dict, job_dir: Path) -> str:
    """Validates whichever of fn_model/fn_mic/fn_data is set (real RELION's
    own precedence, _select_mode) resolves to something real -- the actual
    work (picking classes/micrographs/particles) happens afterward through
    the Picker button, exactly like custom_jobs.run_manual_pick/
    run_exclude_tilt_images.

    Overwrite reuses this SAME function (start_custom_job's overwrite_run_id
    path, same as run_manual_pick), which is why clearing a prior selection
    lives HERE -- a fresh run's directory has nothing to clear (clear_
    selection is then a no-op), so this only actually removes anything on a
    genuine Overwrite, matching real RELION's own "Overwrite re-runs into
    the SAME directory" semantics."""
    return await asyncio.to_thread(_run_select_interactive_sync, project_dir, values, job_dir)
