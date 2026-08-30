"""
submit.py — fill in an sbatch template and submit it.

This is the command-line path for running a RELION-US converter, or any
RELION job, as a proper SLURM batch job. The job popup's own "Submit to
SLURM cluster" option (JobRunManager._run_slurm_job, backend/job_runner.py)
does the same thing for a job launched interactively, sharing this same
template-filling logic (backend/slurm_bridge.py) — this script remains the
right tool for a converter run over a full dataset, or any command you'd
rather submit by hand outside the GUI. Works with any SLURM cluster —
nothing here is site-specific; pass your own --account/--partition (and
--cpus-per-task/--mem/--time if the defaults don't fit) for your cluster.

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

    python3 slurm/submit.py \\
        --template slurm/template_relion_job.sbatch \\
        --account mygroup --partition gpu --job-name motioncorr_job012 \\
        --command 'relion_run_motioncorr --i Import/job001/movies.star --o MotionCorr/job012/ --j 8' \\
        --dry-run

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import slurm_bridge  # noqa: E402 -- after sys.path setup, matching this repo's other flat-module scripts


def fill_template(
    template_path: Path,
    account: str,
    job_name: str,
    *,
    partition: str = "PARTITION_NAME",
    cpus_per_task: int = 8,
    mem: str = "64G",
    time_limit: str = "08:00:00",
    command: str = "",
    out_dir: Path = Path("."),
) -> str:
    """
    Thin wrapper around slurm_bridge.fill_sbatch_template — this CLI and
    JobRunManager's "Submit to SLURM cluster" path share one template-
    filling implementation so they can't drift. Only template_relion_job.
    sbatch has the new placeholders (JOB_NAME/ntasks/cpus_per_task/mem/
    time_limit/gres_line/out_path/err_path/command); template_python_job.
    sbatch was deliberately left as-is (still a fixed --job-name, no
    RELION_COMMAND placeholder — it forwards "$@" to python3 instead), so
    none of those tokens exist in it and substitution is a harmless no-op
    there.

    out_path/err_path use SLURM's own %x/%j patterns (job name / job ID,
    resolved by SLURM itself at runtime) rather than a fully pre-resolved
    path -- fine for this standalone CLI, which (unlike JobRunManager's
    _run_slurm_job) has no live tracking/tailing that needs to know the
    exact filename in advance.
    """
    if not command:
        command = ("echo \"Fill in the actual relion_* command before submitting "
                    "(or pass --command).\"; exit 1")
    out_dir_str = str(Path(out_dir).resolve())
    return slurm_bridge.fill_sbatch_template(
        template_path,
        command=command,
        job_name=job_name,
        account=account,
        partition=partition,
        ntasks=1,
        cpus_per_task=cpus_per_task,
        mem=mem,
        time_limit=time_limit,
        gres_line="#SBATCH --gres=gpu:1",
        out_path=f"{out_dir_str}/%x-%j.out",
        err_path=f"{out_dir_str}/%x-%j.err",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path, help="Path to an .sbatch template")
    parser.add_argument("--account", required=True, help="Your SLURM allocation/account name for this cluster")
    parser.add_argument("--job-name", default="relion_us_job")
    parser.add_argument("--partition", default="PARTITION_NAME", help="SLURM partition/queue name for this cluster")
    parser.add_argument("--cpus-per-task", type=int, default=8)
    parser.add_argument("--mem", default="64G")
    parser.add_argument("--time", default="08:00:00", help="SLURM --time limit, e.g. 08:00:00")
    parser.add_argument("--command", default="", help="The relion_* command to run (template_relion_job.sbatch only)")
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

    args.out_dir.mkdir(parents=True, exist_ok=True)
    filled = fill_template(
        args.template, args.account, args.job_name,
        partition=args.partition, cpus_per_task=args.cpus_per_task,
        mem=args.mem, time_limit=args.time, command=args.command,
        out_dir=args.out_dir,
    )
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
