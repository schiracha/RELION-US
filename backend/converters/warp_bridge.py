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
    """
    source_cols = set(source_df.columns)
    ref_cols = set(reference_columns)
    return {
        "matched": sorted(source_cols & ref_cols),
        "missing_from_source": sorted(ref_cols - source_cols),
        "extra_in_source": sorted(source_cols - ref_cols),
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
    """
    mapping = DEFAULT_COLUMN_MAP if column_map is None else column_map
    renamed = df.rename(columns=mapping)

    missing = [c for c in required_columns if c not in renamed.columns]
    if missing:
        raise MissingColumnError(
            f"After applying column_map, still missing {missing}. "
            f"Available columns: {list(renamed.columns)}. "
            f"Update column_map (or DEFAULT_COLUMN_MAP) to point the "
            f"correct Warp/M source column at each missing RELION name."
        )
    return renamed


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
