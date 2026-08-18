# RELION-US

**RELION - User Supported Frontend** — a browser-based, portable job
manager for RELION, built as a *companion* to RELION, not a modified
RELION GUI. It
reads RELION's own source (`pipeline_jobs.cpp`/`.h`, `gui_jobwindow.cpp`)
to build accurate forms for every RELION job type (32 of them, single-
particle and tomography), folds in IMOD/Warp-M/DeepETPicker/AreTomo2 import
bridges as four more entries in the same Jobs list, and runs everything
through one consistent popup-window UI: every option RELION's own GUI shows
in the top panel, an Advanced tab for the command-line options it doesn't, an
Errors tab, live streaming output at the bottom — and, critically, **an
editable command box you approve before anything runs.**

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
  app is pointed at, at any time, without restarting the backend. Every
  project you open or create is remembered, so the dialog opens with a
  **Recent projects** list — one click browses to a project, a double-click
  switches straight to it, and the ✕ drops it from the list (the folder
  itself is never touched). A project that has since been deleted stays
  listed but struck through, rather than quietly disappearing. Otherwise:
  type a path directly and hit Go/Enter, or click into subfolders in the
  browser below it — that browser lists folders on the *machine running the
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
  bridges. Click one to open it in its own popup — nearly window-filling
  with rounded corners, and only one open at a time (opening a different
  job closes whichever popup was already open, rather than stacking
  windows).
- **Top panel**: **every option RELION's own GUI shows for that job**, in
  RELION's own groups and order (I/O, Reference, CTF, Optimisation, Sampling,
  Helix, Compute, Running), as collapsible sections — extracted directly from
  `gui_jobwindow.cpp` and `pipeline_jobs.cpp`, not guessed. The I/O section
  starts open; the rest are one click away. Nothing RELION shows is hidden
  behind a tab.
- **Running section**: MPI procs, threads, and **Additional arguments** —
  RELION's own Running tab. Setting MPI procs above 1 does exactly what RELION
  does: prefixes `$RELION_MPIRUN -n N` (default `mpirun`) and switches to that
  job's `_mpi` binary, with both binary names read out of the job's own C++
  source rather than guessed by appending a suffix. Additional arguments are
  appended verbatim at the end, as RELION appends them.
- **Advanced tab**: the opposite of the top panel — command-line options the
  program accepts that **RELION's GUI never exposes**, the ones you would
  otherwise find by running the binary with `--help` or reading the source.
  The list comes from asking your installed binary (see "The Advanced tab"
  below), so it matches your build.
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
rather than guessed at — check the RELION Source tab for those, and add them
to the command box by hand if needed.

Where a flag isn't simply `--` + the option key, the pairing is read out of the
job's own builder too (`command += " --i " + joboptions["input_star_mics"]...`),
so `--i`, `--Box`, `--j` and ~80 others are drafted correctly rather than
reported as unmapped. Pairings that RELION only emits inside a branch depending
on a *different* option — Autopick's `--particle_diameter` in Topaz mode versus
`--LoG_diam_min` in LoG mode — are deliberately left out: emitting both would
produce a command that contradicts itself.

Three job types (DynaMight, ModelAngelo, External) don't hard-code a
binary at all — RELION runs whatever executable path you set in a
"Location of X executable" field. The draft command resolves that
automatically from the field's current value.

## The Advanced tab (options the GUI doesn't show)

RELION's GUI exposes a subset of what each program actually accepts. The rest —
expert and developmental flags — are what its "Additional arguments" box exists
for, and finding them normally means running the binary with no arguments and
reading the usage dump.

The Advanced tab does that for you. On opening a job it runs the job's program
with `--help`, parses RELION's own usage format, subtracts every flag the form
above already covers, and lists what's left with its default, its section, and
its help text. Filter the list, fill in a value, and **Add** appends it to the
command box — where you can still edit or delete it, like everything else here.

Three things worth knowing:

- It asks **your installed binary**, so the list reflects your RELION build,
  including local patches — not whichever checkout the job definitions came
  from. If MPI procs is above 1 it asks the `_mpi` binary, which can accept
  flags the serial one doesn't.
- If the program isn't on the backend's PATH, the tab says so plainly instead
  of showing an empty list. You can still type anything into the command box or
  into Additional arguments.
- RELION-5's Python tomo tools are Typer-based and don't print RELION's usage
  format. Rather than guess at a format it doesn't understand, the tab shows
  their raw `--help` output as-is.

Each program's help is read once per backend session and cached on the
binary's path, size and modification time, so rebuilding RELION or switching
versions picks up the new options without a restart.


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

## Opening a project built in RELION's GUI

Point RELION-US at an existing project and it reads RELION's own
`default_pipeline.star` (never writes it):

- **Its jobs fill the Command Center**, tagged `RELION`, with RELION's own job
  numbers, aliases, types and statuses (Succeeded/Failed/Running map onto the
  same states this app uses). They carry no timestamps — RELION's pipeline file
  records none, and a directory's mtime is not a start time.
- **Opening one shows the settings it actually ran with**, read from that job's
  own `job.star` — the same file RELION's GUI reads to reopen a job. A job from
  RELION 3.0 or earlier (which wrote `run.job` in a different format) opens with
  the job type's defaults and says so.
- **Its Outputs and Progress tabs work.** An old classification's
  `run_it###_model.star` files are still there, so you get its resolution curve
  and class images without re-running anything.
- **New jobs continue the project's numbering.** RELION-US takes
  `rlnPipeLineJobCounter` and the existing process list into account, and skips
  any number whose directory is already on disk. In a project sitting at job011
  your next job is job012 — not job001 on top of somebody's Import.

By default it does **not** register its own runs back into
`default_pipeline.star` — that file is RELION's own state, and writing it
incorrectly would damage a project this app is only a companion to. The two
tools then keep separate records: jobs you run here won't show in RELION's
GUI, and RELION's counter won't know about them. Turn on **⇄ RELION sync**
(below) if you want to switch between the two GUIs on the same project.

## ⇄ RELION sync (switching between the two GUIs)

Click **⇄ RELION sync** in the top bar to turn on two-way sync for the
current project. It's off by default, and it's a per-project setting, not a
global one — the button is hidden entirely if `relion_pipeliner` isn't on
this app's `PATH`.

With it on, every job you run here is also registered in
`default_pipeline.star`, so it shows up in RELION's own GUI too:

- RELION-US still never writes `default_pipeline.star` itself. It writes the
  job's settings to a `job.star` and hands that to RELION's own
  `relion_pipeliner --addJobFromStar`, the same binary RELION's GUI would use
  in your place. That binary decides the job number, creates the job
  directory, works out the input/output node graph, and appends the process
  to the pipeline — RELION-US only reads the result back.
- The run then executes in the directory RELION allocated (renumbering the
  draft command's `--o` if RELION picked a different slot than the one this
  app proposed — this can happen if RELION's own GUI created a job in
  between), with `--pipeline_control <job_dir>/` appended so the running
  program reports its own completion the way RELION expects.
- When the job finishes, RELION-US calls
  `relion_pipeliner --check_job_completion` so the process's status in
  `default_pipeline.star` (Succeeded/Failed/Aborted) is updated immediately,
  instead of waiting for RELION's GUI to notice on its own.
- If `relion_pipeliner` is busy — RELION's own GUI is mid-write and holding
  the project's `.relion_lock` — registration waits (up to two minutes) for
  the lock rather than skipping it. If it still can't register or can't
  confirm completion, the run itself is unaffected; a note in that job's
  output log says so and tells you to run
  `relion_pipeliner --check_job_completion` yourself, or just open RELION's
  GUI, to catch the pipeline file up.
- Jobs already run here before you turned sync on are **not** added
  retrospectively — sync only covers what happens from that point on.
- Jobs RELION's own GUI already treats as read-only here (see above) are
  unaffected either way; sync only changes what happens to jobs *you start in
  RELION-US*.

## Command Center (job history)

The main panel lists every job run in the current project, in three
togglable views: a **table** (sortable by job name/number, type, status, or
start time), a **timeline** (newest-first or oldest-first, with a card per
job that links to the jobs its inputs came from), and a **network** view —
a lineage graph, oldest jobs at the top, with every job that used another
job's output drawn directly beneath it and connected by a branch line. A job
whose output fed two later jobs (say job010 feeding both job011 and job012)
shows job010 with two branches down to job011 and job012 side by side. For a
project built in RELION's own GUI, this lineage isn't guessed from file
paths — it's read straight from `default_pipeline.star`'s own
`pipeline_input_edges`/`pipeline_output_edges` tables, the graph RELION
itself computed when each job ran, so the network view (and the timeline's
"Inputs from:" chips) work identically whether a job ran here or in RELION.

Clicking a job reopens its popup — nearly window-filling, rounded corners,
and only one open at a time (opening a new one closes whichever was open,
rather than stacking windows) — showing the options it ran with, its live or
final status, an **Outputs** tab (browse/download individual files or a
`.zip` of any selection), the **Errors** tab, and the **RELION Source** tab.

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
STAR) — type a path or hit the **…** browse button — and it loads one
tomogram at a time.

**Three linked orthogonal views**, laid out the way DeepETPicker's picker is:
**XY** is the large main view, **ZY** sits to its left, **XZ** below it. All
three are cuts through one crosshair position, so:

- **click** (or click-drag) in any view to move the crosshair — the other two
  jump to that point;
- **scroll** over a view to step along its own axis (scroll the main view to
  walk through Z); hold **Shift** for 10-slice steps;
- or drive X/Y/Z directly with the sliders in the side panel.

The three panels share one isotropic scale, so a voxel is the same size in
each and the crosshair lines up across the panel borders — the side views are
as tall/wide as the volume actually is, not stretched to fill a box.

Everything else lives in a narrow rail on the right so the images get the
window: the two file inputs, black/white-point contrast sliders (default is a
robust 0.5–99.5% percentile, since raw cryo-ET min/max is usually washed
out), pick diameter and line width, and toggles for the pick overlay and the
crosshair.

Picks are overlaid using DeepETPicker's own model — a particle is drawn on
every slice within ±(diameter/2) of its centre, with radius `sqrt(r² − Δ²)`
so the marker grows toward the particle's centre slice — in all three views
at once. If the tomogram's name doesn't match any `rlnTomoName` in the picks
file, you get a warning with **Load anyway / Reload files / Cancel**.

Both inputs have a **…** browse button. It lists files on the *machine
running the backend*, not your own — which is the point when the backend is
on a cluster login node and a native file dialog would show you the wrong
filesystem. It filters to the relevant extensions (STAR/MRC for the tomogram
field, STAR only for the picks field), remembers the folder you were last
in, and fills the field with a project-relative path.

The volume is never loaded whole: the backend memory-maps the MRC and
returns one slice at a time as a PNG, and only the panels whose own slice
index moved are refetched — clicking in XY changes the ZY and XZ cuts but not
XY's own. This needs `mrcfile` and `pillow` (both in
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

The top bar itself is a fixed blue (`#134394`) in both themes rather than a
theme-swapped color — everything on it (buttons, the project path label, the
zoom control) still adjusts for legible contrast against that blue in either
mode.

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
- Jobs RELION itself ran are **read-only** here (see "Opening a project built
  in RELION's GUI"): abort, overwrite, rename, note and delete are refused on
  them, because RELION-US doesn't write `default_pipeline.star` and couldn't
  keep RELION's record straight afterwards.
- By default, RELION-US's own runs do **not** appear in RELION's pipeline
  file, so RELION's GUI won't list them, and RELION's own counter won't know
  about them — check the job number if you go back and forth between the two.
  Turn on **⇄ RELION sync** (above) to register runs into
  `default_pipeline.star` as they happen instead.
- Sync depends on `relion_pipeliner` being on RELION-US's `PATH` and, per
  registration, on the project's `.relion_lock` being free within about two
  minutes — if RELION's own GUI is mid-operation on the same project, a
  registration can wait that long before the run starts.
- Job history persists run *summaries* (command, status, timestamps) per
  project, not full stdout/stderr transcripts — reopening a job from
  history after the backend has restarted shows its last known status but
  not its old live output. Runs from the current backend session stream
  normally either way.
- No SLURM integration in the job popups yet (see "SLURM templates" above)
  — jobs run as direct subprocesses.

## Testing

```bash
./run_tests.sh              # backend suite only — seconds, run it always
./run_tests.sh viewer       # + the tomogram viewer / recent-projects suite
./run_tests.sh progress     # + the Progress tab / theme / file-picker suite
./run_tests.sh options      # + option placement, MPI/threads, Advanced tab
./run_tests.sh jobs         # + job popups, Command Center, abort/overwrite
./run_tests.sh project      # + Change Project, recents, Create Folder
./run_tests.sh legacy       # + opening a project built in RELION's own GUI
./run_tests.sh all          # everything (~80 s) — before you commit a milestone
```

The browser suites are tiered because each one needs its own backend on its own
throwaway project, and running all of them to check a change that touched one
module costs minutes and tells you nothing. Pick the tier that covers what you
changed; `run_tests.sh`'s header comment has the mapping. `all` is for a real
checkpoint, or for a change to something shared like the popup scaffolding in
`app.js`.

The runner creates a fresh project directory and picks a free port per suite,
waits for each backend to answer before starting, and tears everything down
afterwards — including on Ctrl-C. It also redirects `XDG_CONFIG_HOME`, so a
test run never touches your real recent-projects list. Nothing is left running
and no project of yours is touched: a suite that asserts "no jobs yet" would
fail against a project that has history, which is a false alarm rather than a
bug.

The backend suite runs against real extracted RELION data and real converter
behaviour rather than synthetic fixtures, so a change in RELION's job
definitions or a regression in a format bridge shows up as a failure. One test
auto-skips unless IMOD's `point2model`/`model2point` are on PATH (the `.mod`
round-trip).

To run one suite by hand, point it at any live instance — each takes a base URL
(and the two that write fixtures also take a project directory):

```bash
python3 test_command_center.py http://127.0.0.1:8420
python3 test_viewer_and_recents.py http://127.0.0.1:8420 /path/to/empty/project
```

Set `RELION_US_CHROMIUM` if Playwright can't find a usable Chromium itself
(a shared read-only install on a cluster, say); otherwise `playwright install
chromium` is all that's needed.

## License

RELION-US is released under the **GNU General Public License, version 2 or
later** (`LICENSE`).

That license follows the material this repository redistributes:
`data/job_definitions_raw.json` embeds the verbatim `getCommands<Job>Job()`
C++ source and the field defaults and help strings for all 32 job types,
extracted from RELION (© MRC Laboratory of Molecular Biology, GPL-2.0-or-later)
— the same data the RELION Source tab shows you. `frontend/vendor/` bundles
WinBox.js under Apache-2.0.

`NOTICE.md` has the full attribution, what was taken from where, and the
third-party format/dependency situation. RELION-US is an independent project,
not endorsed by or affiliated with the RELION authors or the developers of any
other software named here.
