#!/usr/bin/env python3
"""
stage_isonet_tomograms.py -- lay out the tomograms of a RELION-5 tomograms.star
the way IsoNet2's `prepare_star` wants them.

`isonet.py prepare_star` takes whole FOLDERS (--full / --even / --odd), each
holding nothing but that kind of tomogram, and pairs them up by sorted file
name. RELION keeps full tomograms and both half-maps side by side in one
folder (`<name>.mrc`, `<name>_half1.mrc`, `<name>_half2.mrc`), so pointing
IsoNet at it gives a star file whose rows are a mix of all three. This script
reads the tomograms STAR instead, which says exactly which file is which, and
writes three folders of symlinks, one file per tomogram under the same name
(<rlnTomoName>.mrc), so sorted order pairs them correctly:

    <out>/full/<name>.mrc   <- rlnTomoReconstructedTomogram   (or --full_column)
    <out>/even/<name>.mrc   <- rlnTomoReconstructedTomogramHalf1
    <out>/odd/<name>.mrc    <- rlnTomoReconstructedTomogramHalf2

A folder is only made when its column is in the STAR. Nothing is copied --
except with --average, for a STAR that has only the half-maps: the full
tomograms IsoNet's deconvolution and mask steps need are then made by summing
each pair of halves (what isonet.py's own create_average does, but into
<out>/full/ rather than an `averaged_tomos` folder in the working directory).

Self-contained on purpose (starfile and pandas only, no imports from the rest
of RELION-US): the IsoNet2 Prepare Star job runs it as a plain script, as a
visible step in front of the isonet.py command.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import mrcfile
import numpy as np
import pandas as pd
import starfile

FULL_COLUMN = "rlnTomoReconstructedTomogram"
HALF_COLUMNS = {"even": "rlnTomoReconstructedTomogramHalf1", "odd": "rlnTomoReconstructedTomogramHalf2"}
KINDS = ("full", "even", "odd")


def _tomogram_table(path, full_column=FULL_COLUMN):
    """The table with rlnTomoName and at least one tomogram column."""
    blocks = starfile.read(path, always_dict=True)
    wanted = {full_column, *HALF_COLUMNS.values()}
    for df in blocks.values():
        if isinstance(df, pd.DataFrame) and "rlnTomoName" in df.columns and wanted & set(df.columns):
            return df
    present = {n: list(d.columns) for n, d in blocks.items() if isinstance(d, pd.DataFrame)}
    raise ValueError(f"{path}: no table has rlnTomoName with one of {sorted(wanted)}; tables and columns present: {present}")


def columns_for(path, full_column=FULL_COLUMN) -> dict:
    """{"full": column, "even": column, "odd": column} for what the STAR has.
    Halves count only as a pair: IsoNet needs --even and --odd together."""
    df = _tomogram_table(path, full_column)
    out = {}
    if full_column in df.columns:
        out["full"] = full_column
    if all(c in df.columns for c in HALF_COLUMNS.values()):
        out.update(HALF_COLUMNS)
    if not out:
        raise ValueError(f"{path}: has only one of the two half-map columns; IsoNet needs both (even and odd) or the full tomogram")
    return out


def _resolve(value, star_path, project_dir) -> Path:
    p = Path(str(value))
    candidates = [p] if p.is_absolute() else [Path(project_dir) / p, Path(star_path).parent / p]
    for cand in candidates:
        if cand.exists():
            return cand.resolve()
    raise FileNotFoundError(f"{star_path}: tomogram file not found: {value}")


def _write_average(even_path: Path, odd_path: Path, out_path: Path):
    """even + odd, as float32, with the even map's voxel size."""
    with mrcfile.open(even_path, permissive=True) as a, mrcfile.open(odd_path, permissive=True) as b:
        if a.data.shape != b.data.shape:
            raise ValueError(f"{even_path.name} and {odd_path.name} have different sizes "
                             f"({a.data.shape} vs {b.data.shape}) and cannot be averaged")
        total = a.data.astype(np.float32) + b.data.astype(np.float32)
        voxel = a.voxel_size
    with mrcfile.new(out_path, overwrite=True) as m:
        m.set_data(total)
        m.voxel_size = voxel


def stage(star_path, out_dir, project_dir=None, full_column=FULL_COLUMN, average=False) -> dict:
    """Write <out_dir>/{full,even,odd}/<rlnTomoName>.mrc symlinks. With
    average=True and no full column, <out_dir>/full/ gets the sum of each
    pair of halves as real files. Returns {kind: [paths]} for what was made."""
    project_dir = project_dir or os.getcwd()
    df = _tomogram_table(star_path, full_column)
    cols = columns_for(star_path, full_column)
    names = df["rlnTomoName"].astype(str).tolist()
    if len(set(names)) != len(names):
        raise ValueError(f"{star_path}: rlnTomoName is not unique, so the tomograms cannot be told apart")
    made = {}
    if average and "full" not in cols:
        cols = {"full": None, **cols}
    for kind, column in cols.items():
        folder = Path(out_dir) / kind
        if folder.is_dir():
            for old in folder.iterdir():          # a re-run: only ever remove what we made
                if old.is_symlink() or (column is None and old.suffix == ".mrc"):
                    old.unlink()
        folder.mkdir(parents=True, exist_ok=True)
        made[kind] = []
        for i, name in enumerate(names):
            link = folder / f"{name}.mrc"
            if link.is_symlink() or link.exists():
                raise FileExistsError(f"{link} already exists and is not something this step created")
            if column is None:
                _write_average(*(_resolve(df[HALF_COLUMNS[k]].iloc[i], star_path, project_dir) for k in ("even", "odd")),
                               link)
            else:
                link.symlink_to(_resolve(df[column].iloc[i], star_path, project_dir))
            made[kind].append(str(link))
    return made


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--star", required=True, help="RELION tomograms STAR (rlnTomoName + tomogram columns)")
    ap.add_argument("--out", required=True, help="folder to create full/, even/, odd/ in")
    ap.add_argument("--full_column", default=FULL_COLUMN, help="column holding the full tomograms")
    ap.add_argument("--average", action="store_true",
                    help="no full-tomogram column: make full/ by summing each even+odd pair")
    ap.add_argument("--project_dir", help="base for relative paths (default: current directory)")
    a = ap.parse_args(argv)
    try:
        made = stage(a.star, a.out, a.project_dir, a.full_column, a.average)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    n = len(next(iter(made.values())))
    print(f"Staged {n} tomograms for IsoNet2 from {a.star}:")
    for kind, paths in made.items():
        print(f"  {kind}: {len(paths)} files -> {Path(a.out) / kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
