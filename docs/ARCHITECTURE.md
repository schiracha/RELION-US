# RELION-US — architecture & scope

RELION-US ("RELION - User Sourced") is a browser-based companion to
RELION — not a fork or patch of RELION itself, and not a wrapper around
RELION's own compiled GUI. It's a separate front end that reads RELION's
own source to build accurate job forms, then drives RELION's real
command-line programs as subprocesses, with format-conversion bridges for
IMOD, Warp/M, DeepETPicker, and AreTomo2 folded in as four more entries in the same
Jobs list.

## Why a companion tool instead of patching RELION itself

RELION's own GUI (`relion`, built from `src/apps/maingui.cpp` and the
`gui_*` sources in [3dem/relion](https://github.com/3dem/relion)) is a
monolithic Qt5 C++ application compiled together with the whole processing
engine. Patching it directly means rebuilding the entire RELION binary
(CMake + Qt5 + FFTW + CUDA toolchain) every time you want to test a change,
re-merging your patch by hand on every upstream release, and your changes
only running wherever you've built that exact binary — which fights the
"portable" goal directly.

Instead, RELION-US is a **separate, lightweight layer that sits next to a
normal RELION install**: it reads and writes the same STAR files RELION
uses (RELION's actual interchange format — the GUI itself is just a job
scheduler over STAR files and `relion_*` command-line programs), and drives
`relion_*` binaries as subprocesses, exactly as the real GUI would. That
gets a friendlier, more visual, portable front end without touching
upstream code, zero merge conflicts with upstream RELION releases, and
something that runs identically on a laptop and, launched as a job, on
any SLURM cluster.

**Concretely, at runtime**: RELION-US never runs RELION's own GUI binary.
`backend/data/extract_job_definitions.py` reads RELION's GUI source
(`gui_jobwindow.cpp`) and pipeline source (`pipeline_jobs.cpp`/`.h`) as
*text*, once, at build/extraction time, to get every job's real field
names, defaults, help text, and command-assembly logic — those files are
never compiled or executed. What actually runs, when you click Run in a
job popup, is the same `relion_*` command-line program the real GUI would
have called, via `asyncio.create_subprocess_shell` (see
`backend/job_runner.py`) with the exact command string you approved in the
editable command box — nothing added, removed, or silently rewritten.

## What already exists — don't reinvent these

- **[`starfile`](https://pypi.org/project/starfile/)** (PyPI) — a mature
  Python library for reading/writing RELION-style STAR files (including the
  multi-block "optimisation set" files RELION-5's tomography pipeline uses)
  into pandas DataFrames. Written by Alister Burt, who is also a co-author
  on the RELION-5 tomography paper (see References). `backend/converters/
  star_io.py` wraps it rather than writing a STAR parser from scratch.
- **IMOD command-line tools** (`point2model`, `model2point`, `imodtrans`,
  etc., part of the IMOD package) — used as subprocesses for anything
  involving `.mod` files, rather than reimplementing IMOD's binary model
  format.
- **RELION's own source**, read as ground truth by the extractor (see
  below) rather than hand-typed field lists — the whole point is to avoid
  the exact class of bug ("RELION inserted a flag I can't see or override")
  that motivated this project in the first place.

## Components

```
relion_us/
├── backend/
│   ├── main.py              # FastAPI app: REST + one websocket per job run
│   ├── job_registry.py      # raw extraction -> API-ready job definitions,
│   │                        #   standard/advanced field split, draft-command
│   │                        #   heuristic (see "Draft command" below)
│   ├── job_catalog.py       # curated display metadata (names, categories)
│   ├── job_runner.py        # executes the approved command exactly as given;
│   │                        #   per-project run history persistence
│   ├── project_manager.py   # RELION-project detection, project switching,
│   │                        #   history load/save (see "Change Project" below)
│   ├── custom_jobs.py       # wires the 4 converters in as Job types
│   ├── viz.py               # tomogram/pick VIEWER (not a job): mrcfile mmap ->
│   │                        #   PNG slices + pick JSON (see "Visualizer" below)
│   ├── progress.py          # live per-iteration charts + class thumbnails for
│   │                        #   iterative jobs (see "Live job progress" below)
│   ├── converters/          # pure-Python + subprocess format bridges
│   │   ├── star_io.py           # thin wrapper over `starfile`, RELION-5 tomo aware
│   │   ├── coord_transform.py   # shared axis swap/mirror for coordinate importers
│   │   ├── imod_bridge.py       # IMOD .xf/.tlt/.mod <-> RELION tilt-series STAR
│   │   ├── warp_bridge.py       # Warp/M metadata <-> RELION-5 STAR
│   │   ├── deepetpicker_bridge.py  # DeepETPicker coordinates -> particles.star
│   │   └── aretomo_bridge.py    # AreTomo2 .aln -> IMOD .xf/.tlt for RELION import
│   └── tests/                # pytest: job_registry regression suite (against
│                              #   real extracted data) + converter unit tests
├── frontend/                 # vanilla JS/HTML/CSS, no build step; WinBox.js
│                              #   (vendored, not CDN-loaded) for popup windows
├── data/
│   └── extract_job_definitions.py  # parses real RELION source -> job_definitions_raw.json
├── slurm/                    # generic sbatch templates + submit.py, any SLURM cluster;
│                              #   not yet wired into the job popups, see below
├── docs/                     # this file
├── run.sh                    # launch helper (no install script -- see README.md
│                              #   for building the Python environment yourself)
└── test_frontend.py, test_frontend_project.py  # Playwright browser smoke tests
```

### Why this instead of a Streamlit GUI

An earlier iteration of this project (`relion_tomo_bridge`) used Streamlit
for a quick browser-rendered front end. That was replaced once the actual
requirements became clear: draggable/resizable popup windows per job
(cryoSPARC-style), a hideable job list, UI-wide zoom, and a live-streaming
output pane alongside an editable command box — all things Streamlit's
rerun-the-whole-script execution model can't do cleanly. RELION-US's
frontend is instead a small vanilla JS app using WinBox.js for the popup
windows and a raw websocket per job run for live stdout/stderr, which
supports all of that directly. The `relion_tomo_bridge` converters
(`star_io.py`, `imod_bridge.py`, `warp_bridge.py`, `deepetpicker_bridge.py`)
carried over unchanged into `backend/converters/` — only the GUI wrapping
them changed.

### Draft command heuristic

The command box in every job popup is pre-filled by a **best-effort** rule,
not a full reimplementation of RELION's C++ command logic: for each active
field, if a literal `--<key>` flag appears in that job's real
`getCommands<Job>Job()` source (extracted verbatim), the draft emits
`--<key> <value>`. RELION's real branching logic (e.g. MotionCorr's
`nr_mpi`-dependent choice of binary, or `do_float16` mapping to the
differently-named `--float16` flag) is intentionally *not* mechanically
reimplemented — fields without a literal matching flag are left out of the
draft and listed as "unmapped," rather than guessed at, and the job's real
RELION C++ source is one tab away for cross-checking. See the main
`README.md`'s "How the draft command is built" for the full explanation.

#### Draft-command overlays for RELION-5's Python tomo tools

The `--<key>`-matches-the-flag rule holds for the ~27 core RELION programs,
whose CLI flag names equal their internal option keys. It breaks for
RELION-5's newer **Python tomo tools** (`relion_python_tomo_import`,
`_pick`, `_denoise`, `_exclude_tilt_images`) and DynaMight, which use
hyphenated multi-word flags (`--tilt-image-movie-pattern`,
`--nominal-pixel-size`, `--output-directory`) that share no spelling with
the snake_case option key (`movie_files`, `angpix`). Two bugs resulted, both
found in an August 2026 audit of the import commands:

1. **Truncated flags.** `extract_job_definitions.py`'s flag regex
   (`--[A-Za-z0-9_]+`) stopped at the first hyphen, so `flags_used` recorded
   `--tilt`, `--nominal`, `--dose` instead of the real flags. Fixed the
   regex to allow hyphens inside a flag body and re-extracted; the change
   touched only those 5 jobs' `flags_used`.

2. **Wrong program + unmappable flags for `TomoImport`.** The extractor's
   `program_guess` picked the first `command = "..."` literal in
   `getCommandsTomoImportJob()`, which is the `do_coords == true`
   coordinate-importer (`relion_tomo_import_coordinates`) — even though
   `do_coords` **defaults to false** and the real default program is the
   SerialEM tilt-series importer (`relion_python_tomo_import SerialEM`). So
   the default draft was the wrong program plus two stray coordinate-branch
   flags.

The fix is a small, **source-verified data overlay** in `job_catalog.py`
(`DRAFT_PROGRAM_OVERRIDE`, `DRAFT_FLAG_MAP`, `DRAFT_SUPPRESS`), transcribed
verbatim from `getCommandsTomoImportJob()` / `getCommandsTomoExcludeTiltImagesJob()`
and cited by source line — the same "curated overlay verified against RELION
source" pattern as `JOB_DIRNAME`. A mapped flag is authoritative (always
emitted, bypassing the unreliable `flags_used` test); `DRAFT_SUPPRESS` keeps
the non-default (`do_coords`) branch's options out of the default draft. This
is deliberately **not** a reimplementation of RELION's per-job command
branching: jobs with genuinely multi-command / mode-branched builders
(`TomoPickTomograms`, `TomoDenoiseTomograms`) are left as program-name-only
drafts with every field flagged unmapped and the real source shown, rather
than risk a subtly-wrong reconstruction. `TomoImport` and
`TomoExcludeTiltImages`, whose default builders are single clean commands,
now draft correct, complete commands.

#### Execution model: run from the project root (matches RELION)

The runner executes the approved command with `cwd` set to the **project
root**, exactly like RELION — so project-root-relative inputs (`frames/*.mrc`)
and the command's `--o <JobDir>/jobNNN/` output path resolve the same way
RELION's own GUI resolves them. To make that work, the draft now includes the
RELION-style output flag: `--o <JobDir>/jobNNN/` for most programs,
`--output-directory` for the RELION-5 Python tomo tools (per-job table
`job_catalog.DRAFT_OUTPUT_FLAG`, verified against source — e.g.
TomoAlignTiltSeries/TomoReconstructTomograms were checked and use plain `--o`,
so they are *not* in the override list). `run.cwd` stays the job's own output
directory (what the Outputs tab / clean / delete / download operate on); only
the process's working directory is the project root.

The prospective `jobNNN` is computed when the popup opens (from the shared job
counter, RELION's `rlnPipeLineJobCounter` convention) and shown in the draft.
At Run time the authoritative number is finalized; if another job was recorded
in between, the runner allocates the next free number, creates that directory,
and rewrites the command's output path to match — surfaced as a single note in
the run's live output, so the command and the created directory never
disagree. Custom (in-process) importers resolve their input paths against the
project root too (`custom_jobs._resolve_in`), and continue to write outputs
relative to the project root.

This keeps RELION-US faithful to RELION's own contract (`getCommands*Job()`
appends `--o outputname`, working directory = project root), which is the
stated goal: when RELION changes, the overlays and the extraction pipeline are
the small, well-marked places to update.

### Change Project

RELION-US isn't tied to the directory it was launched from. A folder counts
as a RELION project if it has RELION's own `default_pipeline.star` or a
`.relion_us/` marker RELION-US writes itself the first time you point it at
a folder (holding only a small run-history summary — never RELION's own
pipeline state, which only RELION's own tools create correctly). Switching
projects, browsing folders, and the "this doesn't look like a RELION
project" prompt are all handled by `backend/project_manager.py` and the
`/api/project/*` endpoints in `main.py`; see that module's docstring for
the full design reasoning.

### SPA / Tomo / All jobs-list toggle

The Jobs sidebar has a three-way toggle above the search box to declutter
the list — "SPA", "Tomo", "All" — for users who only work in one pipeline
day to day. **It is a display filter only.** It never restricts which jobs
can be opened or run: a non-empty search always searches the full 35-job
catalog regardless of the toggle (see `applyJobFilters()` in
`frontend/app.js`), so every job stays one search away no matter what's
selected. "All" is the honest default for anyone who wants it.

**Are SPA/tomography flags available in the project's own STAR files? No.**
Checked directly against RELION's own source
(`src/pipeliner.cpp`'s `PipeLine::write()`, ~lines 2192-2205 in the
checkout this app is built against): the `pipeline_general` block of
`default_pipeline.star` holds only `rlnPipeLineJobCounter` — nothing that
labels the project as SPA or tomography. The closest real signal is
per-job: each row of the `pipeline_processes` block carries
`rlnPipeLineProcessTypeLabel`, the same string as `job_catalog.py`'s
`label_new` column for that job type.

So the toggle's classification (`backend/job_catalog.py`:
`PIPELINE_SPA_ONLY` / `PIPELINE_TOMO_ONLY` / `pipeline_type()`) is a
per-job-type heuristic grounded in what's actually verifiable from RELION's
source, not a project-level flag:

1. Every `internal_name` RELION itself prefixes with `Tomo` in
   `pipeline_jobs.h` is tomography-specific by construction (10 jobs).
2. Where a `Tomo`-prefixed job is the direct sibling of a non-`Tomo` job
   doing the analogous step — `Ctfrefine`/`TomoCtfRefine`,
   `Motionrefine`/`TomoAlign`, `Autopick`+`Manualpick`/`TomoPickTomograms`,
   `Extract`/`TomoSubtomo` — the non-`Tomo` original is classified
   SPA-only (5 jobs): RELION built a whole separate `Tomo` job rather than
   reusing it.
3. The 3 custom bridges (ImodImport, WarpImport, DeepETPickerImport) are
   tomography-only in this app's scope, per each bridge's own docstring.
4. Everything else (17 jobs — `Import`, `Motioncorr`, `Ctffind`, and the
   classification/refinement/post-processing jobs from `Select` through
   `External`) is `shared` and visible in both the SPA and Tomo views:
   RELION-5's tomography pipeline explicitly funnels pseudo-subtomograms
   through the *same* `Class2D`/`Class3D`/`Inimodel`/`Autorefine`/
   `Postprocess`/`Localres`/`Maskcreate`/etc. jobs SPA particles use — the
   documented purpose of "pseudo-subtomograms" (Burt et al. 2024, PMID
   39147729) is making tomography particles look like ordinary
   `particles.star` rows so those downstream jobs work unmodified.

**Auto-switching based on the loaded project** — the "nice to have" from
the original request — works when there's a signal to use:
`project_manager.detect_pipeline_hint()` reads `default_pipeline.star`'s
`pipeline_processes` block (via the same `starfile` wrapper `star_io.py`
already uses) and checks which known SPA-only/Tomo-only
`rlnPipeLineProcessTypeLabel` values that project has actually run,
returning `'spa'`, `'tomo'`, `'mixed'`, or `'unknown'`. `GET /api/project`
exposes this as `pipeline_hint`; the frontend auto-applies it on project
load/switch only when it's unambiguous (`'spa'` or `'tomo'`) — a brand-new
project (`'unknown'`, no `default_pipeline.star` yet) or one that's run
both types (`'mixed'`) leaves the toggle wherever it was, which is exactly
the "if not that's fine, a manual switch is good" fallback that was asked
for. The user's last manual choice also persists across reloads via
`localStorage` (falls back to `'all'` silently if storage is unavailable).

### Division of labor: local vs. a SLURM cluster

A common policy on shared HPC systems: trivial local file operations
(launching the app, a small STAR edit) run directly on a login node or
workstation, while anything that pulls from the web, takes more than a few
seconds, or runs iteratively — essentially all `relion_*` processing jobs,
and the converters when run over a full dataset rather than a handful of
test files — goes through the SLURM queue instead. **As of this version,
that's not yet wired into the job popups themselves** (an explicit v1
scope decision: direct subprocess execution only, no SLURM integration
yet) — `slurm/submit.py` and the two `.sbatch` templates are available as
a standalone command-line path for running a job as a proper batch job in
the meantime, and are the natural starting point for adding a "Run on
cluster" option to the popups later. The templates are generic (no
site-specific partition/module names — see "SLURM templates" in
`README.md`), so they work on any SLURM cluster, not just one particular
site's.

## Format-bridging honesty note

RELION-5's tomography STAR schema (`rlnTomoName`, `rlnCoordinateX/Y/Z`,
`rlnTomoParticleId`, per-tomogram optics groups, etc.) is documented in the
RELION-5 tomography paper and the ReadTheDocs pages linked below, and
`star_io.py` targets that. Warp/M's and DeepETPicker's exact output column
names can drift between versions and installs, so `warp_bridge.py` and
`deepetpicker_bridge.py` keep the field-mapping isolated in one place
(`warp_bridge.DEFAULT_COLUMN_MAP`) rather than hard-coding names that
couldn't be verified — send a real `.tomostar`/particle STAR export or
DeepETPicker run to get an exact, verified mapping instead of the current
pass-through/diff behavior.

### August 2026 import-bridge audit (verified against upstream)

Each external format was checked against its own docs/source; findings and
the resulting code changes:

- **IMOD** (bio3d.colorado.edu/imod/doc): `.xf` (`A11 A12 A21 A22 DX DY`,
  one line per tilt image) and `.tlt` (one angle/deg per line) formats
  **confirmed correct**. `model2point`/`point2model` accept `-input`/`-output`
  (PIP programs) and `-scat` — the flags the bridge uses are **valid**;
  default `model2point` output is `X Y Z` only, which is what
  `model_to_coordinates` parses. **Caveat surfaced (real):** IMOD tomograms
  exist in "rotated" (depth = Z, correct for RELION) vs "flipped" (`trimvol
  -yz`, Y↔Z swapped, handedness inverted) orientations, and a model on a raw
  `tilt` reconstruction has depth in Y. The bridge copies X,Y,Z verbatim and
  does not guess orientation; this is now documented in
  `imod_bridge.model_to_coordinates` and the `ImodImport` field help. IMOD
  model coords are 0-based; Z carries a -0.5 half-pixel offset.

- **DeepETPicker** (github.com/cbmi-group/DeepETPicker): `.coords` column
  order `class_id x y z` (4 cols, voxels) **confirmed correct**, and the
  paper citation (PMID 38453943) **verified**. Its own
  `utils/coords_to_relion4.py` also accepts a bare 3-column `x y z` file
  (class_id defaults to 1); `read_coords` was **too strict** (required 4
  cols) and now matches that tolerance.

- **Warp/M** (warpem.github.io): the conservative empty `DEFAULT_COLUMN_MAP`
  is **vindicated**. There are two Warp→RELION paths: `ts_export_particles`
  already writes a RELION-5 optimisation set (native `rln*`, no bridge
  needed), while `.tomostar` and older particle exports use Warp's own `wrp*`
  columns that genuinely need mapping. The "Warp and RELION converged"
  framing is true only for the export path; docstrings and the `WarpImport`
  help now state the distinction and the reconstruction-vs-export pixel-size
  caveat.

- **AreTomo2** (`aretomo_bridge.py`, new): reads AreTomo2's `.aln` global
  alignment block (`SEC ROT GMAG TX TY SMEAN SFIT SCALE BASE TILT`, verified
  against teamtomo/alnfile + the AreTomo manual) and converts it to IMOD
  `.xf` + `.tlt`, which RELION-5's IMOD tilt-series import consumes. This
  hand-off (rather than writing RELION's tilt-series STAR directly) is a
  deliberate safety choice: the `.xf` mapping (`θ = -ROT`, negated shift
  rotated into the transformed frame) is verified by working community code
  and AreTomo's own `-OutImod` export, whereas the exact sign conventions for
  RELION's `rlnTomoZRot`/`XShiftAngst`/etc. are not something we could verify
  to the standard this project holds ("don't hallucinate"). Only ROT/TX/TY/
  TILT/SEC are consumed; dark frames are reported. TX/TY are in pixels of the
  aligned stack (the `.aln` has no pixel size), noted in the field help.

**Coordinate flips (`coord_transform.py`, new):** the IMOD and DeepETPicker
importers gained shared, tested options to swap Y/Z (the IMOD flipped-vs-
rotated fix) and mirror any axis about a supplied tomogram dimension. One
implementation, routed through `apply_coordinate_transform`, so the two
importers can't drift. A mirror requested without its dimension raises rather
than silently producing wrong coordinates. (Contrast inversion was considered
but deliberately not added: these are coordinate/alignment importers, not
density importers, so there's nothing to invert — it belongs on a future
tomogram/map importer.)

**Command Center lineage:** `list_runs` now attaches `input_links` — each run's
detected inputs that live under an earlier job's output directory are linked
to that producing job, and the timeline view renders them as clickable
"↳ from jobNNN" chips. Still best-effort (not RELION's real pipeline graph),
consistent with `_detect_inputs`.

## Tomogram / particle-pick visualizer

A viewer, launched from the "🔍 Visualize" topbar button — **not a RELION
job**: it never appears in the Command Center and writes nothing. napari and
DeepETPicker's own picker GUI are desktop Qt/pyqtgraph apps that can't run in
a browser at usable speed, so `backend/viz.py` reproduces DeepETPicker's
*interaction model* in a browser-native way (verified against
github.com/cbmi-group/DeepETPicker `main.py` / `utils/utils.py`, read
2026-08-15):

- **Slice browsing.** `mrcfile.mmap` memory-maps the volume so a slice request
  touches only that slice's bytes; the server contrast-stretches it to 8-bit
  and returns a PNG. The volume is never loaded whole. An XY/XZ/YZ axis toggle
  covers the three orthogonal planes (DeepETPicker shows all three at once via
  a linked tri-view; we use one pane with a toggle).
- **Pick overlay.** Picks are sent once as JSON (voxel coords); the browser
  draws them on a `<canvas>` using DeepETPicker's exact rule — a particle
  appears on every slice within ±(diameter/2) of its centre, with radius
  `√(r² − Δ²)` (the spherical cross-section), diameter and outline width as
  separate controls. Scrubbing Z is one small PNG fetch per slice, no per-slice
  pick round-trip.
- **Contrast.** DeepETPicker's base display is a global min/max stretch with an
  optional 1–99% percentile clip. Raw cryo-ET min/max is usually washed out, so
  the default here is a robust 0.5–99.5% percentile estimated from a strided
  slice sample at open time; black/white-point sliders override it.
- **Inputs.** Accepts an MRC (+ optional particles STAR), or a STAR that is an
  optimisation set (→ resolves tomograms.star + particles.star), a
  tomograms.star, or a particles/coords STAR. Multiple tomograms populate a
  selector (one loaded at a time). Coords use `rlnCoordinateX/Y/Z` (voxels),
  with a best-effort conversion for `rlnCenteredCoordinate*Angst` using the
  volume dims + pixel size.
- **Filename-mismatch warning.** If the chosen tomogram's name doesn't
  correspond to any `rlnTomoName` in the picks file, the viewer warns and
  offers **Load anyway / Reload files / Cancel** (the user asked for cancel or
  reload; "load anyway" is added so a legitimate naming difference isn't a dead
  end). Cancel loads the volume without the mismatched picks.
- **Safety.** Every path is resolved against the active project directory and
  must stay inside it; the viewer only ever reads.
- **Browse buttons.** Both inputs have a server-side file picker
  (`pickFileDialog()` in `app.js`) rather than an `<input type="file">` — the
  backend often runs on a different machine than the browser (an HPC login
  node), so the browser's own filesystem is the wrong one. It reuses the
  existing `POST /api/project/browse` endpoint, which already returns files
  alongside folders, filters by the extensions `viz.py` accepts, resumes in the
  folder the field currently points at, and returns a project-relative path
  (what the viewer's API expects, and the idiom RELION itself stores).

Endpoints: `POST /api/viz/inspect`, `GET /api/viz/volume-info`,
`GET /api/viz/slice`, `POST /api/viz/picks`. New deps: `mrcfile`, `pillow`.

## Code-quality audit (August 2026)

A full review of the codebase (mechanical linting + line-by-line review of the
backend, converters, and frontend). Fixes worth knowing about, because several
were silent-wrong-data bugs rather than style issues:

**Scientific correctness**

- **Tomogram-name matching was a bare substring test.** `TS_1` matched `TS_10`,
  so the viewer would happily overlay one tomogram's particles onto another —
  and `TS_1`/`TS_10`/`TS_11` naming is completely normal. Now
  `viz._names_match()` compares filename stems and only accepts a substring at
  a separator boundary.
- **A no-match returned every pick.** When no `rlnTomoName` matched, `load_picks`
  fell back to the whole table, drawing all tomograms' particles on one
  tomogram — visually indistinguishable from a correct overlay. It now returns
  an empty list.
- **The axis mirror was off by one voxel.** `coord -> size - coord` sends
  coordinate 0 to `size`, one voxel outside the volume. Reflection about the
  centre of a 0-based axis is `(size - 1) - coord`; the code, docstrings, field
  help and tests now all say so.
- **NaN voxels rendered an all-black slice.** `np.percentile` returned NaN, the
  `hi <= lo` guard silently didn't fire (NaN comparisons are always False), and
  the user concluded the tomogram was broken. Now `nanpercentile` + an explicit
  finite check.
- **AreTomo2 silently dropped unparseable `.aln` rows.** IMOD and RELION pair
  `.xf` line N with stack image N positionally, so one dropped row mis-pairs
  every subsequent transform — a corrupted reconstruction that still looks
  plausible. `aln_to_imod` now cross-checks the row count against the header's
  `RawSize` minus dark frames and refuses to write on a mismatch.
- **RELION help text was mojibake.** The extractor encoded UTF-8 then decoded
  latin-1 via `unicode_escape`, so RELION's `−4 to −7` displayed as `â4 to â7`.
  Fixed and re-extracted (only those 5 help strings changed).

**Data loss**

- **Custom-job outputs went to the project root**, not the job directory the
  Outputs tab / Clean / Delete operate on — so the tracked directory was always
  empty and successive imports silently overwrote one shared `particles.star`.
  Runners now receive their job dir (`custom_jobs._resolve_out`).
- **The backup clobbered itself.** A second run copied the *generated* output
  over `particles.star.bak`, destroying the hand-curated original the backup
  exists to protect. It now falls back to `.bak2`, `.bak3`, …
- **`StarDocument.write()` dropped block names** for single-block files, writing
  an anonymous `data_` header that RELION-5 can't look up by name.
- **Warp was the only importer overwriting with no backup**; it now matches the
  others.

**Correctness / robustness**

- **Custom jobs had no `default_values`**, so every field opened blank — a blank
  numeric parses to `NaN`, a blank output path resolved to the job directory
  itself. Derived once in `main._custom_job_definition()` from each option's
  declared `default`.
- **A job aborted before its process existed kept running.** `abort_run` rejected
  `pending` runs, and cancelling the launcher mid-spawn could orphan a process
  group. There's now an `abort_requested` flag the launcher honours (it declines
  to spawn at all), and abort accepts `pending`.
- **A raising output pump stranded a run as "running" forever**, leaked its
  sibling and never reaped the child. `_run_subprocess` now has the same
  `try/finally` `_run_custom` always had.
- **The run websocket never observed a disconnect** (Starlette raises only from
  `receive()`, never `send()`), so it parked on `queue.get()` forever — leaking
  a task and a subscriber per popup opened. It now races a reader task.
- **The temp zip leaked** on any failure inside the archive loop.
- **`PATCH /api/runs/{id}` validated after writing** — an invalid status was
  rejected only after the alias/note edits had already hit disk. Now validated
  up front.

**Frontend**

- Double-clicking **Run** started a second job and orphaned the first
  websocket; the button now stays disabled once a run exists.
- The **download arrows in the Clean review list were dead** — that view
  rendered them but never wired the click handler.
- **Overwrite closed the popup before the request**, so a failure destroyed the
  user's edited command with no way back.
- The project's **auto-detected pipeline hint overwrote the user's saved
  SPA/Tomo/All choice** (same `localStorage` key); it no longer persists.
- The visualizer **committed `state.mrc` before the volume loaded**, so a failed
  load left it requesting slices of the new volume with the old one's index
  range.
- Slice/contrast sliders now **debounce** (a drag fired ~60 server-side
  mmap+PNG encodes per second).
- **13 native `alert()` calls** contradicted the file's own documented rule
  (they block the page, including Playwright); all now use the custom
  `errorDialog`.
- Removed dead state (`openPopups`, which retained every popup ever opened,
  plus `jobCounter`, `vizCounter`, `outputsLoaded`, `withCheckboxes`).

**Performance**

- `job_definitions_raw.json` (~500 KB) was re-read and re-parsed on every job
  open *and* every draft recompute — the latter fires as the user types. Now
  `lru_cache`d.
- `viz.load_picks` used `.iterrows()` (which boxes each row as a Series) on up
  to 10⁵ particles; now vectorized.
- The contrast sample fancy-indexed 24 full-resolution slices (~1.6 GB for an
  unbinned 4096² tomogram, against this module's "never load the volume"
  premise); it now strides in-plane and takes both percentiles in one pass.

Backend tests: **204 passed, 1 skipped** (up from 183). Both Playwright suites
pass with zero console errors.

## Live job progress (iterative jobs)

Classification and refinement jobs run for many iterations and RELION writes a
small status file after each one, so the Progress tab in those job popups shows
what's happening rather than only a wall of log text.

**Source of truth.** Everything comes from files RELION writes itself, verified
against `src/ml_optimiser.cpp` (`MlOptimiser::write()`) and `src/ml_model.cpp`
(RELION cloned 2026-08-14):

```
run_it###_model.star          (run_it###_half1_model.star for split half-sets)
run_it###_classes.mrcs        2D: all classes in one stack
run_it###_class###.mrc        3D: one volume per class
```

The `model.star` is a few KB and carries exactly what's worth watching —
`model_general`: `rlnCurrentResolution` (note: **1/Å**, converted to Å here so
one unit is used throughout), `rlnNrClasses`, `rlnReferenceDimensionality`;
`model_classes`: `rlnReferenceImage`, `rlnClassDistribution`,
`rlnEstimatedResolution` (Å), `rlnAccuracyRotations/TranslationsAngst`. Every
label name was read out of RELION's `src/metadata_label.h`, not assumed. Only
`half1` is read for a split-half refinement — the two halves track each other,
and reading both would double-count every iteration.

**Which jobs.** `progress.PROGRESS_JOBS` = Class2D, Class3D, Autorefine
(Refine3D), Inimodel, MultiBody, TomoReconPart. Deliberately not everything: an
Import or MaskCreate has nothing to plot, and the tab hides itself for them. The
frontend keeps a matching set for tab visibility, but the backend is
authoritative — it returns `supported: false` and the tab disappears if the two
ever drift.

**Cost.** This was built to the constraint "don't take up too much memory or
storage":

- **Nothing is written to disk.** Charts parse the small per-iteration
  `model.star`; thumbnails are rendered on demand from the MRCs RELION already
  wrote, and are never cached server-side. The feature adds zero storage.
- Thumbnails are downsampled to 128 px and 8-bit greyscale; a 3D class shows one
  central slice, not a rendering.
- Parsed iterations are memoised on `(path, mtime, size)`, so re-polling a run
  whose earlier iterations haven't changed costs a `stat()` rather than a
  reparse. RELION never rewrites a finished iteration, so that key is safe.
- A single poll is capped at the most recent 200 iterations.
- Polling runs every 4 s and **only while the job is actually running**.

**User controls** (per job, in the Progress tab):

- **Live progress** — on by default for supported jobs; unticking stops polling
  entirely.
- **Images every N iterations** — thumbnails refresh only on iterations that are
  a multiple of N (1 = every). Charts still update every iteration, since they're
  nearly free.
- **Keep all** — off by default. On, earlier iterations' thumbnails are kept so
  you can compare how classes evolved; off, only the newest set is held, so
  memory is constant no matter how long the run is.

**Charts** are hand-rolled inline SVG — no charting library, keeping the frontend
dependency-free and offline-capable (HPC login nodes often have no outbound
internet). Two forms only: a line chart of resolution against iteration (two
series, one shared Å axis — never a second y-scale) and a bar chart of particles
per class. Series colours come from a validated light/dark-paired palette and are
read from CSS variables, so both charts follow the theme switch and are repainted
on it.

## Dark / light theme

The stylesheet was already written entirely against CSS custom properties, so the
light theme is a straight variable swap under `:root[data-theme="light"]` — no
per-component light-mode branches. Dark remains the default; a remembered choice
wins, and with nothing stored the app stays dark rather than following the OS
(this app's designed default). The switch lives in the top bar and persists to
`localStorage`. The few hardcoded terminal-ish surfaces (command box, live output)
became variables (`--console-bg`, `--console-bg-deep`, `--console-text`) so they
restyle too. Chart series colours are separate, mode-specific values validated
against each surface rather than an automatic flip.

## References

- Burt A, Toader B, Warshamanage R, von Kügelgen A, Pyle E, Zivanov J,
  Kimanius D, Bharat TAM, Scheres SHW. *An image processing pipeline for
  electron cryo-tomography in RELION-5.* FEBS Open Bio. 2024;14(11):1788-1804.
  PMID: [39147729](https://pubmed.ncbi.nlm.nih.gov/39147729/) · DOI: [10.1002/2211-5463.13873](https://doi.org/10.1002/2211-5463.13873)
- Zivanov J, Otón J, Ke Z, von Kügelgen A, Pyle E, Qu K, Morado D,
  Castaño-Díez D, Zanetti G, Bharat TAM, Briggs JAG, Scheres SHW. *A
  Bayesian approach to single-particle electron cryo-tomography in
  RELION-4.0.* eLife. 2022;11:e83724.
  PMID: [36468689](https://pubmed.ncbi.nlm.nih.gov/36468689/) · DOI: [10.7554/eLife.83724](https://doi.org/10.7554/eLife.83724)
- Zivanov J, Nakane T, Forsberg BO, Kimanius D, Hagen WJH, Lindahl E,
  Scheres SHW. *New tools for automated high-resolution cryo-EM structure
  determination in RELION-3.* eLife. 2018;7:e42166.
  PMID: [30412051](https://pubmed.ncbi.nlm.nih.gov/30412051/) · DOI: [10.7554/eLife.42166](https://doi.org/10.7554/eLife.42166)
- Liu G, Niu T, Qiu M, Zhu Y, Sun F, Yang G. *DeepETPicker: Fast and
  accurate 3D particle picking for cryo-electron tomography using weakly
  supervised deep learning.* Nat Commun. 2024;15:2090.
  PMID: [38453943](https://pubmed.ncbi.nlm.nih.gov/38453943/) · DOI: [10.1038/s41467-024-46041-0](https://doi.org/10.1038/s41467-024-46041-0)
- RELION source and GUI code: https://github.com/3dem/relion
- IMOD documentation (Boulder Lab): https://bio3d.colorado.edu/imod/doc/
  (`model2point`, `point2model`, `tilt`, `trimvol` man pages; `.xf`/`.tlt`
  formats; binary model coordinate spec)
- DeepETPicker: https://github.com/cbmi-group/DeepETPicker
  (`.coords` format, `utils/coords_to_relion4.py`)
- Warp/WarpTools/M documentation: https://warpem.github.io
  (`.tomostar` `wrp*` fields, `ts_export_particles` optimisation-set output)
- AreTomo2: https://github.com/czimaginginstitute/AreTomo2 and the AreTomo
  user manual; `.aln` parsing + IMOD `.xf` mapping cross-checked against
  teamtomo/alnfile: https://github.com/teamtomo/alnfile
  (`src/apps/maingui.cpp`, `src/pipeline_jobs.h`, `src/gui_jobwindow.cpp`)
- RELION documentation: https://relion.readthedocs.io/
- IMOD tomogram import reference:
  https://relion.readthedocs.io/en/release-4.0/STA_tutorial/ImportTomo.html
- DeepETPicker source: https://github.com/cbmi-group/DeepETPicker
