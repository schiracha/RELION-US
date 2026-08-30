"""
warp_bridge.py — bridge between Warp/M tilt-series and particle metadata and
RELION-5 tomography STAR files.

Important honesty note (see docs/ARCHITECTURE.md), refined against the
Warp docs (warpem.github.io, verified 2026-08-14) — there are two distinct
Warp→RELION paths and only one of them needs this bridge:

  1. `ts_export_particles` (Warp 2.0 / WarpTools / M) already writes a
     RELION-5 tomography OPTIMISATION SET directly — matching_optimisation_
     set.star + matching.star + matching_tomograms.star, using native
     `rln*`/`rlnTomo*` columns — meant to be opened straight in the
     `relion --tomo` GUI. For that output you do NOT need this bridge.
  2. Warp's own per-tilt-series metadata (`.tomostar`) and older/particle
     STAR exports use Warp's `wrp*` columns (e.g. wrpMovieName, wrpAngleTilt,
     wrpAxisAngle, wrpDose) and genuinely DO need a wrp*→rln* mapping. That
     is what this bridge is for.

So the "Warp and RELION-5 converged on the same rlnTomo* set" claim is true
only for the optimisation-set export, and an oversimplification for
`.tomostar`. Warp's column set has also changed across versions, and I don't
have a verified-current sample from your install to hard-code exact source
column names against — hard-coding a guessed mapping would risk silently
mis-mapping a column, which is worse than making you confirm it once.

Pixel-size caveat: Warp separates reconstruction pixel size (--angpix, used
for the tomogram/picking) from export pixel size (--output_angpix, the
resampled particle box). Coordinates live in the tomogram's (unbinned) pixel
space; get the binning/scale wrong and the STAR parses cleanly but particles
land in the wrong place. Set the scale accordingly when harmonizing.

So this module is built around three genuinely version-independent
operations, plus a *configurable* mapping step:

1. load_warp_star()      — generic STAR loader (Warp's .tomostar and its
                             particle star exports are themselves valid
                             STAR files, so this reuses star_io directly).
2. diff_columns()         — shows you exactly which columns your Warp
                             export has vs. what RELION expects, so you can
                             see in one call whether you even need to remap
                             anything.
3. harmonize_particle_star() — applies a column-name mapping dict you
                             supply (or the DEFAULT_COLUMN_MAP below, which
                             starts empty on purpose) and validates the
                             result has RELION's required columns.
4. remap_tomogram_paths() — practical utility for the very common case
                             where Warp's processing directory and your
                             RELION project directory don't share a root,
                             so tomogram/movie paths need a prefix swap.

Send me one real particles star file (or .tomostar) from your Warp/M
project and I'll fill in DEFAULT_COLUMN_MAP with a verified, version
-specific mapping instead of this placeholder.

Re-verified 2026-08-30 against WarpTools' official docs
(warpem.github.io/warp/reference/warptools/tomogram_particle_files/,
verbatim loop_ block quoted, plus 3 corroborating community threads/gists
citing real Warp/M column headers) — this confirms the two-path framing
above is correct: real Warp/M particle exports use native `rln*` names,
so DEFAULT_COLUMN_MAP staying empty is the right default, not a gap.
Two further, narrower gaps this research did surface (both handled below,
each labeled with its own confidence):

  * The official WarpTools example for the older "RELION 3.0 single STAR
    file" tomography format uses `rlnMicrographName` (holding the
    .tomostar filename) as the tomogram-identity column, where the
    RELION-5 `ts_export_particles` path uses `rlnTomoName` instead — see
    TOMOGRAM_NAME_COL_ALTERNATES below. This is an official-source finding.
  * Warp/RELION >=3.1 exports Angstrom-scale `rlnOriginXAngst`/
    `rlnOriginYAngst`; M (older versions) expects pixel-scale
    `rlnOriginX`/`rlnOriginY` — see angstrom_origin_to_pixel_origin()
    below. This is community-sourced (a protocol gist), not from an
    official Warp/RELION doc — treat it as a helper to opt into, not a
    default behavior.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import pandas as pd

from .star_io import COORD_COLS, StarDocument, TOMOGRAM_NAME_COL, MissingColumnError

PathLike = Union[str, Path]

# Intentionally empty placeholder — see module docstring. Fill in as
# {"warpColumnName": "rlnColumnName", ...} once verified against a real
# export from your Warp/M version.
DEFAULT_COLUMN_MAP: dict[str, str] = {}

RELION_REQUIRED_PARTICLE_COLUMNS = (TOMOGRAM_NAME_COL,) + COORD_COLS

# Alternate names RELION/Warp have used for the tomogram-identity column
# across versions — see the module docstring's WarpTools citation. Only
# rlnMicrographName is confirmed from an official source; kept as its own
# tuple (rather than folded into RELION_REQUIRED_PARTICLE_COLUMNS) so
# diff_columns/harmonize_particle_star can treat it as "satisfies the
# rlnTomoName requirement" without ever reporting BOTH as independently
# required.
TOMOGRAM_NAME_COL_ALTERNATES = (TOMOGRAM_NAME_COL, "rlnMicrographName")


def _resolve_tomogram_name_alternate(
    present_columns,
    required_columns: tuple[str, ...] = RELION_REQUIRED_PARTICLE_COLUMNS,
) -> Optional[str]:
    """
    Shared by diff_columns() and harmonize_particle_star() so they can't
    drift into disagreeing about what counts as an alternate (an earlier
    version had each reimplement this independently with different
    semantics — set-based "any alternate" vs. list-based "first
    alternate" — which would have silently diverged the moment a second
    alternate name was ever added).

    Returns the alternate column name that should satisfy/be renamed to
    TOMOGRAM_NAME_COL, or None if no alternate applies. Only relevant when
    TOMOGRAM_NAME_COL is itself required and currently absent. An alternate
    that is ALSO independently listed in required_columns does not count —
    that means the caller wants both names as genuinely separate columns,
    not "either name satisfies the same requirement", so it must not be
    collapsed/renamed away (the collision this guard specifically prevents:
    without it, a required_columns tuple listing both rlnTomoName and
    rlnMicrographName would have the latter silently renamed into the
    former, then reported as "still missing" even though it was present).
    """
    if TOMOGRAM_NAME_COL not in required_columns or TOMOGRAM_NAME_COL in present_columns:
        return None
    for alt in TOMOGRAM_NAME_COL_ALTERNATES:
        if alt != TOMOGRAM_NAME_COL and alt in present_columns and alt not in required_columns:
            return alt
    return None


def load_warp_star(path: PathLike, block: Optional[str] = None) -> pd.DataFrame:
    """Load a Warp/M .tomostar or particle STAR file (reuses star_io)."""
    doc = StarDocument.read(path)
    return doc.block(block)


def diff_columns(
    source_df: pd.DataFrame,
    reference_columns: tuple[str, ...] = RELION_REQUIRED_PARTICLE_COLUMNS,
) -> dict[str, list[str]]:
    """
    Compare a loaded Warp/M STAR block against the RELION columns this
    pipeline needs. Returns {'matched': [...], 'missing_from_source': [...],
    'extra_in_source': [...]} so you can see at a glance whether a mapping
    step is even necessary.

    If reference_columns requires TOMOGRAM_NAME_COL (rlnTomoName) and the
    source has rlnMicrographName instead, that alternate counts as matched
    rather than being reported as both missing and (confusingly) extra —
    see _resolve_tomogram_name_alternate and the module docstring.
    """
    source_cols = set(source_df.columns)
    ref_cols = set(reference_columns)
    matched = source_cols & ref_cols
    missing = ref_cols - source_cols
    extra = source_cols - ref_cols

    alt = _resolve_tomogram_name_alternate(source_cols, reference_columns)
    if alt is not None:
        missing = missing - {TOMOGRAM_NAME_COL}
        matched = matched | {alt}
        extra = extra - {alt}

    return {
        "matched": sorted(matched),
        "missing_from_source": sorted(missing),
        "extra_in_source": sorted(extra),
    }


def harmonize_particle_star(
    df: pd.DataFrame,
    column_map: Optional[dict[str, str]] = None,
    required_columns: tuple[str, ...] = RELION_REQUIRED_PARTICLE_COLUMNS,
) -> pd.DataFrame:
    """
    Apply column_map (Warp/M name -> RELION name) and validate the result
    has every column in required_columns. Raises MissingColumnError naming
    exactly what's still missing after mapping, rather than failing deep
    inside RELION.

    If column_map is None, uses DEFAULT_COLUMN_MAP (empty until verified —
    see module docstring). Passing an explicit column_map is always safer
    than relying on the default until that default has been confirmed
    against your actual Warp/M output.

    If required_columns needs TOMOGRAM_NAME_COL (rlnTomoName) and it's
    still absent after column_map but rlnMicrographName is present, that
    alternate is renamed to rlnTomoName too (see
    _resolve_tomogram_name_alternate and the module docstring's WarpTools
    citation) — this is an explicit, documented, version-aware rename for
    a confirmed real naming convention, not a guessed mapping like
    DEFAULT_COLUMN_MAP's placeholder would be. Leaving it unrenamed would
    just move the same failure downstream into write_particles(), which
    hard-requires rlnTomoName by name.
    """
    mapping = DEFAULT_COLUMN_MAP if column_map is None else column_map
    renamed = df.rename(columns=mapping)

    alt = _resolve_tomogram_name_alternate(set(renamed.columns), required_columns)
    if alt is not None:
        renamed = renamed.rename(columns={alt: TOMOGRAM_NAME_COL})

    missing = [c for c in required_columns if c not in renamed.columns]
    if missing:
        raise MissingColumnError(
            f"After applying column_map, still missing {missing}. "
            f"Available columns: {list(renamed.columns)}. "
            f"Update column_map (or DEFAULT_COLUMN_MAP) to point the "
            f"correct Warp/M source column at each missing RELION name."
        )
    return renamed


def angstrom_origin_to_pixel_origin(
    df: pd.DataFrame,
    pixel_size_angst: float,
    columns: tuple[str, str] = ("rlnOriginXAngst", "rlnOriginYAngst"),
    out_columns: tuple[str, str] = ("rlnOriginX", "rlnOriginY"),
) -> pd.DataFrame:
    """
    Convert Warp/RELION >=3.1-style Angstrom-scale particle origin columns
    (rlnOriginXAngst/rlnOriginYAngst) to the pixel-scale rlnOriginX/
    rlnOriginY columns older M versions expect: pixel = angstrom /
    pixel_size_angst.

    COMMUNITY-SOURCED, not from an official Warp/RELION doc (see the
    module docstring) — this is a real, reported friction point in the
    Warp->RELION->M pipeline, but treat it as an opt-in helper to call
    only if you hit this specific mismatch, not something applied by
    default anywhere else in this module.

    Only touches `columns` that are actually present; raises KeyError if
    NEITHER is present (nothing to convert), same style as
    remap_tomogram_paths. Does NOT raise if only one of the two is present
    (matching remap_tomogram_paths' "only touch what's present" philosophy)
    — but that leaves the output with only a partial pixel-scale pair, so
    check the returned columns if you need both.
    """
    if pixel_size_angst <= 0:
        raise ValueError(f"pixel_size_angst must be > 0, got {pixel_size_angst!r}")
    present = [c for c in columns if c in df.columns]
    if not present:
        raise KeyError(f"None of {columns} present; available: {list(df.columns)}")
    out = df.copy()  # single copy — mutate in place from here, not per-column
    for src, dst in zip(columns, out_columns):
        if src in out.columns:
            out[dst] = out[src] / pixel_size_angst
            del out[src]
    return out


def remap_tomogram_paths(
    df: pd.DataFrame,
    old_prefix: str,
    new_prefix: str,
    column: str = TOMOGRAM_NAME_COL,
) -> pd.DataFrame:
    """
    Swap a path prefix in `column` (e.g. Warp's absolute processing path ->
    the path RELION's project directory expects). Only touches values that
    actually start with old_prefix; others pass through unchanged so you
    can safely run this even if only some rows need remapping.
    """
    if column not in df.columns:
        raise KeyError(f"Column {column!r} not present; available: {list(df.columns)}")
    out = df.copy()
    mask = out[column].astype(str).str.startswith(old_prefix)
    out.loc[mask, column] = out.loc[mask, column].astype(str).str.replace(
        old_prefix, new_prefix, n=1, regex=False
    )
    return out
