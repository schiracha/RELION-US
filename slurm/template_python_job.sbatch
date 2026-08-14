#!/bin/bash
#
# Template SBATCH script for a Python conversion/processing step (e.g. one
# of RELION-US's backend/converters/ modules run over a full dataset, or
# anything that pulls from the web or runs iteratively) — per your rule
# that only very simple, quick, non-iterative bash belongs outside the
# SLURM queue.
#
# See template_relion_job.sbatch for notes on confirmed Rivanna/Afton
# partition names and the lmod module system.
#
# NOTE: as of this version, RELION-US's job popups execute their command
# directly via subprocess, not through this template (SLURM submission
# from inside the GUI is a planned follow-up, see docs/ARCHITECTURE.md).
# This template and submit.py are for running RELION-US's converters (or
# any RELION job) as a standalone batch job in the meantime.

#SBATCH --job-name=relion_us_py_job
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
# module load imod     # uncomment if the script calls backend/converters/imod_bridge.py

# Activate (or create once, then reuse) a conda/venv environment with
# RELION-US's dependencies (see ../install.sh / ../backend/requirements.txt):
# conda activate relion_us
# -- or, if you set up the venv via install.sh instead of conda:
# source /path/to/relion_us/venv/bin/activate

cd "${SLURM_SUBMIT_DIR}"

python3 "$@"
