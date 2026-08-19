"""
star_io.py — thin, RELION-5-tomography-aware wrapper around the `starfile`
package (https://pypi.org/project/starfile/).

Design choice: we do NOT hard-code a rigid schema and reject anything that
doesn't match it. RELION STAR files are self-describing (each loop declares
its own column names), and the exact optional columns present vary by
RELION version and by which pipeline step produced the file. Being schema
-flexible here means this module keeps working across RELION 4.0/4.1/5.0
tomography STAR files without needing to be re-verified against every point
release.

What this module *does* enforce: the small set of columns that are load
-bearing for tomography interoperability (rlnTomoName, rlnCoordinateX/Y/Z,
rlnTomoParticleId) are checked when you ask for them specifically, and you
get a clear error naming the missing column rather than a silent KeyError
three functions later.

Reference for the RELION-5 tomography metadata design: Burt et al. 2024,
FEBS Open Bio 14(11):1788-1804. PMID 39147729, DOI 10.1002/2211-5463.13873.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import pandas as pd
import starfile

PathLike = Union[str, Path]

# Columns that this module treats as load-bearing for specific operations.
# Presence is checked lazily (only when the relevant accessor is used), not
# on every load, since not every STAR file needs every column.
TOMOGRAM_NAME_COL = "rlnTomoName"
COORD_COLS = ("rlnCoordinateX", "rlnCoordinateY", "rlnCoordinateZ")


class MissingColumnError(KeyError):
    """Raised when a STAR block is missing a column this module needs."""


@dataclass
class StarDocument:
    """
    A parsed STAR file. `blocks` maps block name -> DataFrame for multi-block
    files (e.g. RELION-5's optimisation_set.star, tomograms.star). Single
    -block files still populate `blocks` with a single entry so callers have
    one consistent interface.
    """

    path: Path
    blocks: dict[str, pd.DataFrame] = field(default_factory=dict)

    @classmethod
    def read(cls, path: PathLike) -> "StarDocument":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"STAR file not found: {path}")
        # always_dict=True already returns a dict of name -> DataFrame.
        return cls(path=path, blocks=starfile.read(path, always_dict=True))

    def block(self, name: Optional[str] = None) -> pd.DataFrame:
        """Return a specific block, or the only block if the file has one."""
        if name is not None:
            if name not in self.blocks:
                raise KeyError(
                    f"Block '{name}' not found in {self.path}. "
                    f"Available blocks: {sorted(self.blocks)}"
                )
            return self.blocks[name]
        if len(self.blocks) != 1:
            raise ValueError(
                f"{self.path} has {len(self.blocks)} blocks "
                f"({sorted(self.blocks)}); pass a block name explicitly."
            )
        return next(iter(self.blocks.values()))

    def require_columns(self, df: pd.DataFrame, columns: tuple[str, ...]) -> None:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise MissingColumnError(
                f"{self.path}: missing required column(s) {missing}. "
                f"Present columns: {list(df.columns)}"
            )

    def write(self, path: PathLike, overwrite: bool = False) -> Path:
        """Write every block back out, PRESERVING block names.

        Note the single-block case is not special-cased: handing `starfile`
        a bare DataFrame writes an anonymous `data_` header, losing the
        `data_particles`/`data_tomograms` name that RELION-5 looks blocks up
        by. Passing the dict keeps the name in both cases."""
        path = Path(path)
        if path.exists() and not overwrite:
            raise FileExistsError(f"{path} exists; pass overwrite=True to replace it")
        starfile.write(self.blocks, path, overwrite=overwrite)
        return path


def load_particles(star_path: PathLike, block: Optional[str] = None) -> pd.DataFrame:
    """
    Load a RELION-5 tomography particles.star (or equivalent block) and
    validate that it has the columns every downstream converter here relies
    on: rlnTomoName + 3D coordinates. Does not require rlnTomoParticleId
    (older exports may lack it).
    """
    doc = StarDocument.read(star_path)
    df = doc.block(block)
    doc.require_columns(df, (TOMOGRAM_NAME_COL,) + COORD_COLS)
    return df


def load_tomograms(star_path: PathLike, block: Optional[str] = None) -> pd.DataFrame:
    """Load a RELION-5 tomograms.star and validate it names each tomogram."""
    doc = StarDocument.read(star_path)
    df = doc.block(block)
    doc.require_columns(df, (TOMOGRAM_NAME_COL,))
    return df


def write_particles(
    df: pd.DataFrame,
    out_path: PathLike,
    block_name: str = "particles",
    overwrite: bool = False,
) -> Path:
    """
    Write a particles DataFrame back out as a RELION-compatible STAR file.
    Validates the load-bearing columns are present before writing, so a
    malformed converter output fails loudly here instead of failing inside
    RELION with a less clear error.
    """
    missing = [c for c in (TOMOGRAM_NAME_COL,) + COORD_COLS if c not in df.columns]
    if missing:
        raise MissingColumnError(
            f"Refusing to write {out_path}: DataFrame missing required "
            f"column(s) {missing}. Present columns: {list(df.columns)}"
        )
    out_path = Path(out_path)
    starfile.write({block_name: df}, out_path, overwrite=overwrite)
    return out_path


def backup_before_overwrite(path: PathLike) -> Optional[Path]:
    """
    RELION pipelines are sensitive to STAR files being edited out from under
    a running project. Always call this before write_particles(...,
    overwrite=True) on a file that's part of a live RELION project directory.
    """
    path = Path(path)
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + ".bak")
    # Never overwrite an existing backup: a second import run would otherwise
    # copy the FIRST run's generated output over the .bak, destroying the
    # original hand-curated file this function exists to protect. Fall back to
    # .bak2, .bak3, ... so every previous version survives.
    if backup.exists():
        n = 2
        while True:
            candidate = path.with_suffix(f"{path.suffix}.bak{n}")
            if not candidate.exists():
                backup = candidate
                break
            n += 1
    shutil.copy2(path, backup)
    return backup
