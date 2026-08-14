"""
imod_bridge.py — bridge between IMOD's plain-text alignment files (.xf,
.tlt) and IMOD's binary model files (.mod) on one side, and RELION-5
tomography STAR files on the other.

Scope, deliberately narrow:

1. .xf / .tlt readers/writers are implemented in pure Python here, because
   they are simple, stable, well-documented plain-text formats (IMOD user
   guide: .xf = one line per tilt image, 6 numbers `A11 A12 A21 A22 DX DY`;
   .tlt = one tilt angle in degrees per line, same image order). There's
   nothing to "not reinvent" here — it's a couple of columns of floats.

2. RELION can already import an IMOD-processed tilt series (aligned stack +
   .xf/.tlt/.defocus) directly via `relion_tomo_import_tomograms` — see
   https://relion.readthedocs.io/en/release-4.0/STA_tutorial/ImportTomo.html.
   This module does NOT duplicate that importer. What it fills in is the
   gap on the *other* side: round-tripping particle picks between RELION's
   particles.star and IMOD's binary .mod model files, so you can pick or
   curate particles by eye in 3dmod and bring them into RELION, or push a
   RELION particle set back out to 3dmod for visual QC on top of the
   tomogram. For the binary .mod format itself we shell out to IMOD's own
   `point2model` / `model2point`, per your "don't reinvent the wheel" rule
   — those tools are the correct, version-matched way to touch .mod files.

Everything that shells out checks the binary is on PATH first and raises a
clear RuntimeError naming the missing tool, rather than a bare
FileNotFoundError from subprocess.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import pandas as pd

PathLike = Union[str, Path]


# --------------------------------------------------------------------------
# .xf / .tlt (pure Python — plain text, no IMOD install needed to read/write)
# --------------------------------------------------------------------------


@dataclass
class XfRecord:
    a11: float
    a12: float
    a21: float
    a22: float
    dx: float
    dy: float


def read_xf(path: PathLike) -> list[XfRecord]:
    """Read an IMOD .xf transform file: one line per tilt image."""
    path = Path(path)
    records = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 6:
            raise ValueError(
                f"{path}:{lineno}: expected 6 fields (A11 A12 A21 A22 DX DY), "
                f"got {len(parts)}: {line!r}"
            )
        a11, a12, a21, a22, dx, dy = (float(p) for p in parts)
        records.append(XfRecord(a11, a12, a21, a22, dx, dy))
    return records


def write_xf(records: list[XfRecord], path: PathLike) -> Path:
    path = Path(path)
    lines = [
        f"{r.a11: .7f} {r.a12: .7f} {r.a21: .7f} {r.a22: .7f} {r.dx: .3f} {r.dy: .3f}"
        for r in records
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def read_tlt(path: PathLike) -> list[float]:
    """Read an IMOD .tlt file: one tilt angle in degrees per line."""
    path = Path(path)
    angles = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            angles.append(float(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: not a number: {line!r}") from exc
    return angles


def write_tlt(angles: list[float], path: PathLike) -> Path:
    path = Path(path)
    path.write_text("\n".join(f"{a: .2f}" for a in angles) + "\n")
    return path


# --------------------------------------------------------------------------
# .mod <-> particles.star (via IMOD's own point2model / model2point)
# --------------------------------------------------------------------------


def _require_binary(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(
            f"'{name}' not found on PATH. This function shells out to IMOD's "
            f"own {name} rather than reimplementing the .mod format — load "
            f"the IMOD module (e.g. `module load imod` on Rivanna/Afton) or "
            f"activate your local IMOD install first."
        )
    return resolved


def model_to_coordinates(
    mod_path: PathLike,
    tomo_name: str,
    scale_xyz: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> pd.DataFrame:
    """
    Convert an IMOD .mod point/scattered-point model into a DataFrame with
    RELION-compatible coordinate columns (rlnTomoName, rlnCoordinateX/Y/Z).

    scale_xyz lets you convert IMOD model units (typically unbinned pixels
    at the tomogram's own binning) into whatever pixel size RELION expects
    for this tomogram if the .mod was built on a differently-binned volume.
    Leave as (1, 1, 1) if the .mod and the RELION tomogram share binning.
    """
    model2point = _require_binary("model2point")
    mod_path = Path(mod_path)
    if not mod_path.exists():
        raise FileNotFoundError(mod_path)

    out_txt = mod_path.with_suffix(".point.txt")
    cmd = [model2point, "-input", str(mod_path), "-output", str(out_txt)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"model2point failed (exit {result.returncode}):\n{result.stderr}"
        )

    coords = []
    for line in out_txt.read_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        x, y, z = (float(p) for p in parts[:3])
        coords.append((x, y, z))

    sx, sy, sz = scale_xyz
    df = pd.DataFrame(
        {
            "rlnTomoName": [tomo_name] * len(coords),
            "rlnCoordinateX": [x * sx for x, _, _ in coords],
            "rlnCoordinateY": [y * sy for _, y, _ in coords],
            "rlnCoordinateZ": [z * sz for _, _, z in coords],
        }
    )
    return df


def coordinates_to_model(
    df: pd.DataFrame,
    out_mod_path: PathLike,
    tomo_name: Optional[str] = None,
) -> Path:
    """
    Write a RELION particles DataFrame (or a subset already filtered to one
    tomogram) out as an IMOD .mod scattered-point model, so you can load it
    in 3dmod on top of the tomogram for visual QC.

    If tomo_name is given, the DataFrame is filtered to
    df['rlnTomoName'] == tomo_name first; otherwise the whole DataFrame is
    assumed to already be single-tomogram.
    """
    point2model = _require_binary("point2model")
    required = ("rlnCoordinateX", "rlnCoordinateY", "rlnCoordinateZ")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(f"DataFrame missing required column(s): {missing}")

    if tomo_name is not None:
        if "rlnTomoName" not in df.columns:
            raise KeyError("tomo_name given but DataFrame has no rlnTomoName column")
        df = df[df["rlnTomoName"] == tomo_name]
        if df.empty:
            raise ValueError(f"No rows with rlnTomoName == {tomo_name!r}")

    out_mod_path = Path(out_mod_path)
    tmp_txt = out_mod_path.with_suffix(".point.txt")
    tmp_txt.write_text(
        "\n".join(
            f"{row.rlnCoordinateX:.3f} {row.rlnCoordinateY:.3f} {row.rlnCoordinateZ:.3f}"
            for row in df.itertuples()
        )
        + "\n"
    )

    cmd = [
        point2model,
        "-scat",  # scattered points, not a contour — matches particle picks
        "-input",
        str(tmp_txt),
        "-output",
        str(out_mod_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"point2model failed (exit {result.returncode}):\n{result.stderr}"
        )
    return out_mod_path
