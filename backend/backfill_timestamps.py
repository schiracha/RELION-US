"""
backfill_timestamps.py — one-time scan to fill in missing started_at/ended_at
on a project's run_history.json entries.

Why this exists: neither RELION's own pipeline files nor a run this app lost
track of (backend crashed/restarted mid-run) are guaranteed to have timing
recorded. See project_manager.estimate_job_timestamps' docstring for what
this infers it from (specific marker files' mtimes, not the job directory's
own) and, importantly, exactly how wrong that estimate can be if the
project's files were copied/migrated after the jobs actually ran. Every
value this fills in is marked `timestamp_estimated: true` in the history
entry so the UI shows it as approximate, never as a recorded fact.

Usage:
    python3 backfill_timestamps.py <project_dir> [--dry-run]

Only touches run_history.json (this app's own tracked runs). RELION-native
jobs merged from default_pipeline.star get the same estimate computed live,
every time, in job_runner.py's _relion_pipeline_entries -- there is no file
to backfill for those, since this app never persists a record of them.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from job_runner import JobRunManager


def _fmt(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "—"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--dry-run", action="store_true",
                         help="Report what would change without writing run_history.json.")
    args = parser.parse_args(argv)

    project_dir = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"Not a directory: {project_dir}", file=sys.stderr)
        return 1

    manager = JobRunManager(project_dir)

    if args.dry_run:
        import project_manager
        history = project_manager.load_history(project_dir)
        # Mirror backfill_missing_timestamps' own selection logic exactly,
        # without writing anything, so --dry-run's report matches what a
        # real run would touch.
        candidates = [
            e for e in history
            if e.get("cwd") and (
                e.get("started_at") is None
                or (e.get("ended_at") is None and e.get("status") not in ("pending", "running"))
            )
        ]
        if not candidates:
            print("Nothing to backfill.")
            return 0
        print(f"Would check {len(candidates)} run(s) missing timing data:")
        for entry in candidates:
            print(f"  {entry.get('job_name') or entry.get('run_id')}: "
                  f"started_at={entry.get('started_at')}, ended_at={entry.get('ended_at')}")
        return 0

    updated = manager.backfill_missing_timestamps()
    if not updated:
        print("Nothing to backfill.")
        return 0

    print(f"Backfilled {len(updated)} run(s):")
    for entry in updated:
        name = entry.get("job_name") or entry.get("run_id")
        print(f"  {name}: started_at={_fmt(entry.get('started_at'))}  "
              f"ended_at={_fmt(entry.get('ended_at'))}  (estimated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
