#!/bin/bash
#
# Template SBATCH script for a Python conversion/processing step (e.g. one
# of the converters/ modules run over a full dataset, or anything that
# pulls from the web or runs iteratively) — per your rule that only very
# simple, quick, non-iterative bash belongs outside the SLURM queue.
#
# See template_relion_job.sbatch for notes on confirmed Rivanna/Afton
# partition names and the lmod module system.

#SBATCH --job-name=tomo_bridge_py_job
#SBATCH --account=ACCOUNT_NAME
#SBATCH --partition=standard
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=02:00:00
#SBATCH --output=%x-%j.out
#SBATCH --error=%x-%j.err

set -euo pipefail

module purge
module load anaconda   # confirm exact module name with: module spider anaconda
# module load imod     # uncomment if the script calls converters/imod_bridge.py

# Activate (or create once, then reuse) a conda/venv environment with this
# repo's dependencies: pandas, starfile
# conda activate relion_tomo_bridge

cd "${SLURM_SUBMIT_DIR}"

python3 "$@"
