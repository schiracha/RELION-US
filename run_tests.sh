#!/usr/bin/env bash
#
# run_tests.sh — test runner for RELION-US.
#
# The backend suite is cheap and always runs. The browser suites are not: each
# one needs its own backend on its own throwaway project, and running all of
# them to check a change that touched one module is minutes of wall clock for
# no information. So they are grouped into tiers you pick from.
#
#   ./run_tests.sh              # backend pytest only (the default; seconds)
#   ./run_tests.sh viewer       # + the tomogram viewer / recent-projects suite
#   ./run_tests.sh progress     # + the Progress tab / theme / file-picker suite
#   ./run_tests.sh options      # + where a job's options live (top panel /
#                               #   Advanced tab) and the MPI/threads/extra-args
#                               #   wiring
#   ./run_tests.sh jobs         # + job popups, Command Center, abort/overwrite
#   ./run_tests.sh project      # + Change Project, recents, Create Folder
#   ./run_tests.sh ui           # + every browser suite
#   ./run_tests.sh all          # everything (use before staging a milestone)
#
# Tiers can be combined:  ./run_tests.sh viewer progress
#
# Which tier for which change:
#   converters, viz.py, progress.py, job_registry ....... (default)
#   the tomogram viewer or the file picker ............... viewer
#   the Progress tab or the theme ........................ progress
#   job popups, run/abort/overwrite, Outputs ............. jobs
#   job_registry / the extractor / the Advanced tab ...... options
#   project_manager.py or the Change Project dialog ...... project
#   frontend/app.js scaffolding shared by all popups ..... ui
#
# Every browser suite gets a fresh project directory and its own backend on its
# own port, torn down afterwards. Nothing is left running and no existing
# project is touched -- a suite that asserts "no jobs yet" fails against a
# project that has history, which is a false alarm, not a bug.
#
# The `options` suite needs a program on PATH answering to a RELION binary
# name, so it can check what the Advanced tab lists. A stub printing RELION's
# own --help format is generated for it -- point RELION_US_REAL_BINARIES at a
# real RELION bin directory to run it against the genuine article instead.
#
# Environment:
#   RELION_US_PYTHON    python to use (default: python3)
#   RELION_US_CHROMIUM  chromium binary, if Playwright's own is not usable
#   RELION_US_PORT_BASE first port to allocate from (default: 8500)

set -uo pipefail

cd "$(dirname "$0")"

PYTHON="${RELION_US_PYTHON:-python3}"
PWD_APP="$PWD"
PORT_BASE="${RELION_US_PORT_BASE:-8500}"
TMPROOT="$(mktemp -d -t relion_us_tests.XXXXXX)"
FAILED=()
PASSED=()

MAIN_PID=$$

cleanup() {
  # Only the main shell cleans up. Bash runs EXIT traps in subshells too, and
  # a `$(...)` helper exiting would otherwise delete the temp tree and kill the
  # backend out from under the suite that is about to use it.
  [[ "$BASHPID" != "$MAIN_PID" ]] && return
  # Kill any backend this script started, whatever happened -- including on
  # Ctrl-C, which is when an orphaned uvicorn is most likely and most annoying
  # (the port stays busy and the next run silently talks to the old one).
  for pid in "${BACKEND_PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null
  done
  rm -rf "$TMPROOT"
}
BACKEND_PIDS=()
BACKEND_PID=""
BACKEND_PORT=""
BACKEND_PROJ=""
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------

free_port() {
  local port=$PORT_BASE
  while "$PYTHON" - "$port" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(0)      # in use
finally:
    s.close()
sys.exit(1)          # free
PY
  do
    port=$((port + 1))
    if (( port > PORT_BASE + 50 )); then
      echo "no free port found from $PORT_BASE" >&2
      return 1
    fi
  done
  PORT_BASE=$((port + 1))
  echo "$port"
}

# A stand-in for `relion_refine --help`, in RELION's own IOParser usage format
# (src/args.cpp). The Advanced-tab suite needs *a* program to interrogate; this
# keeps the test hermetic on a machine with no RELION install.
make_stub_bin() {
  local dir="$TMPROOT/stub-bin"
  [[ -d "$dir" ]] && { echo "$dir"; return; }
  mkdir -p "$dir"
  cat > "$dir/relion_refine" <<'STUB'
#!/usr/bin/env python3
print("""+++ RELION: command line arguments (with defaults for optional ones between parantheses) +++
====== General options ===== 
                                --i : Input images (in a star-file)
                                --o : Output rootname
                     --angpix (1.0) : Pixel size in Angstroms
                            --j (1) : Number of threads
====== Expert options ===== 
          --dont_check_norm (false) : Skip the check whether images are normalised
                         --verb (1) : Verbosity (1=normal, 0=silent)
           --onlyflipphases (false) : Only flip phases, do not correct amplitudes
                          --pad (2) : Oversampling factor for the Fourier transforms
                          --version : Print RELION version and exit""")
STUB
  chmod +x "$dir/relion_refine"
  echo "$dir"
}

# start_backend <name>
# Sets BACKEND_PORT / BACKEND_PROJ / BACKEND_PID. Deliberately not "echo the
# port and capture it": a command substitution runs in a subshell, so the
# backend PID recorded there would be lost and the process left orphaned.
start_backend() {
  local name="$1"
  local proj="$TMPROOT/$name"
  local port; port="$(free_port)" || return 1

  # A directory only counts as a project if it has RELION's own
  # default_pipeline.star or our marker; without the marker the backend falls
  # back to the app's own relion_project, and the suite would run against
  # whatever history is sitting in it.
  mkdir -p "$proj/.relion_us"
  echo "[]" > "$proj/.relion_us/run_history.json"

  # XDG_CONFIG_HOME is redirected so the recent-projects cache written by the
  # test run never touches the developer's real one.
  # `exec` matters: without it, $! is the subshell's pid and killing the
  # subshell leaves uvicorn itself running -- the port stays busy and the next
  # run silently talks to a backend pointed at the wrong project.
  local stub_path=""
  if [[ -z "${RELION_US_REAL_BINARIES:-}" ]]; then
    stub_path="$(make_stub_bin)"
  else
    stub_path="$RELION_US_REAL_BINARIES"
  fi

  (
    cd "$proj" || exit 1
    export XDG_CONFIG_HOME="$TMPROOT/$name-config"
    export PATH="$stub_path:$PATH"
    exec "$PYTHON" -m uvicorn main:app --host 127.0.0.1 --port "$port" \
      --app-dir "$PWD_APP/backend" > "$TMPROOT/$name.log" 2>&1
  ) &
  BACKEND_PID="$!"
  BACKEND_PIDS+=("$BACKEND_PID")

  # Wait for it to answer rather than sleeping a guessed amount.
  for _ in $(seq 1 60); do
    if "$PYTHON" - "$port" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/project", timeout=1)
except Exception:
    sys.exit(1)
PY
    then
      BACKEND_PORT="$port"
      BACKEND_PROJ="$proj"
      return 0
    fi
    sleep 0.5
  done
  echo "backend for $name did not come up; see $TMPROOT/$name.log" >&2
  cat "$TMPROOT/$name.log" >&2
  return 1
}

stop_backend() {
  [[ -z "$BACKEND_PID" ]] && return
  kill "$BACKEND_PID" 2>/dev/null
  wait "$BACKEND_PID" 2>/dev/null
  BACKEND_PID=""
}

# run_browser_suite <script> [pass_project_dir]
run_browser_suite() {
  local script="$1" pass_proj="${2:-no}"
  local name="${script%.py}"
  echo
  echo "=== $script"
  if ! start_backend "$name"; then
    FAILED+=("$script (backend failed to start)")
    return
  fi

  local args=("http://127.0.0.1:$BACKEND_PORT")
  [[ "$pass_proj" == "yes" ]] && args+=("$BACKEND_PROJ")

  if "$PYTHON" "$script" "${args[@]}"; then
    PASSED+=("$script")
  else
    FAILED+=("$script")
  fi
  stop_backend
}

# ---------------------------------------------------------------------------

TIERS=("$@")
[[ ${#TIERS[@]} -eq 0 ]] && TIERS=(fast)

wants() {
  local tier="$1"
  for t in "${TIERS[@]}"; do
    [[ "$t" == "$tier" || "$t" == "all" ]] && return 0
    [[ "$t" == "ui" && "$tier" != "fast" ]] && return 0
  done
  return 1
}

# Backend suite: always. It is ~5 s and it is the layer where a silently-wrong
# number hides, so there is never a good reason to skip it.
echo "=== backend pytest"
if (cd backend && "$PYTHON" -m pytest -q); then
  PASSED+=("backend pytest")
else
  FAILED+=("backend pytest")
fi

wants viewer   && run_browser_suite test_viewer_and_recents.py yes
wants progress && run_browser_suite test_progress_and_theme.py yes
wants options  && run_browser_suite test_job_options_panel.py
wants jobs     && run_browser_suite test_frontend.py
wants jobs     && run_browser_suite test_command_center.py
wants jobs     && run_browser_suite test_command_center_abort_overwrite.py
wants project  && run_browser_suite test_frontend_project.py

echo
echo "======================================================================"
for s in "${PASSED[@]:-}"; do [[ -n "$s" ]] && echo "  PASS  $s"; done
for s in "${FAILED[@]:-}"; do [[ -n "$s" ]] && echo "  FAIL  $s"; done
echo "======================================================================"

if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo "logs kept for this run were under $TMPROOT (removed on exit)"
  exit 1
fi
echo "All selected suites passed."
