#!/bin/bash
# install.sh — one-time environment setup for RELION-US.
#
# Creates a Python virtual environment and installs the backend's
# dependencies: FastAPI/uvicorn/websockets for the server, pandas/starfile
# for the IMOD/Warp-M/DeepETPicker import bridges, pytest for the test
# suite. This never touches RELION itself — RELION-US calls your existing
# `relion_*` command-line programs, it doesn't install, replace, or wrap
# RELION's own GUI (see docs/ARCHITECTURE.md).
#
# Note on Rivanna/Afton: the `pip install` step below pulls packages from
# PyPI. That's a one-time, ~1 minute operation, and typical HPC practice is
# to run this kind of interactive environment setup directly on a login
# node the same way you'd run `pip install --user` for any tool — it's not
# the iterative/long-running kind of job the SLURM-for-web-pulls rule is
# aimed at. If you'd rather keep it off the login node entirely, submit it
# instead, e.g.:
#   sbatch --wrap="bash install.sh" -A <account> -p standard -t 00:10:00
#
# Usage:
#   ./install.sh              # creates ./venv here
#   ./install.sh /path/to/venv

set -euo pipefail

VENV_DIR="${1:-venv}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r "$SCRIPT_DIR/backend/requirements.txt"

echo
echo "RELION-US environment ready in: $VENV_DIR"
echo
echo "To run it:"
echo "  source $VENV_DIR/bin/activate"
echo "  ./run.sh"
echo
echo "(Or, from inside an existing RELION project directory, run ./run.sh"
echo " from there and it opens that project automatically — otherwise use"
echo " the 'Change Project' button in the top bar once it's running.)"
echo
echo "Optional, only needed to run the browser-based smoke tests"
echo "(test_frontend.py / test_frontend_project.py) — the playwright"
echo "Python package is already installed, just fetch a browser for it:"
echo "  playwright install chromium"
