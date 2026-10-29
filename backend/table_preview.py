"""
Outputs-tab previews of plain-text tables that are not STAR files: DeepETPicker's
`.coords` (whitespace separated, no header) and `.csv` (e.g. its metrics.csv).

Both come back in the shape the STAR preview already renders -- one "loop"
block with columns and rows -- so the panel needs no new view. Readers are
lenient on purpose: this shows what is in the file, it does not validate it
(the strict .coords reader used by the import bridge refuses comments and the
5-column variant; a preview should simply display them).

Only the first `max_rows` rows are read into memory; the total is counted by
streaming the file, and left out (None) for a file too large to count quickly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

TABLE_SUFFIXES = (".coords", ".csv")
COUNT_LIMIT_BYTES = 200_000_000         # beyond this the row total is not counted

# DeepETPicker writes `class_id x y z` (or a bare `x y z`); its Coords_withArea
# variant adds a trailing area column.
_COORD_COLUMNS = {
    3: ("x", "y", "z"),
    4: ("class_id", "x", "y", "z"),
    5: ("class_id", "x", "y", "z", "area"),
}


def _number(token: str):
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        return token


def _count_lines(path: Path, skip_header: bool) -> Optional[int]:
    if path.stat().st_size > COUNT_LIMIT_BYTES:
        return None
    with open(path, "rb") as f:
        n = sum(1 for line in f if line.strip())
    return max(0, n - 1) if skip_header else n


def preview_coords(path: Path, max_rows: int) -> list[dict]:
    rows: list[list] = []
    total = 0
    with open(path, "r", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            total += 1
            if len(rows) < max_rows:
                rows.append(s.split())
    if not rows:
        return []
    width = max(len(r) for r in rows)
    columns = list(_COORD_COLUMNS.get(width) or [f"col{i + 1}" for i in range(width)])
    body = [[_number(t) for t in r] + [None] * (width - len(r)) for r in rows]
    return [{"name": path.name, "kind": "loop", "columns": columns, "rows": body,
             "total_rows": total, "truncated": total > len(body)}]


def _delimiter(header: str) -> str:
    counts = {d: header.count(d) for d in (",", "\t", ";")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _headerless(header: str, delimiter: str) -> bool:
    """A first line with a bare number in it is data, not column names: nobody
    names a column "0" or "3.5" (pandas would swallow the first row as names)."""
    fields = [f.strip().strip('"') for f in header.rstrip("\r\n").split(delimiter)]
    return any(isinstance(_number(f), (int, float)) for f in fields if f)


def preview_csv(path: Path, max_rows: int) -> list[dict]:
    import json

    import pandas as pd

    with open(path, "r", errors="replace") as f:
        header = f.readline()
    if not header.strip():
        return []
    delimiter = _delimiter(header)
    headerless = _headerless(header, delimiter)
    df = pd.read_csv(path, sep=delimiter, nrows=max_rows, encoding_errors="replace",
                     header=None if headerless else "infer")
    total = _count_lines(path, skip_header=not headerless)
    if not len(df.columns):
        return []
    columns = [f"col{i + 1}" for i in range(len(df.columns))] if headerless else [str(c) for c in df.columns]
    # pandas' own to_json/loads turns NaN into null and numpy scalars into plain types
    split = json.loads(df.to_json(orient="split", date_format="iso"))
    shown = len(split["data"])
    return [{"name": path.name, "kind": "loop", "columns": columns,
             "rows": split["data"], "total_rows": total,
             "truncated": (total is None) or total > shown}]


def preview_table(path: Path, max_rows: int) -> list[dict]:
    """Blocks for a .coords or .csv file ([] when it is empty)."""
    suffix = path.suffix.lower()
    if suffix == ".coords":
        return preview_coords(path, max_rows)
    if suffix == ".csv":
        return preview_csv(path, max_rows)
    raise ValueError(f"not a table file: {path.name}")
