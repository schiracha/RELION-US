#!/bin/bash
# run.sh — launch the RELION-US backend (it also serves the frontend, so
# this is the only process you need to start).
#
# Usage:
#   ./run.sh                        # binds 0.0.0.0:8420
#   ./run.sh --port 8888
#   ./run.sh --host 127.0.0.1 --port 8420
#
# Run this from inside an existing RELION project directory to have
# RELION-US open it automatically ("Do I have to run this from the working
# directory?" — no, but if you do, it's used). Otherwise it falls back to a
# default project folder, and you switch to the real one from the
# "Change Project" button in the top bar once it's running.
#
# On Rivanna/Afton: launch this on a login node (or an interactive job),
# then either port-forward over SSH from your laptop
# (ssh -L 8420:localhost:8420 <node>) or connect directly if your network
# allows it.

set -euo pipefail

HOST="0.0.0.0"
PORT="8420"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--host HOST] [--port PORT]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/backend"
exec uvicorn main:app --host "$HOST" --port "$PORT"
