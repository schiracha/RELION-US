#!/bin/bash
#
# Generic SBATCH template for a RELION processing step on a SLURM cluster.
#
# This is intentionally generic — partition names, module names, and
# account/allocation syntax vary between clusters. Check your own site's
# documentation (or run `sinfo` for partition names and `module avail` /
# `module spider relion` for the exact RELION module string) and adjust
# the placeholders (ALL_CAPS) and the commented-out lines below before
# submitting.
#
# Usage: fill in the placeholders, then `sbatch template_relion_job.sbatch`

#SBATCH --job-name=relion_us_job
#SBATCH --account=ACCOUNT_NAME              # your allocation/account, if your cluster requires one
#SBATCH --partition=PARTITION_NAME          # e.g. gpu, standard, batch -- see `sinfo` on your cluster
#SBATCH --gres=gpu:1                        # remove this line for CPU-only jobs
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=08:00:00
#SBATCH --output=%x-%j.out
#SBATCH --error=%x-%j.err

set -euo pipefail

# Most clusters use environment modules (Lmod/Tcl modules) to expose
# software. Adjust or remove these lines to match how RELION (and
# optionally IMOD) are made available on your system -- some sites use
# `module load`, others expect you to activate a conda env or add a
# directory to PATH directly.
module purge 2>/dev/null || true
module load relion   # confirm the exact module name/version on your cluster
# module load imod   # uncomment if this job also calls IMOD CLI tools

# RELION job command goes here. Example (relion_tomo_reconstruct_particle
# is illustrative — replace with the actual relion_tomo_* / relion_refine
# invocation this job needs, matching what the RELION GUI would generate
# for the equivalent interactive job):
#
# relion_tomo_reconstruct_particle \
#     --i "${RELION_PROJECT_DIR}/optimisation_set.star" \
#     --o "${RELION_PROJECT_DIR}/Reconstruct/job001/" \
#     --j "${SLURM_CPUS_PER_TASK}"

echo "Fill in the actual relion_* command above before submitting."
exit 1
