#!/bin/bash
#
# Template SBATCH script for a RELION tomography processing step on
# UVA Research Computing's Rivanna/Afton cluster.
#
# Confirmed against UVA Research Computing's Rivanna/Afton FAQ
# (https://www.rc.virginia.edu/userinfo/faq/rivanna-faq/, accessed
# 2026-08-14): standard partitions are `standard` (serial, single node),
# `parallel` (multi-node), `largemem`, `gpu`, and a `dev` queue for short
# test jobs; software is managed via the `lmod` module system; an
# allocation account (-A) is required for every job.
#
# I do NOT know your specific allocation account name or the exact RELION
# module version currently installed — fill in ACCOUNT_NAME below, and run
# `module spider relion` (and `module spider imod` if this job also touches
# .mod files) on a login node to find the exact module string to load,
# since exact versions/names change as RC updates the module tree.
#
# Fill in the placeholders (ALL_CAPS) below, then: sbatch this_script.sbatch

#SBATCH --job-name=relion_tomo_job
#SBATCH --account=ACCOUNT_NAME              # your allocation, e.g. -A mygroup
#SBATCH --partition=gpu                     # standard | parallel | largemem | gpu | dev
#SBATCH --gres=gpu:1                        # remove this line for CPU-only jobs
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=08:00:00
#SBATCH --output=%x-%j.out
#SBATCH --error=%x-%j.err

set -euo pipefail

module purge
module load relion   # confirm exact version string with: module spider relion
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
