#!/usr/bin/env python3
"""
centered_to_voxels.py -- turn a RELION-5 particles.star in CENTRED ANGSTROM
coordinates (rlnCenteredCoordinateX/Y/ZAngst, what RELION's own pick jobs and
RELION-US's in-browser picker write) into voxel coordinates
(rlnCoordinateX/Y/Z) on a given set of tomograms.

Some tools only take voxel coordinates -- DeepETPicker's `prepare` explicitly
refuses centred Angstrom ones -- so the DeepETPicker Prepare job runs this
first (as a visible `&&` step in its command) when it is handed such a star.

The mapping is the one the viewer and the picker already use (RELION anchors
every tomogram at its centre, N/2):

    voxel = N / 2 + angstrom / voxel_size

with N and voxel_size read from each tomogram's own MRC header, so the result
lands on exactly the volume the next step will read, whatever binning or
thickness it was reconstructed at.

Self-contained on purpose (starfile, pandas, mrcfile only, no imports from the
rest of RELION-US): it is run as a plain script by whichever Python the
backend uses.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import mrcfile
import pandas as pd
import starfile

CENTERED = ("rlnCenteredCoordinateXAngst", "rlnCenteredCoordinateYAngst", "rlnCenteredCoordinateZAngst")
VOXEL = ("rlnCoordinateX", "rlnCoordinateY", "rlnCoordinateZ")
DEFAULT_TOMO_COLUMN = "rlnTomoReconstructedTomogram"


def _table_with(path, columns):
    blocks = starfile.read(path, always_dict=True)
    for df in blocks.values():
        if isinstance(df, pd.DataFrame) and all(c in df.columns for c in columns):
            return df
    present = {n: list(d.columns) for n, d in blocks.items() if isinstance(d, pd.DataFrame)}
    raise ValueError(f"{path}: no table has columns {list(columns)}; tables and columns present: {present}")


def is_centered_angstrom_star(path) -> bool:
    """True for a particles STAR with centred-Angstrom columns and no voxel
    ones -- exactly the case that needs converting. Anything unreadable is
    simply "no" (the caller leaves the command alone)."""
    try:
        blocks = starfile.read(path, always_dict=True)
    except Exception:  # noqa: BLE001
        return False
    frames = [d for d in blocks.values() if isinstance(d, pd.DataFrame)]
    has_centered = any(all(c in d.columns for c in CENTERED) for d in frames)
    has_voxel = any(all(c in d.columns for c in VOXEL) for d in frames)
    return has_centered and not has_voxel


def tomogram_files(tomo_star=None, tomo_column=DEFAULT_TOMO_COLUMN, tomo_dir=None,
                   tomo_ext=".mrc", project_dir=None) -> dict:
    """rlnTomoName -> tomogram path, resolved the way `deepetpicker prepare`
    resolves them: a tomograms STAR column (paths relative to the project
    root, then to the STAR's folder), or a folder of <name><ext> files."""
    root = Path(project_dir) if project_dir else Path.cwd()
    if bool(tomo_star) == bool(tomo_dir):
        raise ValueError("Give exactly one of a tomograms STAR file or a tomograms folder.")
    if tomo_dir:
        return {"__dir__": (root / tomo_dir) if not Path(tomo_dir).is_absolute() else Path(tomo_dir)}
    df = _table_with(tomo_star, ("rlnTomoName", tomo_column))
    out = {}
    for name, f in zip(df["rlnTomoName"].astype(str), df[tomo_column].astype(str)):
        p = Path(f)
        for cand in ([p] if p.is_absolute() else [root / p, Path(tomo_star).parent / p]):
            if cand.exists():
                out[name] = cand
                break
        else:
            raise FileNotFoundError(f"{tomo_star}: tomogram file for {name!r} not found: {f}")
    return out


def _volume_geometry(path):
    with mrcfile.open(path, mode="r", permissive=True, header_only=True) as m:
        nx, ny, nz = int(m.header.nx), int(m.header.ny), int(m.header.nz)
        voxel = float(m.voxel_size.x)
    if voxel <= 0:
        raise ValueError(f"{path}: the MRC header has no voxel size, so Angstroms cannot be converted")
    return nx, ny, nz, voxel


def convert(star_path, out_path, tomo_star=None, tomo_column=DEFAULT_TOMO_COLUMN, tomo_dir=None,
            tomo_ext=".mrc", project_dir=None) -> dict:
    """Write `out_path` (block `particles`: rlnTomoName, rlnCoordinateX/Y/Z and
    rlnClassNumber when the input had it). Returns a small summary."""
    df = _table_with(star_path, ("rlnTomoName",) + CENTERED)
    files = tomogram_files(tomo_star, tomo_column, tomo_dir, tomo_ext, project_dir)
    names = df["rlnTomoName"].astype(str)
    summary = {"picks": len(df), "tomograms": {}}
    out = pd.DataFrame({"rlnTomoName": names.to_numpy()})
    coords = {c: pd.Series(0.0, index=df.index) for c in VOXEL}
    for name in dict.fromkeys(names):
        if "__dir__" in files:
            path = files["__dir__"] / f"{name}{tomo_ext}"
        else:
            path = files.get(name)
        if path is None or not Path(path).exists():
            where = files["__dir__"] if "__dir__" in files else tomo_star
            raise FileNotFoundError(
                f"No tomogram for {name!r} ({'looked for ' + str(path) if path else 'not listed in ' + str(where)}). "
                f"Its picks cannot be placed without the tomogram's size and voxel size.")
        nx, ny, nz, voxel = _volume_geometry(path)
        sel = names == name
        for src, dst, n in zip(CENTERED, VOXEL, (nx, ny, nz)):
            coords[dst][sel] = n / 2.0 + df.loc[sel, src].to_numpy(dtype=float) / voxel
        inb = ((coords["rlnCoordinateX"][sel] >= 0) & (coords["rlnCoordinateX"][sel] < nx)
               & (coords["rlnCoordinateY"][sel] >= 0) & (coords["rlnCoordinateY"][sel] < ny)
               & (coords["rlnCoordinateZ"][sel] >= 0) & (coords["rlnCoordinateZ"][sel] < nz))
        outside = int((~inb).sum())
        summary["tomograms"][name] = {"picks": int(sel.sum()), "outside": outside,
                                      "size": (nx, ny, nz), "voxel_size": voxel, "file": str(path)}
    for c in VOXEL:
        out[c] = coords[c].to_numpy()
    if "rlnClassNumber" in df.columns:
        out["rlnClassNumber"] = df["rlnClassNumber"].to_numpy()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    starfile.write({"particles": out}, out_path, overwrite=True)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--star", required=True, help="particles STAR with rlnCenteredCoordinateX/Y/ZAngst")
    ap.add_argument("--out", required=True, help="voxel-coordinate STAR to write")
    ap.add_argument("--tomo_star", help="tomograms STAR (this OR --tomo_dir)")
    ap.add_argument("--tomo_column", default=DEFAULT_TOMO_COLUMN, help="column of --tomo_star with tomogram file names")
    ap.add_argument("--tomo_dir", help="folder of <name>.mrc tomograms (this OR --tomo_star)")
    ap.add_argument("--tomo_ext", default=".mrc")
    ap.add_argument("--project_dir", help="base for relative paths (default: current directory)")
    a = ap.parse_args(argv)
    try:
        s = convert(a.star, a.out, a.tomo_star, a.tomo_column, a.tomo_dir, a.tomo_ext, a.project_dir)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Converted {s['picks']} picks from centred Angstrom to voxel coordinates -> {a.out}")
    for name, t in s["tomograms"].items():
        note = f", {t['outside']} outside the volume" if t["outside"] else ""
        print(f"  {name}: {t['picks']} picks on {t['size'][0]}x{t['size'][1]}x{t['size'][2]} voxels "
              f"at {t['voxel_size']:g} A/voxel ({t['file']}){note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
