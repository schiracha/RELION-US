"""
aretomo_bridge.py — read AreTomo2's `.aln` alignment output and convert it to
IMOD-style `.xf` + `.tlt` files, which RELION-5 (and IMOD) consume directly.

Why this shape (don't-reinvent-the-wheel + don't-hallucinate):

AreTomo2 (github.com/czimaginginstitute/AreTomo2) writes a `<name>.aln` file
whose GLOBAL alignment block has 10 whitespace-separated columns, verified
against the teamtomo/alnfile parser and the AreTomo user manual (both fetched
2026-08-14):

    SEC  ROT  GMAG  TX  TY  SMEAN  SFIT  SCALE  BASE  TILT

  * SEC   — 0-based index in the FINAL (post-dark-removal) stack, NOT the
            original acquisition index.
  * ROT   — tilt-axis azimuth in degrees, CCW from the image Y axis.
  * TX,TY — translational shift in PIXELS of the aligned stack (the .aln does
            NOT record a pixel size; you supply it downstream).
  * TILT  — refined per-image tilt angle in degrees.
  * GMAG/SMEAN/SFIT/SCALE/BASE — magnification + fit-quality metrics that
            AreTomo does not document precisely; we read them but do not
            build any geometry on them (only ROT/TX/TY/TILT/SEC are used).

Header lines start with `#`; excluded ("dark") images appear as
`# DarkFrame = <sec0> <sec1> <angle>` lines (original 0-based and 1-based
index + nominal angle). AreTomo3 uses the same global block.

The AreTomo ROT/TX/TY -> IMOD `.xf` (A11 A12 A21 A22 DX DY) mapping is the one
implemented in teamtomo/alnfile's `imod_utils.df_to_xf` and matches AreTomo's
own `-OutImod` export:

    theta = -ROT
    A11 =  cos(theta)   A12 = -sin(theta)
    A21 =  sin(theta)   A22 =  cos(theta)
    DX  =  A11*(-TX) + A12*(-TY)
    DY  =  A21*(-TX) + A22*(-TY)

i.e. a pure rotation by -ROT, with the shift negated and rotated into the
transformed frame. This mapping is verified by working community code and by
AreTomo's own IMOD output, but is NOT published as a formula by the AreTomo
authors — so if exactness matters, validate against a real `-OutImod` `.xf`
from your own data (noted in the AreTomo2 field help). GMAG/SCALE (≈1) are
NOT folded in, matching alnfile.

Note: AreTomo2 can itself emit IMOD files with `-OutImod 1/2/3`. If you still
have that output, prefer it. This bridge is for when you only kept the
`.aln`, or want to batch-convert many of them in one place. The `.xf`/`.tlt`
written here cover the SURVIVING (non-dark) images in SEC order, matching
AreTomo's `-OutImod 2` (dark images removed) convention.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from .imod_bridge import XfRecord, write_tlt, write_xf

PathLike = Union[str, Path]

# Verified global-block column order (teamtomo/alnfile global_alignments.py).
ALN_GLOBAL_COLUMNS = (
    "sec", "rot", "gmag", "tx", "ty", "smean", "sfit", "scale", "base", "tilt",
)


@dataclass
class DarkFrame:
    sec0: int   # 0-based index in the ORIGINAL acquisition order
    sec1: int   # 1-based index in the original acquisition order
    angle: float


@dataclass
class AlnData:
    """Parsed AreTomo2 `.aln`: the global alignment table plus the header
    facts we can actually use (raw size, dark frames)."""
    path: Path
    df: pd.DataFrame                                  # global block, ALN_GLOBAL_COLUMNS
    raw_size: Optional[tuple[int, int, int]] = None   # width, height, n_tilts (pre-dark)
    num_patches: int = 0
    dark_frames: list[DarkFrame] = field(default_factory=list)


def read_aln(path: PathLike) -> AlnData:
    """Parse an AreTomo2 `.aln` file. Reads the global alignment block and the
    header (`# RawSize`, `# NumPatches`, `# DarkFrame` lines). The per-patch
    "# Local Alignment" section, if present, is intentionally not parsed here
    (not needed for a global .xf/.tlt export; its column count also varies by
    AreTomo2 version)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    raw_size = None
    num_patches = 0
    dark_frames: list[DarkFrame] = []
    rows: list[tuple] = []
    in_local = False

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            low = body.lower()
            if low.startswith("local alignment"):
                in_local = True
            elif low.startswith("rawsize"):
                # "RawSize = 4096 4096 41"
                nums = body.split("=", 1)[-1].split()
                if len(nums) >= 3:
                    try:
                        raw_size = (int(float(nums[0])), int(float(nums[1])), int(float(nums[2])))
                    except ValueError:
                        pass
            elif low.startswith("numpatches"):
                try:
                    num_patches = int(float(body.split("=", 1)[-1].strip()))
                except ValueError:
                    pass
            elif low.startswith("darkframe"):
                # "DarkFrame =    3    4    -60.00"
                nums = body.split("=", 1)[-1].split()
                if len(nums) >= 3:
                    try:
                        dark_frames.append(
                            DarkFrame(int(float(nums[0])), int(float(nums[1])), float(nums[2]))
                        )
                    except ValueError:
                        pass
            continue
        if in_local:
            continue  # skip per-patch local rows
        parts = line.split()
        if len(parts) < len(ALN_GLOBAL_COLUMNS):
            continue  # not a global data row
        vals = parts[: len(ALN_GLOBAL_COLUMNS)]
        try:
            rows.append((int(float(vals[0])), *(float(v) for v in vals[1:])))
        except ValueError:
            continue

    if not rows:
        raise ValueError(
            f"{path}: no global alignment rows parsed. Expected data lines with "
            f"{len(ALN_GLOBAL_COLUMNS)} columns ({' '.join(ALN_GLOBAL_COLUMNS)})."
        )

    df = pd.DataFrame(rows, columns=list(ALN_GLOBAL_COLUMNS))
    return AlnData(
        path=path, df=df, raw_size=raw_size, num_patches=num_patches, dark_frames=dark_frames
    )


def _row_to_xf(rot_deg: float, tx: float, ty: float) -> XfRecord:
    """AreTomo (ROT, TX, TY) -> IMOD .xf transform, per teamtomo/alnfile's
    df_to_xf (theta = -ROT; negated shift rotated into the transformed
    frame)."""
    theta = math.radians(-rot_deg)
    c, s = math.cos(theta), math.sin(theta)
    a11, a12 = c, -s
    a21, a22 = s, c
    dx = a11 * (-tx) + a12 * (-ty)
    dy = a21 * (-tx) + a22 * (-ty)
    return XfRecord(a11, a12, a21, a22, dx, dy)


def aln_to_xf_records(aln: Union[PathLike, AlnData]) -> list[XfRecord]:
    """Build one IMOD .xf transform per surviving (non-dark) image, in SEC
    order, from an AreTomo2 `.aln`."""
    data = aln if isinstance(aln, AlnData) else read_aln(aln)
    df = data.df.sort_values("sec")
    return [_row_to_xf(r.rot, r.tx, r.ty) for r in df.itertuples()]


def aln_tilt_angles(aln: Union[PathLike, AlnData]) -> list[float]:
    """Refined per-image tilt angles (TILT column), SEC order — the `.tlt`."""
    data = aln if isinstance(aln, AlnData) else read_aln(aln)
    df = data.df.sort_values("sec")
    return [float(r.tilt) for r in df.itertuples()]


def aln_to_imod(
    aln: Union[PathLike, AlnData],
    xf_path: PathLike,
    tlt_path: Optional[PathLike] = None,
) -> dict:
    """
    Write IMOD-style `.xf` (2D transforms) and `.tlt` (refined tilt angles)
    from an AreTomo2 `.aln`, covering the surviving (non-dark) images in SEC
    order — the format RELION-5's IMOD tilt-series import and IMOD itself
    read. Returns a small summary dict (counts + dark-frame info) for the
    job's live output.

    tlt_path defaults to xf_path with a `.tlt` suffix.
    """
    data = aln if isinstance(aln, AlnData) else read_aln(aln)
    xf_path = Path(xf_path)
    tlt_path = Path(tlt_path) if tlt_path is not None else xf_path.with_suffix(".tlt")

    records = aln_to_xf_records(data)
    angles = aln_tilt_angles(data)
    write_xf(records, xf_path)
    write_tlt(angles, tlt_path)

    return {
        "n_images": len(records),
        "n_dark_excluded": len(data.dark_frames),
        "dark_indices_original": [d.sec0 for d in data.dark_frames],
        "raw_size": data.raw_size,
        "xf_path": str(xf_path),
        "tlt_path": str(tlt_path),
    }
