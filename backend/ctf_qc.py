#
# Part of RELION-US. Copyright (C) 2026 the RELION-US authors.
# Licensed under the GNU General Public License v2 or later; see LICENSE.
#
"""
ctf_qc.py — end-of-job CTF Estimation quality-control charts + power-spectrum
thumbnails, read from RELION's own per-micrograph CTF fit results.

Why end-of-job only, unlike progress.py's live per-iteration polling for
Class2D/Class3D/etc.: CtffindRunner::joinCtffindResults() builds the
per-micrograph MetaDataTable in memory across the WHOLE run and writes it
only ONCE, at the very end (verified against src/ctffind_runner.cpp
~407-501, RELION cloned 2026-08-14) -- unlike Class2D/Class3D's
run_it###_model.star, which appears fresh after every iteration. There is no
incremental per-micrograph star file to poll partway through a run, so this
module has no "not available yet, still running" story beyond a flat
available/not-yet-available check -- once the file exists, the job is done.

What this reads (both branches of the same joinCtffindResults(), src/
ctffind_runner.cpp ~472-495; column names from src/metadata_label.h):

    SPA:  micrographs_ctf.star, block "micrographs"
        rlnMicrographName, rlnCtfImage, rlnDefocusU, rlnDefocusV,
        rlnCtfAstigmatism, rlnDefocusAngle, rlnCtfFigureOfMerit,
        rlnCtfMaxResolution

    Tomo: power_spectra_fits.star, the file's one anonymous block ("")
        rlnCtfImage, rlnDefocusU, rlnDefocusV, rlnCtfAstigmatism,
        rlnDefocusAngle, rlnCtfFigureOfMerit, rlnCtfMaxResolution
        -- rlnMicrographName is explicitly REMOVED here
        (MDpower.deactivateLabel(EMDL_MICROGRAPH_NAME), same source ~487),
        so tilt images are named from rlnCtfImage's own basename instead.

rlnCtfImage's value carries RELION/Xmipp's "filename:format" hint (e.g.
"some_mic.ctf:mrc" -- the real file on disk is "some_mic.ctf", confirmed for
real against an actual project); resolving and rendering it as a thumbnail
reuses progress.py's _resolve_reference/render_class_thumbnail exactly as
they already handle a class average or volume, once that suffix is stripped
(see progress._FORMAT_HINT_RE) -- a single 2D image is exactly the
data.ndim == 2 case render_class_thumbnail already has.

Histograms and the worst-fit-first ranking are computed client-side, from
the full per-micrograph list this returns in one shot -- consistent with how
progress.py hands back every iteration's summary and lets the frontend draw
the resolution chart, rather than pre-baking one specific bucketing/limit
server-side that the UI can't adjust without a round trip.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import progress  # reuses _resolve_reference / render_class_thumbnail / ProgressError

ProgressError = progress.ProgressError  # re-exported: same error type, same HTTP mapping

_SPA_FILENAME = "micrographs_ctf.star"
_TOMO_FILENAME = "power_spectra_fits.star"


def supports_ctf_qc(internal_name: str) -> bool:
    return internal_name == "Ctffind"


def _find_ctf_star(job_dir: Path) -> Optional[tuple[Path, str]]:
    """(path, "spa"|"tomo"), or None if the job hasn't finished writing its
    joint output yet (including "hasn't run at all" and "still running")."""
    spa = job_dir / _SPA_FILENAME
    if spa.is_file():
        return spa, "spa"
    tomo = job_dir / _TOMO_FILENAME
    if tomo.is_file():
        return tomo, "tomo"
    return None


def read_ctf_qc(job_dir: Path) -> dict:
    """Every micrograph/tilt-image's CTF fit numbers, for the frontend to
    histogram, trend-plot, and rank by fit quality. Returns
    {available, count, micrographs: [{name, ctf_image, defocus_u, defocus_v,
    astigmatism, defocus_angle, fom, max_resolution_A}, ...]}. `available`
    is False (not an error) when the job simply hasn't written its joint
    output yet -- the normal state for a job that's still running or hasn't
    started, exactly like progress.read_progress's own convention."""
    import starfile

    found = _find_ctf_star(job_dir)
    if found is None:
        return {"available": False, "count": 0, "micrographs": []}
    path, kind = found

    blocks = starfile.read(path, always_dict=True)
    table = blocks.get("micrographs") if kind == "spa" else blocks.get("")
    if table is None or not len(table):
        return {"available": False, "count": 0, "micrographs": []}

    cols = table.columns

    def col(name):
        return table[name].tolist() if name in cols else [None] * len(table)

    names = col("rlnMicrographName")
    ctf_images = col("rlnCtfImage")
    defocus_u = col("rlnDefocusU")
    defocus_v = col("rlnDefocusV")
    astigmatism = col("rlnCtfAstigmatism")
    defocus_angle = col("rlnDefocusAngle")
    fom = col("rlnCtfFigureOfMerit")
    max_res = col("rlnCtfMaxResolution")

    micrographs = []
    for i in range(len(table)):
        ctf_image = str(ctf_images[i]) if ctf_images[i] is not None else ""
        name = names[i]
        if not isinstance(name, str) or not name:
            # Absent (None, our own col() fallback) or NaN (a real column
            # with a missing value) both land here. Tomo's rlnMicrographName
            # is deliberately removed by RELION itself -- fall back to the
            # CTF image's own basename, still unique per tilt image.
            name = Path(ctf_image.split(":", 1)[0]).stem if ctf_image else f"#{i + 1}"

        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        micrographs.append({
            "name": str(name),
            "ctf_image": ctf_image,
            "defocus_u": num(defocus_u[i]),
            "defocus_v": num(defocus_v[i]),
            "astigmatism": num(astigmatism[i]),
            "defocus_angle": num(defocus_angle[i]),
            "fom": num(fom[i]),
            "max_resolution_A": num(max_res[i]),
        })

    return {"available": True, "count": len(micrographs), "micrographs": micrographs}


def render_ctf_thumbnail(job_dir: Path, reference: str, max_px: int = progress.THUMBNAIL_MAX_PX) -> bytes:
    """One micrograph's power-spectrum-with-fit image as a small PNG.
    Identical machinery to progress.render_class_thumbnail (a CTF image is
    always a single 2D array, the simplest of the shapes that function
    already handles) -- kept as its own name here for readability at the
    call site, not because the logic differs."""
    return progress.render_class_thumbnail(job_dir, reference, max_px=max_px)
