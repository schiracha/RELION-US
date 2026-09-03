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

Scope: only the fn_model (class-average selection) variant of Select's
interactive branch is implemented. fn_mic/fn_data (interactively
re-browsing plain micrographs/particles with no class concept) is a
separate, much rarer real workflow, tracked as a follow-up rather than
attempted here -- see run_select_interactive's rejection message below.
do_recenter/do_regroup (real RELION options on this same job) are likewise
not applied by this module.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

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


def thumbnail_source(project_dir: Path, fn_model: str) -> Path:
    """The directory progress.render_class_thumbnail should resolve a
    class's rlnReferenceImage relative to -- the ORIGINAL Class2D/Class3D
    job's own directory (model.star's parent), not this Select job's own
    directory (a class average sits directly in the source job's dir with
    no path component, per progress._resolve_reference's own docstring)."""
    return resolve_model_star(project_dir, fn_model).parent


def save_selection(
    project_dir: Path, job_dir: Path, fn_model: str, selected_class_numbers: list,
) -> dict:
    """Write particles.star (every selected class's particles, every other
    _data.star block preserved verbatim -- matches ObservationModel::save's
    real optics-preserving behavior) and, for a Class2D source only,
    class_averages.star (selected model_classes rows, no optics block --
    matches the real plain MDout.write). ALWAYS re-derives from the
    original _data.star/model.star (never from a previous save), so
    re-running this with a different selection never accumulates -- same
    no-accumulation guarantee as exclude_tilts.save_tilt_series_exclusions."""
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
                starfile.write(
                    {MODEL_CLASSES_BLOCK: sel_df}, job_dir / CLASS_AVERAGES_OUT_NAME, overwrite=True,
                )
                class_averages_written = True

    return {
        "n_classes_selected": len(selected),
        "n_particles": int(len(filtered)),
        "class_averages_written": class_averages_written,
    }


def clear_selection(job_dir: Path) -> int:
    """Delete this job's own previously-saved particles.star/
    class_averages.star -- called at the start of an Overwrite, mirroring
    exclude_tilts.clear_exclusions (a fresh session needs a clean slate; a
    never-run job's directory has nothing to clear, so this is a safe
    no-op there too)."""
    job_dir = Path(job_dir)
    removed = 0
    with _job_lock(job_dir):
        for out_name in (PARTICLES_OUT_NAME, CLASS_AVERAGES_OUT_NAME):
            p = job_dir / out_name
            if p.is_file():
                p.unlink()
                removed += 1
    return removed


def _run_select_interactive_sync(project_dir: Path, values: dict, job_dir: Path) -> str:
    fn_model = values.get("fn_model", "")
    if not fn_model:
        if values.get("fn_mic") or values.get("fn_data"):
            raise ValueError(
                "Interactive selection of micrographs/particles (without a "
                "Class2D/Class3D model) isn't supported by RELION-US's "
                "browser-based selector yet -- only class-average selection "
                "('Select classes from job') is. Use the flag-based modes "
                "above (On value / Discard on statistics / Split) for "
                "micrograph/particle subset selection instead, or file a "
                "GitHub issue if you need this specific workflow."
            )
        raise ValueError("Select classes from job (fn_model) is required for interactive class selection.")

    model_path = resolve_model_star(project_dir, fn_model)
    data_path = data_star_path(model_path)
    if not data_path.is_file():
        raise ValueError(
            f"Expected particle data STAR not found next to {model_path.name}: {data_path.name}"
        )
    removed = clear_selection(job_dir)
    cleared_note = f"Cleared a previous selection ({removed} file(s)) from an earlier session.\n" if removed else ""
    classes = list_classes(project_dir, fn_model)
    class_note = (
        "2D class averages" if is_class2d_source(fn_model)
        else "3D classes (Class3D source -- no separate class_averages.star on save)"
    )
    return (
        f"{cleared_note}"
        f"Found {len(classes)} {class_note} in {fn_model}.\n"
        f"Use the Picker button above to open the class selector and choose "
        f"which classes to keep -- nothing is saved until you save a "
        f"selection there."
    )


async def run_select_interactive(project_dir: Path, values: dict, job_dir: Path) -> str:
    """Validates fn_model resolves to a real Class2D/Class3D model.star with
    a readable sibling _data.star -- the real work (picking classes) happens
    afterward through the Picker button, exactly like
    custom_jobs.run_manual_pick/run_exclude_tilt_images.

    Overwrite reuses this SAME function (start_custom_job's overwrite_run_id
    path, same as run_manual_pick), which is why clearing a prior selection
    lives HERE -- a fresh run's directory has nothing to clear (clear_
    selection is then a no-op), so this only actually removes anything on a
    genuine Overwrite, matching real RELION's own "Overwrite re-runs into
    the SAME directory" semantics."""
    return await asyncio.to_thread(_run_select_interactive_sync, project_dir, values, job_dir)
