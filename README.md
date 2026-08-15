# RELION-US

**RELION - User Supported Frontend** — a browser-based, portable job
manager for RELION, built as a *companion* to RELION, not a modified
RELION GUI. It
reads RELION's own source (`pipeline_jobs.cpp`/`.h`, `gui_jobwindow.cpp`)
to build accurate forms for every RELION job type (32 of them, single-
particle and tomography), folds in IMOD/Warp-M/DeepETPicker/AreTomo2 import
bridges as four more entries in the same Jobs list, and runs everything
through one consistent popup-window UI: standard inputs on top, an Advanced
tab with every other option, an Errors tab, live streaming output at the
bottom — and, critically, **an editable command box you approve before
anything runs.**

The main panel is a **Command Center** showing every job you've run (a
sortable table or a timeline that links each job to its inputs); iterative
jobs get a **live Progress tab** with charts and class images; and the top
bar has a **🔍 Visualize** button that opens a tomogram / particle-pick
viewer plus a **dark/light theme** switch. Jobs run **from the project
directory**, exactly like RELION, so project-root-relative paths behave the
way RELION's own GUI expects.

## Why this exists

RELION's own GUI is a compiled Qt5/C++ application that assembles each
job's command internally and hands it straight to the shell. RELION-US puts
that command in front of you first and lets you edit it: whatever is in the
command textbox when you click Run is executed exactly as written, via the
shell, nothing added or removed. The draft command that pre-fills is based
on the standard inputs suggested (see "How the draft command is built"
below) — always check it, and the job's real RELION C++ source is one tab
away for cross-referencing. It's also built to work between projects in
separate project directories without closing down. You can change working
directories on the fly and the application will reparse the environment.
You need to be vigilant about your own resources and what you are working
on. This is pre-beta software, so use at your own risk, float your own
fixes, and let's build a user interface by users for users.

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
python3 -m venv venv
source venv/bin/activate
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
- **Command Center** (main panel): every run started in the *current*
  project, reload-safe, as a sortable table or a linked timeline — see
  "Command Center" below. Click a row or card to reopen that run's
  options/status/outputs — for a run from the current backend session this
  reconnects to the live stream; for one from a previous session (backend
  since restarted) it shows the saved status and its output files, since the
  live transcript itself isn't persisted, only the summary.
- **Jobs list** (left sidebar, `☰ Jobs` toggles it): every RELION job type
  grouped by category, plus a separate "(custom)" tag for the four import
  bridges. Click one to open it in its own popup — open as many at once as
  you want, each is independent.
- **Standard inputs**: RELION's own first GUI tab for that job (almost
  always "I/O") — extracted directly from `gui_jobwindow.cpp`, not guessed.
- **Advanced tab**: every other real RELION tab for that job (e.g. Class3D
  has Reference / CTF / Optimisation / Sampling / Helix / Compute),
  preserved as named groups.
- **Progress tab** (iterative jobs only): live charts of resolution and class
  distribution plus class images, with per-job controls for how often images
  refresh and whether earlier iterations are kept — see "Live progress for
  iterative jobs" below.
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
- **Theme switch** (top bar): dark (the default) or light; your choice is
  remembered.
- **Scale slider** (top right): zooms the whole UI — useful on a small
  laptop screen or a large shared display.

## How the draft command is built

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

## The four custom import jobs

`Import from IMOD (.mod)`, `Import from Warp/M`, `Import from
DeepETPicker`, and `Import from AreTomo2 (.aln)` live in
`backend/converters/` and use the same popup layout,
live output, and Errors tab as every RELION job — they just don't have a
command box, since they call directly into Python rather than spawning a
subprocess. Status of each, so you know what to double-check before
trusting the output:

- **IMOD bridge**: fully implemented and tested. The `.mod` <-> coordinate
  functions need `point2model`/`model2point` on PATH (`module load imod`
  on the cluster); everything else (`.xf`/`.tlt` I/O) is unit-tested
  directly and doesn't need IMOD installed.
- **Warp/M bridge**: the column-diffing and mapping machinery is
  implemented and tested, but `DEFAULT_COLUMN_MAP` is intentionally empty.
  Warp 2.0's `ts_export_particles` already writes a RELION-5 optimisation
  set with native `rln*` columns, so that output needs no renaming at all;
  `.tomostar` and older particle exports use Warp's `wrp*` columns and do
  need a mapping. Rather than guess at column names that have changed
  across Warp versions, run the job once to see the column diff, then fill
  in `DEFAULT_COLUMN_MAP` for your version.
- **DeepETPicker bridge**: verified against the DeepETPicker README *and*
  its own `utils/coords_to_relion4.py` (`.coords` = `class_id x y z` in
  voxels; a bare 3-column `x y z` file is also accepted, matching what
  DeepETPicker itself accepts). Fully implemented/tested. DeepETPicker also
  ships that converter — prefer it directly for a one-off conversion; this
  module is for wiring `.coords` -> particles.star into RELION-US's Jobs
  list/live-output flow.
- **AreTomo2 bridge**: reads AreTomo2's `.aln` global alignment block
  (`SEC ROT GMAG TX TY SMEAN SFIT SCALE BASE TILT`, verified against the
  AreTomo manual and the teamtomo/alnfile parser) and writes IMOD-style
  `.xf` + `.tlt`, which RELION-5's IMOD tilt-series import reads. It
  deliberately hands off through IMOD files rather than writing RELION's
  tilt-series STAR directly: the `.xf` mapping is independently corroborated
  by AreTomo's own `-OutImod` export, which makes it the better-verified
  route into RELION. Dark (excluded) frames are reported. `TX`/`TY` are
  in pixels of the aligned stack — the `.aln` records no pixel size, so
  supply it downstream. If you still have AreTomo's own `-OutImod` output,
  prefer it; validate against a real `-OutImod` `.xf` if exactness matters.

**Coordinate flips.** The IMOD and DeepETPicker importers have
`Swap Y and Z` and per-axis `Mirror` options (`backend/converters/
coord_transform.py`). The Y/Z swap is the fix for IMOD's "flipped"
(`trimvol -yz`) vs "rotated" (`trimvol -rx`) tomogram convention — a model
built on a flipped or raw-`tilt` volume has depth in Y, not Z. Mirroring
requires the tomogram dimension for that axis, and fails loudly if you
don't supply it rather than silently producing wrong coordinates.

## Command Center (job history)

The main panel lists every job run in the current project, in two togglable
views: a **table** (sortable by job name/number, type, status, or start
time) and a **timeline** (newest-first or oldest-first, with a card per job
that links to the jobs its inputs came from). Clicking a job reopens its
popup showing the options it ran with, its live or final status, an
**Outputs** tab (browse/download individual files or a `.zip` of any
selection), the **Errors** tab, and the **RELION Source** tab.

The toolbar in each popup mirrors RELION's own "Job actions" menu: collapse,
close, rename (RELION's *Alias*), edit note, **Overwrite** (re-runs into the
same job directory and job number, so it stays one entry — matching how
RELION reuses a pipeline job slot), **Abort** (kills the whole process
group, not just the shell), Mark finished / Mark failed, **Delete**, and
**Clean** / **Harsh Clean**. Clean is a *review* flow, not a silent sweep:
it lists every file with its size, pre-checks a suggestion, and deletes only
what you confirm. (RELION's own cleanup uses per-job-type glob patterns
defined in its C++ source; this uses its own review-based suggestion instead
of mirroring them.)

## Tomogram / particle-pick viewer

The **🔍 Visualize** button opens a viewer — it is *not* a job, so it never
appears in the Command Center and writes nothing. Give it an optimiser
STAR, a `tomograms.star`, or an MRC (with or without a particles/coords
STAR) — type a path or hit **Browse…** — and it loads one tomogram at a
time:

- browse slices along **XY / XZ / YZ**, with black/white-point contrast
  sliders (default is a robust 0.5–99.5% percentile, since raw cryo-ET
  min/max is usually washed out);
- picks are overlaid using DeepETPicker's own model — a particle is drawn on
  every slice within ±(diameter/2) of its centre, with radius
  `sqrt(r² − Δ²)` so the marker grows toward the particle's centre
  slice — with diameter and line-width controls;
- if the tomogram's name doesn't match any `rlnTomoName` in the picks file,
  you get a warning with **Load anyway / Reload files / Cancel**.

Both inputs have a **Browse…** button. It lists files on the *machine
running the backend*, not your own — which is the point when the backend is
on a cluster login node and a native file dialog would show you the wrong
filesystem. It filters to the relevant extensions (STAR/MRC for the tomogram
field, STAR only for the picks field), remembers the folder you were last
in, and fills the field with a project-relative path.

The volume is never loaded whole: the backend memory-maps the MRC and
returns one slice at a time as a PNG, so scrubbing stays fast on large
tomograms. This needs `mrcfile` and `pillow` (both in
`backend/requirements.txt`).

## Live progress for iterative jobs

Classification and refinement runs take a long time and report every few
iterations, so those jobs get a **Progress** tab next to Outputs/Errors that
plots that report as it arrives. It covers **Class2D, Class3D,
Refine3D, 3D initial model, MultiBody, and tomo Reconstruct Particle**;
jobs with nothing to plot (Import, MaskCreate, the converters) simply don't
show the tab.

What you get, updated while the job runs:

- **Resolution by iteration** — a line for the current resolution and one for
  the best class, both in Å on one axis, with the latest value labelled.
- **Particles per class** — a bar per class for the newest iteration.
- **Class images** — 2D class averages, or the central slice of each 3D class
  volume, captioned with class number, share of particles, and resolution.

It reads the files RELION already writes each iteration
(`run_it###_model.star`, `run_it###_classes.mrcs` / `run_it###_class###.mrc`),
so there's nothing to configure and nothing extra on disk.

**Keeping it cheap.** Every control is per job, in the tab itself:

- **Live progress** (on by default) — untick it and the job stops being polled
  at all.
- **Images every N iterations** — class images only refresh on multiples of N
  (1 = every iteration). The charts still update every iteration; they're
  nearly free.
- **Keep all** (off by default) — on, earlier iterations' images are kept so
  you can compare how classes evolved. Off, only the newest set is held, so
  memory stays flat however long the run goes.

Under the hood nothing is cached to disk, thumbnails are 128 px greyscale
rendered on demand, and polling only happens while the job is actually
running.

## Dark and light themes

The top bar has a theme switch. **Dark is the default**; pick light and it's
remembered. The charts use separate, mode-specific colours validated against
each background rather than a naive inversion, so they stay legible either
way.

## SLURM templates (any cluster)

`slurm/template_relion_job.sbatch`, `slurm/template_python_job.sbatch`, and
`slurm/submit.py` are a standalone command-line path for running a RELION
job or a converter as a proper batch job — **not yet wired into the job
popups themselves**; see `docs/ARCHITECTURE.md` for what adding a "Run on
cluster" button would involve. These templates are
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

Then re-run the test suite (`cd backend && python3 -m pytest -v`). It runs
against the *real* extracted data rather than synthetic fixtures, so
parsing gaps introduced by a new RELION release show up as failures.

## Known limitations / what to double check

- The draft-command heuristic (above) is best-effort, not a guaranteed
  match for RELION's exact branching logic — always review before running,
  which is the whole point of the editable box.
- `External`'s "Params" tab exposes RELION's generic
  `param1_label`/`param1_value` ... `param10_label`/`param10_value` slots
  verbatim, which is how RELION's own External job works — you name your
  own flags there.
- Warp/M column names in the custom import job are unverified against your
  specific install (see "The four custom import jobs" above).
- The job definitions come from one specific RELION checkout. Job internals
  change across releases, so re-run the extractor (above) after a RELION
  upgrade; the test suite flags most breakage immediately.
- Job history persists run *summaries* (command, status, timestamps) per
  project, not full stdout/stderr transcripts — reopening a job from
  history after the backend has restarted shows its last known status but
  not its old live output. Runs from the current backend session stream
  normally either way.
- No SLURM integration in the job popups yet (see "SLURM templates" above)
  — jobs run as direct subprocesses.

## Testing

```bash
cd backend && python3 -m pytest -q          # backend unit + regression tests
```

The backend suite runs against real extracted RELION data and real
converter behaviour rather than synthetic fixtures, so a change in RELION's
job definitions or a regression in a format bridge shows up as a failure.
One test auto-skips unless IMOD's `point2model`/`model2point` are on PATH
(the `.mod` round-trip).

Browser tests use Playwright against a running instance and an empty
project:

```bash
./run.sh &                                   # or point them at an existing instance
python3 test_frontend.py                     # job list, popups, SPA/Tomo toggle
python3 test_frontend_project.py             # Change Project, Create Folder
python3 test_command_center.py               # history table/timeline, Outputs tab
python3 test_command_center_abort_overwrite.py
python3 test_progress_and_theme.py           # Progress tab, theme, file pickers
```

Point them at a different host/port with a first argument, e.g.
`python3 test_command_center.py http://127.0.0.1:8420`.
