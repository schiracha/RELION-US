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

#SBATCH --job-name=JOB_NAME
#SBATCH --account=ACCOUNT_NAME              # your allocation/account, if your cluster requires one
#SBATCH --partition=PARTITION_NAME          # e.g. gpu, standard, batch -- see `sinfo` on your cluster
GRES_LINE
#SBATCH --nodes=1
#SBATCH --ntasks=NTASKS
#SBATCH --cpus-per-task=CPUS_PER_TASK
#SBATCH --mem=MEM_SIZE
#SBATCH --time=TIME_LIMIT
#SBATCH --output=OUT_PATH
#SBATCH --error=ERR_PATH

set -euo pipefail

# Most clusters use environment modules (Lmod/Tcl modules) to expose
# software. Adjust or remove these lines to match how RELION (and
# optionally IMOD) are made available on your system -- some sites use
# `module load`, others expect you to activate a conda env or add a
# directory to PATH directly.
module purge 2>/dev/null || true
module load relion   # confirm the exact module name/version on your cluster
# module load imod   # uncomment if this job also calls IMOD CLI tools

# RELION job command. Filled in automatically when submitted from the
# RELION-US job popup's "Submit to SLURM cluster" option (the exact same
# command that would run locally) -- when filling this template by hand,
# replace the placeholder line below (all-caps, on its own) with the
# actual relion_tomo_*/relion_refine invocation this job needs, e.g.:
#
# relion_tomo_reconstruct_particle \
#     --i "${RELION_PROJECT_DIR}/optimisation_set.star" \
#     --o "${RELION_PROJECT_DIR}/Reconstruct/job001/" \
#     --j 8   # match the --cpus-per-task value requested above

RELION_COMMAND
