# RELION-US — architecture & scope

RELION-US ("RELION - User Sourced") is a browser-based companion to
RELION — not a fork or patch of RELION itself, and not a wrapper around
RELION's own compiled GUI. It's a separate front end that reads RELION's
own source to build accurate job forms, then drives RELION's real
command-line programs as subprocesses, with format-conversion bridges for
IMOD, Warp/M, and DeepETPicker folded in as three more entries in the same
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
│   ├── custom_jobs.py       # wires the 3 converters in as Job types
│   ├── converters/          # pure-Python + subprocess format bridges
│   │   ├── star_io.py           # thin wrapper over `starfile`, RELION-5 tomo aware
│   │   ├── imod_bridge.py       # IMOD .xf/.tlt/.mod <-> RELION tilt-series STAR
│   │   ├── warp_bridge.py       # Warp/M metadata <-> RELION-5 STAR
│   │   └── deepetpicker_bridge.py  # DeepETPicker coordinates -> particles.star
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
  (`src/apps/maingui.cpp`, `src/pipeline_jobs.h`, `src/gui_jobwindow.cpp`)
- RELION documentation: https://relion.readthedocs.io/
- IMOD tomogram import reference:
  https://relion.readthedocs.io/en/release-4.0/STA_tutorial/ImportTomo.html
- DeepETPicker source: https://github.com/cbmi-group/DeepETPicker
