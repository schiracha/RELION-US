#!/usr/bin/env python3
"""Verify DRAFT_OVERRIDES's flags (backend/job_catalog.py) against a real RELION install.

Each JobDraftOverride.flags entry is a curated option_key -> real CLI flag,
transcribed by hand from a specific read of RELION's source (see the comment
above DRAFT_OVERRIDES in job_catalog.py for the commit it was read against).
This script re-derives each flag independently, from the RELION actually installed
on THIS machine, and reports any flag that doesn't appear where expected --
the fastest way to catch version drift between the RELION build the map was
written against and the one a user is actually running.

Not a pytest test: it requires a real RELION install (`relion_refine`,
`relion_run_ctffind`, etc. on PATH, or set RELION_BIN_DIR) and, for the one
job whose --help crashes before printing usage (Motioncorr), a RELION source
checkout (set RELION_SRC_DIR; defaults to /home/schiracha/relion/src).

Usage:
    python3 backend/tests/verify_draft_flags_against_relion.py
    RELION_BIN_DIR=/opt/relion/bin RELION_SRC_DIR=/opt/relion/src python3 ...
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from job_catalog import DRAFT_OVERRIDES  # noqa: E402

BIN_DIR = Path(os.environ.get("RELION_BIN_DIR", "/usr/local/relion/bin"))
SRC_DIR = Path(os.environ.get("RELION_SRC_DIR", "/home/schiracha/relion/src"))
CONDA_BIN_DIR = Path(
    os.environ.get(
        "RELION_CONDA_BIN_DIR",
        "/home/schiracha/anaconda3/envs/relion-5.0/bin",
    )
)

# job -> ordered list of (binary_dir, binary_name, extra_args) to try `--help` on.
# Several jobs share one binary (all relion_refine-family jobs; all four
# non-refine tomo callers use their own dedicated binary).
JOB_HELP_TARGETS: dict[str, list[tuple[Path, str, list[str]]]] = {
    "Ctffind": [(BIN_DIR, "relion_run_ctffind", [])],
    "Import": [(BIN_DIR, "relion_import", [])],
    "Inimodel": [(BIN_DIR, "relion_refine", [])],
    "Class3D": [(BIN_DIR, "relion_refine", [])],
    "Autorefine": [(BIN_DIR, "relion_refine", [])],
    "Class2D": [(BIN_DIR, "relion_refine", [])],
    "MultiBody": [(BIN_DIR, "relion_refine", [])],
    "TomoSubtomo": [(BIN_DIR, "relion_tomo_subtomo", [])],
    "TomoCtfRefine": [(BIN_DIR, "relion_tomo_refine_ctf", [])],
    "TomoAlign": [(BIN_DIR, "relion_tomo_align", [])],
    "TomoReconPart": [(BIN_DIR, "relion_tomo_reconstruct_particle", [])],
    "TomoImport": [(CONDA_BIN_DIR, "relion_tomo_import", ["SerialEM"])],
    "TomoExcludeTiltImages": [(CONDA_BIN_DIR, "relion_tomo_exclude_tilt_images", [])],
    # Motioncorr's --help exits before printing usage (it errors out demanding
    # --use_motioncor2 / RELION's own implementation be chosen first); its
    # flags are checked against source instead, via SOURCE_FALLBACK below.
}

# job -> RELION source file (relative to SRC_DIR) to grep for `"--flag"`
# literals, used only for jobs not resolvable via --help.
SOURCE_FALLBACK: dict[str, str] = {
    "Motioncorr": "motioncorr_runner.cpp",
}


def get_help_text(bin_dir: Path, name: str, extra_args: list[str]) -> str | None:
    exe = bin_dir / name
    if not exe.exists():
        return None
    try:
        # COLUMNS=300: RELION's typer/rich-based tomo tools word-wrap --help
        # at terminal width and truncate long flag names mid-word when run
        # without a tty (no ellipsis, so a truncated flag silently looks like
        # a missing one). A wide COLUMNS avoids that; RELION's own IOParser
        # --help (the C++ programs) ignores COLUMNS and is unaffected.
        env = {**os.environ, "COLUMNS": "300"}
        result = subprocess.run(
            [str(exe), *extra_args, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout or "") + (result.stderr or "")


def check_source(rel_path: str, flag: str) -> bool:
    path = SRC_DIR / rel_path
    if not path.exists():
        return False
    text = path.read_text(errors="ignore")
    return f'"{flag}"' in text


def main() -> int:
    ok, missing, skipped = [], [], []

    for job, override in DRAFT_OVERRIDES.items():
        help_text_cache: dict[str, str | None] = {}
        for key, flag_override in override.flags.items():
            flag = flag_override.flag
            found = False
            checked_against = []

            for bin_dir, name, extra_args in JOB_HELP_TARGETS.get(job, []):
                cache_key = f"{bin_dir}/{name}/{' '.join(extra_args)}"
                if cache_key not in help_text_cache:
                    help_text_cache[cache_key] = get_help_text(bin_dir, name, extra_args)
                text = help_text_cache[cache_key]
                checked_against.append(f"{name} --help")
                if text and flag in text:
                    found = True
                    break

            if not found and job in SOURCE_FALLBACK:
                src = SOURCE_FALLBACK[job]
                checked_against.append(f"source:{src}")
                if check_source(src, flag):
                    found = True

            if not checked_against:
                skipped.append((job, key, flag))
            elif found:
                ok.append((job, key, flag))
            else:
                missing.append((job, key, flag, checked_against))

    print(f"OK: {len(ok)} flags confirmed present\n")

    if skipped:
        print(f"SKIPPED ({len(skipped)}) -- no verification target configured:")
        for job, key, flag in skipped:
            print(f"  {job}.{key} -> {flag}")
        print()

    if missing:
        print(f"MISMATCH ({len(missing)}) -- flag NOT found where expected:")
        for job, key, flag, checked in missing:
            print(f"  {job}.{key} -> {flag}  (checked: {', '.join(checked)})")
        print("\nThese need re-verifying against the installed RELION version --")
        print("job_catalog.py's DRAFT_OVERRIDES may be stale for this build.")
        return 1

    print("All mapped flags confirmed against the installed RELION build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
