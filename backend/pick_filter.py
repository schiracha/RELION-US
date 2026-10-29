"""
Filter Picks: tune which picks to keep by a numeric column of a particles STAR
file, with a live histogram, a range, per-image counts and a preview of one
image's picks (kept / excluded) -- then write the selection.

The selection itself is RELION's own "Select on value": the output is what

    relion_star_handler --i <particles.star> --o <job>/particles.star \\
        --select <label> --minval <min> --maxval <max>

writes (src/pipeline_jobs.cpp getCommandsSelectJob, do_select_values), and the job
registers in RELION's pipeline as `relion.select.onvalue` with those option values.
This module only helps to CHOOSE label/min/max. When `relion_star_handler` is not
on PATH the same selection is made with the starfile library (same rows kept:
min <= value <= max, inclusive; a missing value is never kept), so the job works on
a machine without RELION; only the file's formatting then differs.

Works for tomography particles (an `rlnTomoName` per pick) and single-particle
picks/particles (`rlnMicrographName`): every function takes the particles table
whatever it holds, finds the image column, and offers every numeric column that
could sensibly exclude a pick.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import viz

STAR_HANDLER = "relion_star_handler"
OUTPUT_NAME = "particles.star"
RELION_DEFAULT_MIN = "-9999."           # RELION's own defaults for select_minval / select_maxval
RELION_DEFAULT_MAX = "9999."
IMAGE_COLUMNS = ("rlnTomoName", "rlnMicrographName")     # tomography first: a tomo STAR has no micrograph column
COORD_COLUMNS = viz.COORD_COLS
CENTERED_COLUMNS = viz.CENTERED_ANGST_COLS

# Columns that rank or qualify a pick, best first. The first one present is the default.
SCORE_COLUMNS = (
    "rlnAutopickFigureOfMerit",       # RELION's pick score; DeepETPicker's score is written here too
    "rlnMaxValueProbDistribution",    # how peaked the orientation posterior is
    "rlnCtfFigureOfMerit",
    "rlnLogLikeliContribution",
    "rlnNrOfSignificantSamples",
    "rlnNormCorrection",
    "rlnCtfMaxResolution",
    "rlnDefocusU", "rlnDefocusV",
    "rlnImageDivisor", "rlnAccuracyRotations", "rlnAccuracyTranslationsAngst",
)
# Bookkeeping numbers that identify rather than describe a pick.
ID_COLUMNS = {
    "rlnIndex", "rlnTomoParticleId", "rlnGroupNumber", "rlnOpticsGroup", "rlnRandomSubset", "rlnHelicalTubeID",
    "rlnTomoVisibleFrames", "rlnParticleId", "rlnImageId",
}
MAX_CACHED_TABLES = 3

_cache: dict = {}


class PickFilterError(ValueError):
    """A problem the user can act on (bad path, no usable column, bad range)."""


# --------------------------------------------------------------------------
# Reading the table
# --------------------------------------------------------------------------


class Table:
    """The particles table of one STAR file, and what is known about it."""

    def __init__(self, path: Path, blocks: dict, name: str, df: pd.DataFrame):
        self.path, self.blocks, self.name, self.df = path, blocks, name, df
        self.image_column: Optional[str] = next((c for c in IMAGE_COLUMNS if c in df.columns), None)
        self._columns: Optional[list[dict]] = None

    @property
    def n_rows(self) -> int:
        return len(self.df)

    def numeric_columns(self) -> list[dict]:
        """Every numeric column that varies and could exclude a pick, as
        {name, group, min, max, mean, std, n_unique, integer}, ordered with the
        score-like ones first."""
        if self._columns is not None:
            return self._columns
        out = []
        for col in self.df.columns:
            s = self.df[col]
            if (col in ID_COLUMNS or col in IMAGE_COLUMNS or not pd.api.types.is_numeric_dtype(s)
                    or pd.api.types.is_bool_dtype(s)):
                continue
            values = s.to_numpy(dtype=float)
            finite = values[np.isfinite(values)]
            if finite.size == 0:
                continue
            n_unique = int(np.unique(finite).size)
            if n_unique < 2:
                continue
            if col in SCORE_COLUMNS:
                group = "Scores and quality"
            elif col in COORD_COLUMNS or col in CENTERED_COLUMNS:
                group = "Position"
            elif col.startswith("rlnAngle") or col.startswith("rlnOrigin"):
                group = "Orientation and offsets"
            else:
                group = "Other"
            out.append({
                "name": col, "group": group, "min": float(finite.min()), "max": float(finite.max()),
                "mean": float(finite.mean()), "std": float(finite.std()), "n_unique": n_unique,
                "integer": bool(np.all(finite == np.rint(finite))),
            })
        rank = {"Scores and quality": 0, "Position": 1, "Orientation and offsets": 2, "Other": 3}
        out.sort(key=lambda c: (rank[c["group"]],
                                SCORE_COLUMNS.index(c["name"]) if c["name"] in SCORE_COLUMNS else 99,
                                list(self.df.columns).index(c["name"])))
        self._columns = out
        return out

    def default_column(self) -> Optional[str]:
        cols = self.numeric_columns()
        return cols[0]["name"] if cols else None

    def values(self, column: str) -> np.ndarray:
        if column not in {c["name"] for c in self.numeric_columns()}:
            raise PickFilterError(
                f"{column!r} is not a numeric column of {self.path.name} that varies "
                f"(choose one of: {', '.join(c['name'] for c in self.numeric_columns()[:12]) or 'none'})")
        return self.df[column].to_numpy(dtype=float)


def _particles_block(blocks: dict) -> tuple[str, pd.DataFrame]:
    """The table that holds the picks: RELION 3.1+ files carry an `optics` table
    beside `particles`; older ones a single unnamed table."""
    frames = {n: d for n, d in blocks.items() if isinstance(d, pd.DataFrame) and len(d.columns)}
    if not frames:
        raise PickFilterError("no table found in this STAR file")
    if "particles" in frames:
        return "particles", frames["particles"]
    for want in (IMAGE_COLUMNS, COORD_COLUMNS, CENTERED_COLUMNS):
        for n, d in frames.items():
            if any(c in d.columns for c in want):
                return n, d
    name = max(frames, key=lambda n: len(frames[n]))
    return name, frames[name]


def load_table(project_dir: Path, star_path: str) -> Table:
    """Read (and cache by path and modification time) the particles table of a STAR file."""
    import starfile

    if not star_path:
        raise PickFilterError("no particles STAR file given")
    p = viz._safe(Path(project_dir), star_path)
    if not p.is_file():
        raise PickFilterError(f"file not found: {star_path}")
    key = (str(p), p.stat().st_mtime_ns)
    if key in _cache:
        return _cache[key]
    try:
        blocks = starfile.read(p, always_dict=True)
    except Exception as exc:  # noqa: BLE001
        raise PickFilterError(f"could not read {p.name} as a STAR file: {exc}") from exc
    name, df = _particles_block(blocks)
    table = Table(p, blocks, name, df.reset_index(drop=True))
    for stale in [k for k in _cache if k[0] == key[0]]:
        _cache.pop(stale)
    while len(_cache) >= MAX_CACHED_TABLES:
        _cache.pop(next(iter(_cache)))
    _cache[key] = table
    return table


# --------------------------------------------------------------------------
# What the tuner shows
# --------------------------------------------------------------------------


def _keep_mask(values: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """RELION's select on value: lo <= value <= hi, a missing value never passes.

    relion_star_handler reads --minval / --maxval with textToFloat, which returns a
    32-bit float even in a double-precision build, while the values are doubles. So
    a pick that sits exactly on a bound written as a decimal that float32 cannot hold
    (a score of 0.9 with maximum 0.9: float32(0.9) is 0.89999998) is left out, where
    one on 0.5 or 0.75 is kept. The bounds are rounded the same way here so the
    counts shown are the counts RELION will write (checked against the real binary)."""
    lo32, hi32 = float(np.float32(lo)), float(np.float32(hi))
    with np.errstate(invalid="ignore"):
        return (values >= lo32) & (values <= hi32)


def summary(table: Table) -> dict:
    cols = table.numeric_columns()
    return {
        "path": str(table.path), "block": table.name, "n_rows": table.n_rows,
        "image_column": table.image_column, "kind": ("tomo" if table.image_column == "rlnTomoName"
                                                     else "spa" if table.image_column else "none"),
        "n_images": int(table.df[table.image_column].nunique()) if table.image_column else 1,
        "columns": cols, "default": table.default_column(),
    }


def histogram(table: Table, column: str, bins: int = 60) -> dict:
    values = table.values(column)
    finite = values[np.isfinite(values)]
    n_unique = int(np.unique(finite).size)
    n_bins = max(2, min(int(bins), n_unique))
    counts, edges = np.histogram(finite, bins=n_bins)
    return {"column": column, "edges": edges.tolist(), "counts": counts.tolist(),
            "n_missing": int(values.size - finite.size), "min": float(finite.min()), "max": float(finite.max())}


def counts(table: Table, column: str, lo: float, hi: float) -> dict:
    """How many picks a range keeps, overall and per image."""
    if lo > hi:
        raise PickFilterError("the minimum is above the maximum")
    keep = _keep_mask(table.values(column), lo, hi)
    out = {"kept": int(keep.sum()), "total": table.n_rows, "images": []}
    if table.image_column:
        frame = pd.DataFrame({"image": table.df[table.image_column].astype(str).to_numpy(), "keep": keep})
        grouped = frame.groupby("image", sort=False)["keep"].agg(["sum", "size"])
        out["images"] = [{"name": n, "kept": int(r["sum"]), "total": int(r["size"])} for n, r in grouped.iterrows()]
    return out


def _image_rows(table: Table, image: str) -> np.ndarray:
    if not table.image_column:
        return np.arange(table.n_rows)
    return np.flatnonzero(table.df[table.image_column].astype(str).to_numpy() == image)


def _volume_for(project_dir: Path, table: Table, image: str, preview_star: str) -> Optional[str]:
    """The file to draw this image on, or None. Micrographs name their own file; a
    tomogram needs the tomograms STAR (rlnTomoName -> volume)."""
    if table.image_column == "rlnMicrographName":
        return image
    if table.image_column == "rlnTomoName" and preview_star:
        info = viz.inspect(Path(project_dir), preview_star)
        for t in info.get("tomograms", []):
            if viz._names_match(t["name"], image):
                return t["mrc_path"]
    return None


def previewable_images(project_dir: Path, table: Table, preview_star: str) -> Optional[list[str]]:
    """Names of the images the tuner can draw (their volume is known), or None for
    "all": micrographs name their own file, so every one counts."""
    if table.image_column != "rlnTomoName":
        return None
    if not preview_star:
        return []
    try:
        listed = [t["name"] for t in viz.inspect(Path(project_dir), preview_star).get("tomograms", [])]
    except viz.VizError:
        return []
    names = table.df[table.image_column].astype(str).unique()
    return [n for n in names if any(viz._names_match(t, n) for t in listed)]


def image_picks(project_dir: Path, table: Table, column: str, image: str, preview_star: str = "") -> dict:
    """One image's picks in the image's own voxels, each with the column's value, plus
    what is needed to draw them (the file and its size)."""
    rows = _image_rows(table, image)
    if not len(rows):
        raise PickFilterError(f"{image!r} has no picks in this file")
    values = table.values(column)[rows]
    df = table.df.iloc[rows]
    path = _volume_for(project_dir, table, image, preview_star)
    volume = None
    if path:
        try:
            volume = viz.volume_info(Path(project_dir), path)
        except viz.VizError:
            path = None
    out = {"image": image, "path": path, "volume": volume, "n": int(len(rows)),
           "message": "", "x": [], "y": [], "z": [], "value": [float(v) if np.isfinite(v) else None for v in values]}
    # Pixel coordinates; the Z column exists for tomography only (a micrograph's picks are 2D).
    if all(c in df.columns for c in COORD_COLUMNS[:2]):
        xs, ys = (df[c].to_numpy(dtype=float) for c in COORD_COLUMNS[:2])
        zs = df[COORD_COLUMNS[2]].to_numpy(dtype=float) if COORD_COLUMNS[2] in df.columns else None
    elif all(c in df.columns for c in CENTERED_COLUMNS[:2]):
        if not volume or not volume.get("voxel_size"):
            out["message"] = "These picks are in centred Angstroms; a tomograms STAR is needed to place them."
            return out
        vs = float(volume["voxel_size"])
        xs = volume["nx"] / 2.0 + df[CENTERED_COLUMNS[0]].to_numpy(dtype=float) / vs
        ys = volume["ny"] / 2.0 + df[CENTERED_COLUMNS[1]].to_numpy(dtype=float) / vs
        zs = (volume["nz"] / 2.0 + df[CENTERED_COLUMNS[2]].to_numpy(dtype=float) / vs
              if CENTERED_COLUMNS[2] in df.columns else None)
    else:
        out["message"] = "This file has no coordinate columns, so the picks cannot be drawn."
        return out
    out["x"], out["y"] = xs.round(2).tolist(), ys.round(2).tolist()
    out["z"] = zs.round(2).tolist() if zs is not None else []
    if path is None:
        out["message"] = ("No image to draw on: " + (
            "add a tomograms STAR to the job to preview tomograms." if table.image_column == "rlnTomoName"
            else "the picks file names no readable micrograph."))
    return out


# --------------------------------------------------------------------------
# Writing the selection
# --------------------------------------------------------------------------


def _number(value: float) -> str:
    return f"{value:.8g}"


def select_command(star_in: Path, out_star: Path, column: str, lo: float, hi: float) -> list[str]:
    """RELION's Select-on-value command, exactly as getCommandsSelectJob builds it."""
    return [STAR_HANDLER, "--i", str(star_in), "--o", str(out_star), "--select", column,
            "--minval", _number(lo), "--maxval", _number(hi)]


def _select_in_python(table: Table, out_star: Path, column: str, lo: float, hi: float) -> int:
    import starfile

    keep = _keep_mask(table.values(column), lo, hi)
    blocks = dict(table.blocks)
    blocks[table.name] = table.df[keep].reset_index(drop=True)
    out_star.parent.mkdir(parents=True, exist_ok=True)
    starfile.write(blocks, out_star, overwrite=True)
    return int(keep.sum())


def apply_filter(project_dir: Path, star_in: str, job_dir: Path, column: str, lo: float, hi: float) -> dict:
    """Write <job_dir>/particles.star: the picks with lo <= column <= hi. Returns
    {kept, total, command, engine}."""
    table = load_table(project_dir, star_in)
    expected = counts(table, column, lo, hi)
    out_star = Path(job_dir) / OUTPUT_NAME
    exe = shutil.which(STAR_HANDLER)
    root = Path(project_dir).resolve()

    def rel(p: Path) -> Path:       # RELION commands name files relative to the project root
        try:
            return Path(p).resolve().relative_to(root)
        except ValueError:
            return Path(p)

    command = select_command(rel(table.path), rel(out_star), column, lo, hi)
    engine = "relion_star_handler"
    if exe:
        out_star.parent.mkdir(parents=True, exist_ok=True)
        out_star.unlink(missing_ok=True)
        proc = subprocess.run([exe] + command[1:], capture_output=True, text=True, timeout=600,
                              cwd=str(project_dir))
        if proc.returncode != 0 or not out_star.is_file():
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise PickFilterError(f"relion_star_handler failed: {detail[-1] if detail else 'no output'}")
        written = re.search(r"with (\d+) item", proc.stdout or "")
        if written:                                  # what RELION actually wrote is the truth
            expected = {**expected, "kept": int(written.group(1))}
    else:
        engine = "python (relion_star_handler is not on PATH)"
        _select_in_python(table, out_star, column, lo, hi)
    _note_command(Path(job_dir), command if exe else None, column, lo, hi, expected["kept"], engine)
    return {"kept": expected["kept"], "total": expected["total"], "command": " ".join(command), "engine": engine}


def _note_command(job_dir: Path, command: Optional[list], column: str, lo: float, hi: float, kept: int, engine: str):
    """note.txt, the way RELION records the commands a job ran."""
    try:
        with open(job_dir / "note.txt", "a") as f:
            f.write(f"\n ++++ Selected on {time.ctime()}\n")
            if command:
                f.write(" ++++ with the following command(s): \n" + " ".join(command) + "\n")
            else:
                f.write(f" ++++ {column} between {_number(lo)} and {_number(hi)} ({engine}), {kept} picks kept\n")
            f.write(" ++++ \n")
    except OSError:
        pass


def record_selection(job_dir: Path, values: Optional[dict], label: str, lo: float, hi: float) -> dict:
    """Make the job remember the selection its output was made with: its recorded
    field values (so a reopened popup / Clone show it) and, when the job is
    registered in RELION's pipeline, the option values in its job.star (as RELION's
    own Select job records them). Returns the three values."""
    import pipeline_bridge

    selection = {"select_label": label, "select_minval": _number(lo), "select_maxval": _number(hi)}
    if values is not None:
        values.update(selection)
    pipeline_bridge.update_job_options(Path(job_dir), selection)
    return selection


def _round_out(value: float, down: bool, digits: int = 6) -> float:
    """`value` rounded away from the data to `digits` significant figures, so the
    bound is a short number that still lies outside every value."""
    if value == 0:
        return 0.0
    exponent = math.floor(math.log10(abs(value)))
    step = 10.0 ** (exponent - digits + 1)
    return (math.floor if down else math.ceil)(value / step) * step


def parse_range(label: str, minval, maxval, table: Table) -> tuple[float, float]:
    """The job's select_minval / select_maxval as numbers, for a session's FIRST
    write. A fresh job must keep every pick (like Exclude Tilt Images' start), but
    RELION's defaults of -9999. and 9999. do not cover every column (a defocus of
    15000), and a blank bound has no value: so a blank bound, or one still at the
    default and not covering the data, becomes just outside the column's extreme.
    A bound the user typed is used exactly as typed."""
    col = {c["name"]: c for c in table.numeric_columns()}[label]
    span = (col["max"] - col["min"]) or 1.0
    defaults = {RELION_DEFAULT_MIN: ("lo", col["min"]), RELION_DEFAULT_MAX: ("hi", col["max"])}

    def one(text, side):
        raw = "" if text is None else str(text).strip()
        if raw:
            try:
                value = float(raw)
            except ValueError as exc:
                raise PickFilterError(f"{text!r} is not a number") from exc
            untouched = raw in defaults
            covers = (float(np.float32(value)) <= col["min"]) if side == "lo" else (float(np.float32(value)) >= col["max"])
            if not untouched or covers:
                return value
        pad = 0.01 * span
        return _round_out(col["min"] - pad, True) if side == "lo" else _round_out(col["max"] + pad, False)

    return one(minval, "lo"), one(maxval, "hi")
