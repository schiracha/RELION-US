"""
submit.py — fill in an sbatch template and submit it.

Standalone for now: RELION-US's job popups run their command directly via
subprocess (v1 scope, by explicit choice — see docs/ARCHITECTURE.md's Open
follow-ups). This script is the command-line path for running a RELION-US
converter, or any RELION job, as a proper SLURM batch job in the meantime;
wiring a "Run on cluster" option into the job popups themselves (sharing
this same code path) is the natural next step if you want it. Works with
any SLURM cluster — nothing here is site-specific; adjust the sbatch
templates' partition/account placeholders for your own cluster.

This intentionally does simple string substitution rather than a templating
engine — the sbatch files are short and readable as plain text, and you can
open/edit them by hand at any point without needing to know a templating
syntax.

Usage:
    python3 slurm/submit.py \\
        --template slurm/template_python_job.sbatch \\
        --account mygroup \\
        --job-name deepet_convert \\
        --extra-args "backend/converters/deepetpicker_bridge.py --coords-dir /scratch/.../coords" \\
        --dry-run   # omit --dry-run to actually call sbatch

On a machine without `sbatch` on PATH (e.g. your local laptop, or this
sandbox), --dry-run is forced automatically and the filled-in script is
written out for inspection/manual submission instead.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def fill_template(template_path: Path, account: str, job_name: str) -> str:
    text = template_path.read_text()
    text = text.replace("ACCOUNT_NAME", account)
    text = text.replace("relion_tomo_job", job_name).replace("tomo_bridge_py_job", job_name)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path, help="Path to an .sbatch template")
    parser.add_argument("--account", required=True, help="Your SLURM allocation/account name for this cluster")
    parser.add_argument("--job-name", default="relion_us_job")
    parser.add_argument(
        "--extra-args",
        default="",
        help="Arguments appended after the template's own command (only used by "
        "template_python_job.sbatch, which forwards \"$@\" to python3)",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("."), help="Where to write the filled sbatch script")
    parser.add_argument("--dry-run", action="store_true", help="Write the script but do not call sbatch")
    args = parser.parse_args(argv)

    if not args.template.exists():
        parser.error(f"Template not found: {args.template}")

    filled = fill_template(args.template, args.account, args.job_name)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"{args.job_name}.sbatch"
    out_path.write_text(filled)
    out_path.chmod(0o755)
    print(f"Wrote {out_path}")

    sbatch_bin = shutil.which("sbatch")
    if sbatch_bin is None:
        print("`sbatch` not found on PATH (expected on a laptop or workstation -- "
              "it will be present on a SLURM cluster's login node). "
              "Review the script above, then submit it yourself once it's on the "
              "cluster, or copy it there and run `sbatch` there.")
        return 0

    if args.dry_run:
        print("--dry-run set: not calling sbatch. Review the script above first.")
        return 0

    cmd = [sbatch_bin, str(out_path)]
    if args.extra_args:
        cmd += args.extra_args.split()
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
