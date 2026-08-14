# RELION-US

**RELION - User Sourced** — a browser-based, portable job manager for
RELION, built as a *companion* to RELION, not a modified RELION GUI. It
reads RELION's own source (`pipeline_jobs.cpp`/`.h`, `gui_jobwindow.cpp`)
to build accurate forms for every RELION job type (32 of them, single-
particle and tomography), folds in IMOD/Warp-M/DeepETPicker import bridges
as three more entries in the same Jobs list, and runs everything through
one consistent popup-window UI: standard inputs on top, an Advanced tab
with every other option, an Errors tab, live streaming output at the
bottom — and, critically, **an editable command box you approve before
anything runs.**

## Why this exists

RELION's own GUI is a compiled Qt5/C++ application. Its command-assembly
logic sometimes duplicates flags or inserts options you can't see or
override from the "additional arguments" box — there's no way to see or
edit the exact string it's about to run. RELION-US never does that:
whatever is in the command textbox when you click Run is executed exactly
as written, via the shell, nothing added or removed. The draft command
that pre-fills that box is a best-effort starting point (see "How the
draft command is built" below) — always check it, and the job's real
RELION C++ source is one tab away for cross-referencing.

It's also built to be portable and multi-machine-friendly: it's a normal
web page. Run the backend on your workstation or a remote HPC cluster
login node, and open the page from any browser on the network — no Qt, no
X11 forwarding, no display server. See `docs/ARCHITECTURE.md` for the full
design rationale, including why this is a separate layer rather than a
modified RELION GUI, and confirmation that RELION-US never runs RELION's
own compiled GUI binary at runtime (only RELION's command-line programs).

## Installing and running it

There's no install script — build the environment yourself with whatever
Python tooling you already use, so this stays portable across Linux
distributions rather than assuming one package manager or layout. Any
approach that gets `backend/requirements.txt` installed into a Python 3.10+
environment works; a plain venv is the least assumption-laden option and
works the same way on every distro:

```bash
python3 -m venv relnu
source relnu/bin/activate
pip install -r backend/requirements.txt
```

If your distro's `python3 -m venv` fails because the `venv` module isn't
installed (common on minimal installs), install it from your distro's
package manager first — e.g. `sudo apt install python3-venv` on Debian/
Ubuntu, `sudo dnf install python3` on Fedora (venv is included), pacman's
`python` package on Arch, or the equivalent for your system — then retry.
`conda`/`mamba` environments work just as well if you prefer them:
`conda create -n relion_us python=3.11 && conda activate relion_us && pip
install -r backend/requirements.txt`.

Once the environment is set up and active, launch it with:

```bash
./run.sh               # binds 0.0.0.0:8420 by default
```

Then open `http://<that machine's address>:8420/` in a browser — including
from a different machine, since it binds `0.0.0.0`. On a remote server or
HPC cluster login node, launch it there and either port-forward over SSH
(`ssh -L 8420:localhost:8420 <host>`) or connect directly if your network
allows it. `./run.sh --help` shows the `--host`/`--port` options.

**You don't have to run it from inside a project directory.** If you `cd`
into an existing RELION project before running `./run.sh`, it's picked up
automatically; otherwise it starts in a default project folder and you
switch to the real one from the **Change Project** button in the top bar
at any time — see "Using it" below.

No CDN dependency: WinBox.js (the popup-window library) is vendored under
`frontend/vendor/` (Apache-2.0, see `WINBOX_LICENSE.txt`), specifically
because many HPC cluster login nodes and workstations have no outbound
internet access.

## Using it

- **Change Project** (top bar): switch which RELION project directory the
  app is pointed at, at any time, without restarting the backend. Type a
  path directly and hit Go/Enter, or click into subfolders in the browser
  below it — that browser lists folders on the *machine running the
  backend*, not your browser's machine, which matters when the backend is
  on a remote host like an HPC cluster login node. If the folder you pick
  doesn't look like a RELION project (no `default_pipeline.star` and not
  previously opened here), you're prompted to either start a new project
  there or pick a different folder — starting a new project never writes
  RELION's own pipeline file for you, only a small marker + history log;
  RELION's own tools still create `default_pipeline.star` correctly the
  first time a real job runs.
- **Job history bar** (below the empty-state message): every run started
  in the *current* project, newest first, reload-safe. Click a chip to
  reopen that run's command/status/live output — for a run from the
  current backend session this reconnects to the live stream; for one from
  a previous session (backend since restarted) it shows the saved status
  only, since the transcript itself isn't persisted, only the summary.
- **Jobs list** (left sidebar, `☰ Jobs` toggles it): every RELION job type
  grouped by category, plus a separate "(custom)" tag for the three import
  bridges. Click one to open it in its own popup — open as many at once as
  you want, each is independent.
- **Standard inputs**: RELION's own first GUI tab for that job (almost
  always "I/O") — extracted directly from `gui_jobwindow.cpp`, not guessed.
- **Advanced tab**: every other real RELION tab for that job (e.g. Class3D
  has Reference / CTF / Optimisation / Sampling / Helix / Compute),
  preserved as named groups.
- **Errors tab**: fills in live if the run writes to stderr; the tab badge
  shows a running error count.
- **RELION Source tab**: the *actual*, unmodified C++ `getCommands<Job>Job()`
  function for this job type, so you can check the draft/edited command
  against RELION's real logic by eye.
- **Command box**: pre-filled with a draft command (see below), fully
  editable. Click **Recompute draft** to regenerate it from the current
  form values (e.g. after changing a field), or just hand-edit it directly.
- **Run**: executes exactly the string in the command box (RELION jobs) or
  calls the converter directly (custom import jobs), streams output live
  via a websocket, and keeps the full transcript if you close and reopen.
- **Scale slider** (top right): zooms the whole UI — useful on a small
  laptop screen or a large shared display.

## How the draft command is built (read this before trusting it blindly)

For each active field, if a `--<field_key>` flag literally appears in that
job's real `getCommands<Job>Job()` source (extracted, not guessed), the
draft emits `--<field_key> <value>` (a bare flag for booleans, only when
true). This is correct for the large majority of RELION options, because
RELION's own convention is overwhelmingly "the flag is named after the
internal option key" — but it is **not** a full reimplementation of
RELION's C++ logic, which has real per-job branching (e.g. MotionCorr picks
between `relion_run_motioncorr` and the `_mpi` variant depending on
`nr_mpi`; `--float16` doesn't literally match its `do_float16` field key).
Fields that don't have a literal matching flag are left out of the draft
and listed in "unmapped fields" (hover the Recompute button's tooltip)
rather than guessed at — check the Advanced tab and the RELION Source tab
for those, and add them to the command box by hand if needed.

Three job types (DynaMight, ModelAngelo, External) don't hard-code a
binary at all — RELION runs whatever executable path you set in a
"Location of X executable" field. The draft command resolves that
automatically from the field's current value.

## The three custom import jobs

`Import from IMOD (.mod)`, `Import from Warp/M`, and `Import from
DeepETPicker` live in `backend/converters/` and use the same popup layout,
live output, and Errors tab as every RELION job — they just don't have a
command box, since they call directly into Python rather than spawning a
subprocess. Status of each, so you know what to double-check before
trusting the output:

- **IMOD bridge**: fully implemented and tested. The `.mod` <-> coordinate
  functions need `point2model`/`model2point` on PATH (`module load imod`
  on the cluster); everything else (`.xf`/`.tlt` I/O) is unit-tested
  directly and doesn't need IMOD installed.
- **Warp/M bridge**: the column-diffing and mapping machinery is
  implemented and tested, but `DEFAULT_COLUMN_MAP` is intentionally empty
  — recent Warp/M versions are moving toward RELION-5's own STAR
  conventions, so you may need little to no renaming, but column names
  weren't hard-coded without verification against a real file. Send a real
  `.tomostar` or particle STAR export and the mapping can be filled in and
  verified.
- **DeepETPicker bridge**: verified against the DeepETPicker README
  (`.coords` = `class_id x y z`, voxels) and fully implemented/tested.
  DeepETPicker also ships its own `coords_to_relion4.py` — prefer that
  directly for a one-off conversion; this module is for wiring `.coords`
  -> particles.star into RELION-US's Jobs list/live-output flow.

## SLURM templates (any cluster)

`slurm/template_relion_job.sbatch`, `slurm/template_python_job.sbatch`, and
`slurm/submit.py` are a standalone command-line path for running a RELION
job or a converter as a proper batch job — **not yet wired into the job
popups themselves** (v1 scope: direct subprocess execution only, an
explicit choice, see `docs/ARCHITECTURE.md`'s Open follow-ups for what
adding a "Run on cluster" button would look like). These templates are
intentionally generic, not written for any specific site: partition names,
account/allocation syntax, and module names all vary between clusters, so
`ACCOUNT_NAME`/`PARTITION_NAME` are placeholders you fill in for your own
system — run `sinfo` for partition names and `module spider relion` /
`module spider imod` (or check however your cluster exposes software) for
the exact module strings on your install.

## Provenance and re-running the extraction

Every job's fields, defaults, help text, and real C++ command logic come
from `data/extract_job_definitions.py`, which parses a real RELION
checkout (`github.com/3dem/relion`, cloned 2026-08-14) rather than being
hand-typed. To regenerate `data/job_definitions_raw.json` against a newer
RELION version:

```bash
git clone --depth 1 https://github.com/3dem/relion.git /tmp/relion_src
python3 data/extract_job_definitions.py \
    /tmp/relion_src/src/pipeline_jobs.cpp \
    /tmp/relion_src/src/pipeline_jobs.h \
    /tmp/relion_src/src/gui_jobwindow.cpp \
    data/job_definitions_raw.json
```

Then re-run the test suite (`cd backend && python3 -m pytest -v`) — it's
written as a regression suite against the *real* extracted data (not
synthetic fixtures), so it will catch new parsing gaps the same way it
caught the bugs listed below during development.

## Known limitations / what to double check

- The draft-command heuristic (above) is best-effort, not a guaranteed
  match for RELION's exact branching logic — always review before running,
  which is the whole point of the editable box.
- `External`'s "Params" tab exposes RELION's generic
  `param1_label`/`param1_value` ... `param10_label`/`param10_value` slots
  verbatim (that's genuinely how RELION's own External job works — you
  name your own flags there).
- Warp/M column names in the custom import job are unverified against your
  specific install (see "The three custom import jobs" above).
- This extraction pipeline was built and tested against one specific
  RELION checkout; job internals do change across releases, so re-running
  the extractor (above) periodically is worth doing, and the test suite
  will flag most breakage immediately.
- Job history persists run *summaries* (command, status, timestamps) per
  project, not full stdout/stderr transcripts — reopening a job from
  history after the backend has restarted shows its last known status but
  not its old live output. Runs from the current backend session stream
  normally either way.
- No SLURM integration in the job popups yet (see "SLURM templates" above)
  — direct subprocess execution only, by explicit choice for this version.

## What was caught by the test suite during development

`backend/tests/` (137 tests: 136 passing + 1 auto-skipped when IMOD's
`point2model`/`model2point` aren't on PATH, which they won't be until
`module load imod` on the cluster) is a regression suite against *real*
extracted RELION data and real converter behavior, not synthetic fixtures
— it caught five real bugs before they shipped:

1. `std::string("")`-style default values leaking raw C++ into a draft
   command (`test_no_leftover_cpp_syntax_in_draft_or_defaults`).
2. Five tomography jobs (TomoSubtomo, TomoReconPart, TomoAlign,
   TomoCtfRefine, TomoAlignTiltSeries) losing their entire "standard" tab
   because their fields are added via a shared `addTomoInputOptions()` /
   `placeTomoInput()` helper rather than inline — fixed by expanding those
   helper calls using their own real definitions
   (`test_standard_and_advanced_fields_are_disjoint_and_known`).
3. Three jobs (DynaMight, ModelAngelo, External) with no hard-coded
   binary — RELION runs a user-configured executable path instead — fixed
   by resolving a `{joboptions.<key>}` placeholder against real field
   values (`test_executable_path_placeholder_resolves_from_field_values`).
4. `POST /api/project/switch` initially 404'd on a project path that
   doesn't exist yet, instead of routing to the same "start new project /
   pick different folder" prompt used for an existing-but-not-a-project
   folder — caught by the Playwright Change Project smoke test, fixed by
   treating "doesn't exist" and "exists but isn't a project" the same way.
5. (Carried over from the standalone converter test suite, still enforced
   here) IMOD `.mod` round-trip, Warp/M column diffing, and DeepETPicker
   `.coords` parsing edge cases — see `backend/tests/test_imod_bridge.py`,
   `test_warp_bridge.py`, `test_deepetpicker_bridge.py`.
