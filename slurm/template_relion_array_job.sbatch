#!/bin/bash
#
# SBATCH ARRAY template for running the SAME command as N independent
# SLURM tasks, one per line of an input-list file (e.g. one line per
# tomogram name, one per input file path -- whatever the command itself
# expects to read from $ARRAY_ITEM below). See template_relion_job.sbatch
# for the single-job equivalent and its own notes on module systems.
#
# This does NOT split a RELION job's own input STAR file for you -- it's
# a generic "run this command N times, each with a different resolved
# item" primitive (see GitHub issue #52 for why: auto-splitting/merging a
# real RELION job's STAR file per task is job-type-specific and out of
# scope here). It's most directly useful for an External Job whose own
# command can reference $ARRAY_ITEM, or any program that accepts a
# single-item override on its own command line.
#
# Usage: fill in the placeholders, then `sbatch template_relion_array_job.sbatch`

#SBATCH --job-name=JOB_NAME
#SBATCH --account=ACCOUNT_NAME              # your allocation/account, if your cluster requires one
#SBATCH --partition=PARTITION_NAME          # e.g. gpu, standard, batch -- see `sinfo` on your cluster
GRES_LINE
#SBATCH --nodes=1
#SBATCH --ntasks=NTASKS
#SBATCH --cpus-per-task=CPUS_PER_TASK
#SBATCH --mem=MEM_SIZE
#SBATCH --time=TIME_LIMIT
#SBATCH --array=ARRAY_RANGE
#SBATCH --output=OUT_PATH
#SBATCH --error=ERR_PATH

set -euo pipefail

module purge 2>/dev/null || true
module load relion   # confirm the exact module name/version on your cluster
# module load imod   # uncomment if this job also calls IMOD CLI tools

# Resolve THIS task's own item from the input-list file (one line per
# array index, 1-indexed for sed since SLURM_ARRAY_TASK_ID itself is
# 0-indexed to match this app's own array_items list order).
ARRAY_ITEM=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" INPUT_LIST_PATH)
export ARRAY_ITEM

# RELION job command. Filled in automatically when submitted from the
# RELION-US job popup's "Submit as SLURM array" option -- reference
# $ARRAY_ITEM in the command itself (in the popup's own editable command
# box, before submitting) to have each of the N tasks act on its own
# resolved item, e.g. for an External Job:
#
# my_external_tool --input "$ARRAY_ITEM" --output "job_dir/${SLURM_ARRAY_TASK_ID}"

RELION_COMMAND
