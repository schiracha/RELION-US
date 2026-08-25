#!/usr/bin/env python3
"""
A stand-in for RELION's `relion_pipeliner`, for tests on a machine with no
RELION install.

It is a TEST DOUBLE, not a reimplementation: it mimics the two subcommands
RELION-US drives, closely enough to check the integration around them —

  --addJobFromStar <job.star>   reads the job type from the star, allocates the
                               next job number from rlnPipeLineJobCounter,
                               creates <JobDir>/jobNNN/, copies the job.star in,
                               appends a Scheduled process, bumps the counter
  --check_job_completion        flips Running/Scheduled processes to Succeeded /
                               Failed / Aborted according to the
                               RELION_JOB_EXIT_* file in their directory

The real binary does far more (it computes the node graph by running the job's
own command builder, and takes the .relion_lock mutex). Anything that depends on
*those* behaviours cannot be tested here and has to be tried against a real
RELION — which is exactly why RELION-US calls the real binary rather than
reimplementing this.
"""
import re
import shutil
import sys
from pathlib import Path

# Same table as src/pipeline_jobs.h's proc_type2dirname, for the job types the
# tests use. The real pipeliner knows all of them.
DIRNAME_BY_LABEL = {
    "relion.import": "Import",
    "relion.motioncorr": "MotionCorr",
    "relion.ctffind": "CtfFind",
    "relion.class2d": "Class2D",
    "relion.class3d": "Class3D",
    "relion.refine3d": "Refine3D",
    "relion.maskcreate": "MaskCreate",
    "relion.autopick": "AutoPick",
    "relion.extract": "Extract",
    "relion.select": "Select",
    "relion.manualpick": "ManualPick",
    "relion.picktomo": "Picks",
}

EXIT_FILES = {
    "RELION_JOB_EXIT_SUCCESS": "Succeeded",
    "RELION_JOB_EXIT_FAILURE": "Failed",
    "RELION_JOB_EXIT_ABORTED": "Aborted",
}

PIPELINE = Path("default_pipeline.star")


def read_pipeline():
    counter = 1
    processes = []
    if not PIPELINE.exists():
        return counter, processes
    text = PIPELINE.read_text()
    m = re.search(r"_rlnPipeLineJobCounter\s+(\d+)", text)
    if m:
        counter = int(m.group(1))
    block = text.split("data_pipeline_processes", 1)
    if len(block) == 2:
        for line in block[1].splitlines():
            line = line.strip()
            if not line or line.startswith(("_", "loop_", "#", "data_")):
                continue
            parts = line.split()
            if len(parts) >= 4:
                processes.append(parts[:4])
    return counter, processes


def write_pipeline(counter, processes):
    lines = [
        "", "# version 30001", "", "data_pipeline_general", "",
        f"_rlnPipeLineJobCounter                      {counter}", "", "",
        "# version 30001", "", "data_pipeline_processes", "", "loop_",
        "_rlnPipeLineProcessName #1",
        "_rlnPipeLineProcessAlias #2",
        "_rlnPipeLineProcessTypeLabel #3",
        "_rlnPipeLineProcessStatusLabel #4",
    ]
    for p in processes:
        lines.append("  ".join(p))
    lines.append("")
    PIPELINE.write_text("\n".join(lines))


def job_type_label(job_star: Path) -> str:
    for line in job_star.read_text().splitlines():
        if line.strip().startswith("_rlnJobTypeLabel"):
            return line.split(None, 1)[1].strip().strip('"')
    return ""


def add_job_from_star(job_star: Path, alias: str) -> int:
    label = job_type_label(job_star)
    base = label.split(".")[0] + "." + label.split(".")[1] if label.count(".") >= 1 else label
    dirname = DIRNAME_BY_LABEL.get(base)
    if dirname is None:
        sys.stderr.write(f"ERROR: unknown job type label: {label}\n")
        return 1
    counter, processes = read_pipeline()
    name = f"{dirname}/job{counter:03d}/"
    job_dir = Path(dirname) / f"job{counter:03d}"
    job_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(job_star, job_dir / "job.star")
    processes.append([name, alias or "None", label, "Scheduled"])
    write_pipeline(counter + 1, processes)
    return 0


def check_job_completion() -> int:
    counter, processes = read_pipeline()
    changed = False
    for p in processes:
        # Only "Running" -- confirmed against the real 5.0.1 binary
        # (PipeLine::checkProcessCompletion, src/pipeliner.cpp: "Only check
        # running processes for file existence"). A job added via
        # --addJobFromStar is always "Scheduled" and STAYS that way through
        # this call, no matter what exit files exist in its directory --
        # this fake used to accept "Scheduled" here too, which is exactly
        # backwards and let a real bug (jobs stuck "Scheduled" forever)
        # hide behind a passing test suite. See pipeline_bridge.
        # set_process_status for the direct-write fix that's actually
        # needed to reach "Running" at all.
        if p[3] != "Running":
            continue
        d = Path(p[0].rstrip("/"))
        for fname, status in EXIT_FILES.items():
            if (d / fname).exists():
                p[3] = status
                changed = True
                break
    if changed:
        write_pipeline(counter, processes)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--check_job_completion" in args:
        return check_job_completion()
    if "--addJobFromStar" in args:
        star = Path(args[args.index("--addJobFromStar") + 1])
        alias = ""
        if "--setJobAlias" in args:
            alias = args[args.index("--setJobAlias") + 1]
        return add_job_from_star(star, alias)
    sys.stderr.write("fake_relion_pipeliner: nothing to do\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
