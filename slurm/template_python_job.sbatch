#!/bin/bash
#
# Generic SBATCH template for a Python conversion/processing step (e.g. one
# of RELION-US's backend/converters/ modules run over a full dataset, or
# anything that pulls from the web or runs iteratively rather than a quick
# local operation).
#
# See template_relion_job.sbatch for a note on module systems. Partition
# and account/allocation syntax vary between clusters -- check your own
# site's documentation and adjust the placeholders below.
#
# NOTE: as of this version, RELION-US's job popups execute their command
# directly via subprocess, not through this template (SLURM submission
# from inside the GUI is a planned follow-up, see docs/ARCHITECTURE.md).
# This template and submit.py are for running a RELION-US converter, or
# any RELION job, as a standalone batch job in the meantime.

#SBATCH --job-name=relion_us_py_job
#SBATCH --account=ACCOUNT_NAME
#SBATCH --partition=PARTITION_NAME
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=02:00:00
#SBATCH --output=%x-%j.out
#SBATCH --error=%x-%j.err

set -euo pipefail

module purge 2>/dev/null || true
module load python 2>/dev/null || module load anaconda 2>/dev/null || true   # adjust to your cluster's module name
# module load imod     # uncomment if the script calls backend/converters/imod_bridge.py

# Activate whichever Python environment has RELION-US's backend
# dependencies installed (see ../README.md for how to build one --
# a venv or a conda env both work fine):
# source /path/to/relion_us/venv/bin/activate
# -- or --
# conda activate relion_us

cd "${SLURM_SUBMIT_DIR}"

python3 "$@"
