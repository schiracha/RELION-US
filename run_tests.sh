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
#   ./run_tests.sh viewer       # + the tomogram viewer, recent-projects, the
#                               #   Progress tab, theme, and file-picker suite
#                               #   (viewer and progress share one script/
#                               #   backend -- pick either name, same suite)
#   ./run_tests.sh progress     # (see viewer, above)
#   ./run_tests.sh options      # + where a job's options live (Inputs tab /
#                               #   Advanced section) and the MPI/threads/extra-args
#                               #   wiring
#   ./run_tests.sh jobs         # + job popups, Command Center, abort/overwrite
#   ./run_tests.sh project      # + Change Project, recents, Create Folder
#   ./run_tests.sh legacy       # + opening a project built in RELION's own GUI,
#                               #   and the network view's geometry on a wide,
#                               #   branching, long-job-name pipeline
#   ./run_tests.sh auth         # + password protection (login/logout, the
#                               #   gate on pages/API/websocket)
#   ./run_tests.sh analyze      # + the Analyze popup (Menu > Tools > Analyze):
#                               #   pipeline graph, classification/refine
#                               #   charts, micrograph/particle scatter
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
#   job_registry / the extractor / the Advanced section .. options
#   project_manager.py or the Change Project dialog ...... project
#   job numbering, the Command Center, RELION's pipeline .. legacy
#   backend/auth.py, login.html, Run-RelionUS's auth flags auth
#   backend/analyze.py or the Analyze popup ............... analyze
#   frontend/app.js scaffolding shared by all popups ..... ui
#
# Every browser suite gets a fresh project directory and its own backend on its
# own port, torn down afterwards. Nothing is left running and no existing
# project is touched -- a suite that asserts "no jobs yet" fails against a
# project that has history, which is a false alarm, not a bug.
#
# Every suite's backend gets a stub `relion_refine`/`relion_refine_mpi` on
# PATH answering to RELION's own --help format (make_stub_bin) -- the
# `options` suite is the one that actually reads it (to check what the
# Advanced section lists), but it's generated unconditionally since it's
# cheap. Point RELION_US_REAL_BINARIES at a real RELION bin directory to run
# against the genuine article instead.
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
#
# relion_refine_mpi is the same stub under RELION's own MPI-binary naming
# convention, not a separate program: the Advanced section asks for the _mpi
# binary's own options whenever the popup's MPI-procs field is > 1 (a real
# RELION install ships both binaries, and both answer --help the same way via
# the same IOParser), so the suite's MPI test needs it on PATH too, exactly
# like the real thing.
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
  cp "$dir/relion_refine" "$dir/relion_refine_mpi"
  chmod +x "$dir/relion_refine_mpi"
  echo "$dir"
}

# The password protection suite needs a login already configured before its
# backend even starts -- unlike every other suite, which never touches
# backend/auth.py at all and so runs (correctly) with protection off, the
# default. Calls straight into auth.py rather than shelling out to
# Run-RelionUS, so this doesn't depend on that script's own interactive
# first-run prompt or argument parsing.
AUTH_TEST_PASSWORD="relion-us-test-password"
make_auth_config() {
  local config_home="$1"
  XDG_CONFIG_HOME="$config_home" "$PYTHON" - <<PY
import sys
sys.path.insert(0, "$PWD_APP/backend")
import auth
auth.set_password("$AUTH_TEST_PASSWORD")
auth.enable()
PY
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
  if [[ "$name" == test_legacy_project ]]; then
    # Deliberately NO .relion_us marker: the point is a project RELION built
    # and this app has never seen.
    make_legacy_project "$proj"
  elif [[ "$name" == test_network_branching ]]; then
    make_legacy_branchy_project "$proj"
  elif [[ "$name" == test_analyze ]]; then
    # Same branching pipeline test_network_branching.py uses (Pipeline tab
    # reuses that exact DAG rendering, see app.js's renderLineageGraph) plus
    # one of this app's OWN completed runs with real iteration files, so the
    # 2D Classification tab's convergence/distribution charts have something
    # real to plot -- see add_analyze_classification_run below.
    make_legacy_branchy_project "$proj"
    add_analyze_classification_run "$proj"
  else
    mkdir -p "$proj/.relion_us"
    echo "[]" > "$proj/.relion_us/run_history.json"
  fi

  # XDG_CONFIG_HOME is redirected so the recent-projects cache (and, for
  # test_auth, the login config) written by the test run never touches the
  # developer's real one.
  if [[ "$name" == test_auth ]]; then
    make_auth_config "$TMPROOT/$name-config"
  fi

  # `exec` matters: without it, $! is the subshell's pid and killing the
  # subshell leaves uvicorn itself running -- the port stays busy and the next
  # run silently talks to a backend pointed at the wrong project.
  local stub_path=""
  local sanitized_path="$PATH"
  if [[ -z "${RELION_US_REAL_BINARIES:-}" ]]; then
    stub_path="$(make_stub_bin)"
    # project_manager.pipeline_sync_setting now defaults to True -- without
    # this, a REAL relion_pipeliner elsewhere on PATH (this dev machine has
    # one installed) would make every job a browser test runs actually
    # attempt real pipeline registration: different job numbering (RELION's
    # own counter, not this app's), real default_pipeline.star writes, and
    # the "close any native RELION GUI" confirm dialog popping up mid-test.
    # The stub bin dir above never includes a relion_pipeliner, so this only
    # ever strips a REAL one found further down PATH. Not done in
    # RELION_US_REAL_BINARIES mode -- testing sync against genuine RELION
    # there is the whole point.
    local real_pipeliner; real_pipeliner="$(command -v relion_pipeliner 2>/dev/null || true)"
    if [[ -n "$real_pipeliner" ]]; then
      local real_dir; real_dir="$(dirname "$real_pipeliner")"
      sanitized_path="$(printf '%s' "$PATH" | tr ':' '\n' | grep -vFx "$real_dir" | tr '\n' ':')"
      sanitized_path="${sanitized_path%:}"
    fi
  else
    stub_path="$RELION_US_REAL_BINARIES"
  fi

  (
    cd "$proj" || exit 1
    export XDG_CONFIG_HOME="$TMPROOT/$name-config"
    export PATH="$stub_path:$sanitized_path"
    exec "$PYTHON" -m uvicorn main:app --host 127.0.0.1 --port "$port" \
      --app-dir "$PWD_APP/backend" > "$TMPROOT/$name.log" 2>&1
  ) &
  BACKEND_PID="$!"
  BACKEND_PIDS+=("$BACKEND_PID")

  # Wait for it to answer rather than sleeping a guessed amount. /api/auth/status
  # rather than /api/project: it's the one endpoint the auth gate always
  # answers with 200 regardless of whether THIS suite's backend has
  # password protection on (see make_auth_config above) -- polling a gated
  # endpoint would make urlopen see every response as an error (401) and
  # this loop would time out waiting for a backend that is actually up.
  for _ in $(seq 1 60); do
    if "$PYTHON" - "$port" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/auth/status", timeout=1)
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

# Shared by make_legacy_project and make_legacy_branchy_project below: both
# build a default_pipeline.star with the exact same four-block shape (job
# counter, then processes/output_edges/input_edges loop_ tables) -- only the
# counter and the actual data rows differ between the two fixtures. Each of
# processes/output_edges/input_edges is one pre-formatted row per line (no
# trailing newline needed).
_write_pipeline_star() {
  local proj="$1" counter="$2" processes="$3" output_edges="$4" input_edges="$5"
  cat > "$proj/default_pipeline.star" <<STAR

# version 30001

data_pipeline_general

_rlnPipeLineJobCounter                      $counter


# version 30001

data_pipeline_processes

loop_
_rlnPipeLineProcessName #1
_rlnPipeLineProcessAlias #2
_rlnPipeLineProcessTypeLabel #3
_rlnPipeLineProcessStatusLabel #4
$processes


# version 30001

data_pipeline_output_edges

loop_
_rlnPipeLineEdgeProcess #1
_rlnPipeLineEdgeToNode #2
$output_edges


# version 30001

data_pipeline_input_edges

loop_
_rlnPipeLineEdgeFromNode #1
_rlnPipeLineEdgeProcess #2
$input_edges
STAR
}

# A project as RELION's own GUI leaves one: a default_pipeline.star with a job
# counter and a process list, real job directories, and a job.star holding the
# options one of them ran with. The `legacy` suite opens this instead of an
# empty project.
make_legacy_project() {
  local proj="$1"
  mkdir -p "$proj"
  _write_pipeline_star "$proj" 12 \
"Import/job001/       None            relion.import.movies     Succeeded
MotionCorr/job002/   my_motioncorr   relion.motioncorr.own    Succeeded
CtfFind/job003/      None            relion.ctffind.ctffind4  Succeeded
Class2D/job005/      None            relion.class2d.em        Failed
Refine3D/job011/     None            relion.refine3d          Succeeded" \
"Import/job001/ Import/job001/movies.star
MotionCorr/job002/ MotionCorr/job002/corrected.star
CtfFind/job003/ CtfFind/job003/ctf.star
Class2D/job005/ Class2D/job005/particles.star" \
"Import/job001/movies.star MotionCorr/job002/
MotionCorr/job002/corrected.star CtfFind/job003/
CtfFind/job003/ctf.star Class2D/job005/
Class2D/job005/particles.star Refine3D/job011/"
  mkdir -p "$proj/Import/job001" "$proj/MotionCorr/job002" \
           "$proj/CtfFind/job003" "$proj/Class2D/job005" "$proj/Refine3D/job011"
  cat > "$proj/Class2D/job005/job.star" <<'STAR'

# version 30001

data_job

_rlnJobTypeLabel                     relion.class2d.em
_rlnJobIsContinue                             0
_rlnJobIsTomo                                 0


# version 30001

data_joboptions_values

loop_
_rlnJobOptionVariable #1
_rlnJobOptionValue #2
fn_img            Select/job004/particles.star
nr_classes        50
tau_fudge         4
particle_diameter 180
do_ctf_correction Yes
nr_mpi            5
nr_threads        8
STAR
  # A few iterations of real RELION output, so the Progress tab has something
  # to plot for a job this app never ran.
  "$PYTHON" - "$proj/Class2D/job005" <<'PY'
import sys
from pathlib import Path
import numpy as np, mrcfile, starfile, pandas as pd
d = Path(sys.argv[1]); NC = 4
for it in (1, 2, 3):
    with mrcfile.new(d / f"run_it{it:03d}_classes.mrcs", overwrite=True) as m:
        m.set_data((np.random.rand(NC, 32, 32) * 0.3).astype(np.float32))
    starfile.write({
        "model_general": pd.DataFrame({"rlnCurrentResolution": [1 / (18.0 - it)],
                                       "rlnNrClasses": [NC],
                                       "rlnReferenceDimensionality": [2],
                                       "rlnPixelSize": [1.4]}),
        "model_classes": pd.DataFrame({
            "rlnReferenceImage": [f"{k+1:06d}@run_it{it:03d}_classes.mrcs" for k in range(NC)],
            "rlnClassDistribution": [0.4, 0.3, 0.2, 0.1],
            "rlnEstimatedResolution": [20.0 - it + k for k in range(NC)]})},
        d / f"run_it{it:03d}_model.star", overwrite=True)
PY
}

# A wider, taller lineage than make_legacy_project's straight 5-job chain:
# one job fans out to four children, one of those fans out to two more, and
# every job carries one of RELION's real (and long -- some wrap to a second
# line at the network view's 176px node width) tomography display names.
# Exists to catch network-view geometry bugs a simple linear chain can't --
# see test_network_branching.py, added for exactly that after the padding
# bug described in style.css's "Network view" comment above #ccNetworkView.
make_legacy_branchy_project() {
  local proj="$1"
  mkdir -p "$proj"
  _write_pipeline_star "$proj" 22 \
"TomoExcludeTilt/job004/       None            relion.excludetilts          Succeeded
TomoAlign/job005/             None            relion.aligntiltseries       Succeeded
TomoRecon/job010/             None            relion.reconstructtomograms  Succeeded
TomoSubtomo/job011/           None            relion.pseudosubtomo         Succeeded
TomoRecon/job015/             None            relion.reconstructtomograms  Succeeded
TomoAlign/job013/             None            relion.aligntiltseries       Succeeded
TomoExcludeTilt/job014/       None            relion.excludetilts          Succeeded
TomoSubtomo/job018/           None            relion.pseudosubtomo         Succeeded
TomoRecon/job021/             None            relion.reconstructtomograms  Succeeded" \
"TomoExcludeTilt/job004/ TomoExcludeTilt/job004/tilts.star
TomoAlign/job005/       TomoAlign/job005/aligned.star
TomoRecon/job010/       TomoRecon/job010/tomograms.star
TomoSubtomo/job011/     TomoSubtomo/job011/subtomo.star
TomoRecon/job015/       TomoRecon/job015/tomograms.star
TomoAlign/job013/       TomoAlign/job013/aligned.star
TomoExcludeTilt/job014/ TomoExcludeTilt/job014/tilts.star
TomoSubtomo/job018/     TomoSubtomo/job018/subtomo.star
TomoRecon/job021/       TomoRecon/job021/tomograms.star" \
"TomoExcludeTilt/job004/tilts.star   TomoAlign/job005/
TomoAlign/job005/aligned.star       TomoRecon/job010/
TomoRecon/job010/tomograms.star     TomoSubtomo/job011/
TomoRecon/job010/tomograms.star     TomoRecon/job015/
TomoRecon/job010/tomograms.star     TomoAlign/job013/
TomoRecon/job010/tomograms.star     TomoExcludeTilt/job014/
TomoExcludeTilt/job014/tilts.star   TomoSubtomo/job018/
TomoExcludeTilt/job014/tilts.star   TomoRecon/job021/"
  mkdir -p "$proj/TomoExcludeTilt/job004" "$proj/TomoAlign/job005" \
           "$proj/TomoRecon/job010" "$proj/TomoSubtomo/job011" \
           "$proj/TomoRecon/job015" "$proj/TomoAlign/job013" \
           "$proj/TomoExcludeTilt/job014" "$proj/TomoSubtomo/job018" \
           "$proj/TomoRecon/job021"
}

# add_analyze_classification_run <project_dir>
# Two completed runs of THIS app's own (not RELION-native, so no job.star/
# pipeline entry needed -- .relion_us/run_history.json is enough for
# run_manager._resolve_run_cwd to find them): a Class2D job with 3 real
# iterations of run_it###_model.star + run_it###_optimiser.star (2D
# Classification tab's convergence/class-distribution charts), and a
# Class3D job additionally carrying model_class_N FSC/SSNR sub-blocks and a
# run_it###_data.star (3D Classification tab's FSC chart + viewing-
# direction heatmap), plus an Extract/job024/particles.star (Particles tab)
# and a CtfFind/job003 + MotionCorr/job002 pair (Micrographs tab -- the
# picked file's rlnMicrographName values point back at MotionCorr/job002/
# so its corrected_micrographs.star gets merged in). STAR shapes match real
# RELION output (list blocks for model_general/optimiser_general, loop_ for
# model_classes/model_class_N/micrographs) -- same discipline
# test_viz_and_progress.py's own fixtures use.
add_analyze_classification_run() {
  local proj="$1"
  local job2d="$proj/Class2D/job022"
  local job3d="$proj/Class3D/job023"
  local job_extract="$proj/Extract/job024"
  local job_motioncorr="$proj/MotionCorr/job002"
  local job_ctffind="$proj/CtfFind/job003"
  mkdir -p "$job2d" "$job3d" "$job_extract" "$job_motioncorr" "$job_ctffind" "$proj/.relion_us"
  cat > "$proj/.relion_us/run_history.json" <<JSON
[{"run_id": "analyze-fixture-c2d", "source": null, "internal_name": "Class2D",
  "display_name": "2D Classification", "job_name": "job022", "job_number": 22,
  "command": "true", "cwd": "$job2d", "status": "completed", "exit_code": 0,
  "started_at": 1700000000.0, "ended_at": 1700000100.0, "field_values": {},
  "detected_inputs": [], "note": "", "alias": "", "pid": null, "abortable": false},
 {"run_id": "analyze-fixture-c3d", "source": null, "internal_name": "Class3D",
  "display_name": "3D Classification", "job_name": "job023", "job_number": 23,
  "command": "true", "cwd": "$job3d", "status": "completed", "exit_code": 0,
  "started_at": 1700000200.0, "ended_at": 1700000300.0, "field_values": {},
  "detected_inputs": [], "note": "", "alias": "", "pid": null, "abortable": false}]
JSON
  "$PYTHON" - "$job2d" "$job3d" "$job_extract" "$job_motioncorr" "$job_ctffind" <<'PY'
import sys
import numpy as np
import pandas as pd
import starfile

job2d, job3d, job_extract, job_motioncorr, job_ctffind = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

nc = 3
for it in range(1, 4):
    dist = [0.5 - it * 0.03, 0.3 + it * 0.01, 0.2 + it * 0.02]
    starfile.write({
        "model_general": {
            "rlnCurrentResolution": 1.0 / (25.0 - it),
            "rlnNrClasses": nc,
            "rlnReferenceDimensionality": 2,
            "rlnPixelSize": 1.4,
        },
        "model_classes": pd.DataFrame({
            "rlnReferenceImage": [f"{k + 1:06d}@run_it{it:03d}_classes.mrcs" for k in range(nc)],
            "rlnClassDistribution": dist,
            "rlnEstimatedResolution": [20.0 - it + k for k in range(nc)],
            "rlnAccuracyRotations": [3.0] * nc,
            "rlnAccuracyTranslationsAngst": [1.1] * nc,
        }),
    }, f"{job2d}/run_it{it:03d}_model.star", overwrite=True)
    starfile.write({
        "optimiser_general": {
            "rlnChangesOptimalOrientations": 10.0 / it,
            "rlnChangesOptimalOffsets": 3.0 / it,
            "rlnChangesOptimalClasses": 50.0 / it,
        }
    }, f"{job2d}/run_it{it:03d}_optimiser.star", overwrite=True)

nc3 = 2
shells = list(range(20))
resolutions = [40.0 / (i + 1) for i in shells]
for it in range(1, 3):
    dist3 = [0.6, 0.4] if it == 1 else [0.55, 0.45]
    blocks = {
        "model_general": {
            "rlnCurrentResolution": 1.0 / (15.0 - it),
            "rlnNrClasses": nc3,
            "rlnReferenceDimensionality": 3,
            "rlnPixelSize": 1.4,
        },
        "model_classes": pd.DataFrame({
            "rlnReferenceImage": [f"run_it{it:03d}_class{k + 1:03d}.mrc" for k in range(nc3)],
            "rlnClassDistribution": dist3,
            "rlnEstimatedResolution": [8.0, 9.0],
            "rlnAccuracyRotations": [3.0] * nc3,
            "rlnAccuracyTranslationsAngst": [1.1] * nc3,
        }),
    }
    for k in range(nc3):
        fsc = [max(0.0, 1.0 - i / 16.0 - k * 0.05) for i in shells]
        ssnr = [max(0.01, 15.0 - i * 0.7 - k * 2) for i in shells]
        blocks[f"model_class_{k + 1}"] = pd.DataFrame({
            "rlnSpectralIndex": shells,
            "rlnAngstromResolution": resolutions,
            "rlnGoldStandardFsc": fsc,
            "rlnSsnrMap": ssnr,
        })
    starfile.write(blocks, f"{job3d}/run_it{it:03d}_model.star", overwrite=True)
    starfile.write({"optimiser_general": {
        "rlnChangesOptimalOrientations": 4.0 / it,
        "rlnChangesOptimalOffsets": 1.0 / it,
        "rlnChangesOptimalClasses": 15.0 / it,
    }}, f"{job3d}/run_it{it:03d}_optimiser.star", overwrite=True)

rng = np.random.default_rng(0)
n = 200
particles = pd.DataFrame({
    "rlnAngleRot": rng.uniform(-180, 180, n),
    "rlnAngleTilt": rng.uniform(0, 180, n),
})
starfile.write({"particles": particles}, f"{job3d}/run_it002_data.star", overwrite=True)

# For the Particles tab (C4) -- not tied to a run, just a real particles
# STAR somewhere under the project for its own path input/Browse button.
n_p = 40
starfile.write({
    "optics": pd.DataFrame({"rlnOpticsGroup": [1], "rlnOpticsGroupName": ["opticsGroup1"], "rlnVoltage": [300.0]}),
    "particles": pd.DataFrame({
        "rlnMicrographName": [f"mic_{i % 5}.mrc" for i in range(n_p)],
        "rlnImageName": [f"{i + 1:06d}@Extract/job024/particles.mrcs" for i in range(n_p)],
        "rlnCoordinateX": rng.uniform(0, 4000, n_p),
        "rlnCoordinateY": rng.uniform(0, 4000, n_p),
        "rlnDefocusU": rng.normal(15000, 2000, n_p),
        "rlnOpticsGroup": [1] * n_p,
    }),
}, f"{job_extract}/particles.star", overwrite=True)

# For the Micrographs tab (C4) -- a CtfFind-style picked STAR whose
# rlnMicrographName values point back at MotionCorr/job002/, so
# read_micrograph_scatter_columns' job-dir regex finds and merges in
# corrected_micrographs.star's own motion-tracking columns.
n_m = 6
mic_names = [f"MotionCorr/job002/mic_{i}.mrc" for i in range(n_m)]
starfile.write({
    "optics": pd.DataFrame({"rlnOpticsGroup": [1], "rlnOpticsGroupName": ["opticsGroup1"], "rlnVoltage": [300.0]}),
    "micrographs": pd.DataFrame({
        "rlnMicrographName": mic_names,
        "rlnDefocusU": rng.normal(15000, 2000, n_m),
        "rlnCtfMaxResolution": rng.uniform(3.0, 8.0, n_m),
        "rlnOpticsGroup": [1] * n_m,
    }),
}, f"{job_ctffind}/micrographs_ctf.star", overwrite=True)
starfile.write({
    "micrographs": pd.DataFrame({
        "rlnMicrographName": mic_names,
        "rlnAccumMotionTotal": rng.uniform(10.0, 60.0, n_m),
        "rlnAccumMotionEarly": rng.uniform(2.0, 10.0, n_m),
        "rlnAccumMotionLate": rng.uniform(5.0, 50.0, n_m),
    }),
}, f"{job_motioncorr}/corrected_micrographs.star", overwrite=True)
PY
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

# A typo'd tier name (e.g. "vewer") used to just silently match nothing --
# wants() would never fire, only backend pytest would run, and the script
# would still print "All selected suites passed.", indistinguishable from a
# real pass of the tier the caller actually meant to run. Validate up front
# instead. "fast" is the internal default-with-no-args sentinel (see wants()
# below), not something a caller passes explicitly, but it's harmless either
# way so it's allowed here too.
KNOWN_TIERS=(fast viewer progress options jobs project legacy auth analyze ui all)
for t in "${TIERS[@]}"; do
  known=0
  for k in "${KNOWN_TIERS[@]}"; do
    [[ "$t" == "$k" ]] && { known=1; break; }
  done
  if [[ "$known" -eq 0 ]]; then
    echo "Unknown tier: '$t'" >&2
    echo "Known tiers: ${KNOWN_TIERS[*]/fast/}" >&2
    exit 1
  fi
done

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

# viewer and progress share one script/backend now (test_viz_and_progress.py
# covers both) -- run it once if either tier was requested, not twice.
if wants viewer || wants progress; then
  run_browser_suite test_viz_and_progress.py yes
fi
wants options  && run_browser_suite test_job_options_panel.py
wants jobs     && run_browser_suite test_jobs.py
wants project  && run_browser_suite test_frontend_project.py
wants legacy   && run_browser_suite test_legacy_project.py yes
wants legacy   && run_browser_suite test_network_branching.py
wants auth     && run_browser_suite test_auth.py
wants analyze  && run_browser_suite test_analyze.py yes

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
