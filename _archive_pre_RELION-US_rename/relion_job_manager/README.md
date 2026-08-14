# RELION Job Manager

A browser-based, portable job manager for RELION — built as a *companion*
to RELION, not a modified RELION GUI. It reads RELION's own source
(`pipeline_jobs.cpp`/`.h`, `gui_jobwindow.cpp`) to build accurate forms for
every RELION job type (32 of them, single-particle and tomography), plus
the IMOD/Warp-M/DeepETPicker import bridges from the earlier
`relion_tomo_bridge` project, and runs everything through one consistent
popup-window UI: standard inputs on top, an Advanced tab with every other
option, an Errors tab, live streaming output at the bottom — and, critically,
**an editable command box you approve before anything runs.**

## Why this exists

RELION's own GUI is a compiled Qt5/C++ application. Its command-assembly
logic sometimes duplicates flags or inserts options you can't see or
override from the "additional arguments" box — there's no way to see or
edit the exact string it's about to run. This app never does that: whatever
is in the command textbox when you click Run is executed exactly as
written, via the shell, nothing added or removed. The draft command that
pre-fills that box is a best-effort starting point (see "How the draft
command is built" below) — always check it, and the job's real RELION C++
source is one tab away for cross-referencing.

It's also built to be portable and multi-machine-friendly: it's a normal
web page. Run the backend on your workstation or a Rivanna/Afton login
node, and open the page from any browser on the network — no Qt, no X11
forwarding, no display server.

## Running it

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8420
```

Then open `http://<that machine's address>:8420/` in a browser — including
from a different machine, since it binds `0.0.0.0`. On Rivanna/Afton,
launch it on a login node (or an interactive job) and either port-forward
over SSH (`ssh -L 8420:localhost:8420 <node>`) or connect directly if your
network allows it.

**You do not have to launch it from inside a project directory** — see
"Change Project" below. If you *do* `cd` into an existing RELION project
before running `uvicorn`, it's picked up automatically; otherwise the app
starts in a default project folder next to `backend/` and you switch from
there.

No CDN dependency: WinBox.js (the popup-window library) is vendored under
`frontend/vendor/` (Apache-2.0, see `WINBOX_LICENSE.txt`), specifically
because Rivanna/Afton login nodes and many workstations have no outbound
internet access.

## Using it

- **Change Project** (top bar): switch which RELION project directory the
  app is pointed at, at any time, without restarting the backend. Type a
  path directly and hit Go/Enter, or click into subfolders in the browser
  below it — that browser lists folders on the *machine running the
  backend*, not your browser's machine, which matters when the backend is
  on a remote host like a Rivanna/Afton login node. If the folder you pick
  doesn't look like a RELION project (no `default_pipeline.star` and not
  previously opened here), you're prompted to either start a new project
  there or pick a different folder — starting a new project never writes
  RELION's own pipeline file for you (see "How project detection works"
  below), it only creates the folder if needed and a small marker RELION
  itself never sees.
- **Job history bar** (below the empty-state message): every run started
  in the *current* project, newest first, reload-safe. Click a chip to
  reopen that run's command/status/live-output popup — for a run from the
  current backend session this reconnects to the live stream; for one from
  a previous session (i.e. the backend has since restarted) it shows the
  saved status only, since the transcript itself isn't persisted, only the
  summary.
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
DeepETPicker` are the same converters built in the earlier
`relion_tomo_bridge` project (`backend/converters/`, 22/23 unit tests
passing — see that project's README for details on what's verified vs.
what needs a real sample file from your install, particularly Warp/M's
exact column names). They don't have a command box — they call directly
into Python — but they use the same popup layout, live output, and Errors
tab as every RELION job.

## How project detection works, and whether this needs RELION's real GUI

A folder counts as a RELION project if it already has RELION's own
`default_pipeline.star` (written by the real RELION GUI or by
`relion_pipeliner`) **or** a `.relion_job_manager/` marker this app writes
the first time you point it at a folder. That marker only ever contains a
small `run_history.json` (job summaries: name, command, status, timestamps
— never full transcripts) — this app deliberately never fabricates
`default_pipeline.star` itself; that's RELION's own file format, and only
RELION's own command-line tools (which is what "Run" in every job popup
calls) create it correctly the first time a real job executes there.

This also answers a natural follow-up question: **this app never runs
RELION's own compiled GUI** (the `relion` binary, built from
`maingui.cpp`/`gui_jobwindow.cpp`/Qt5). It only shells out to RELION's
*command-line* programs (`relion_import`, `relion_refine_mpi`, etc.) — the
same binaries the real GUI would call anyway. RELION's GUI source was only
ever read as *text*, by `data/extract_job_definitions.py`, at build time,
to get accurate field names/labels/command logic — it isn't compiled and
isn't required at runtime. In short: this is a standalone alternative front
end, not a wrapper around RELION's own GUI process, and it needs RELION's
command-line tools installed (any normal RELION install provides these)
but never the GUI binary itself.

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

Then re-run `backend/tests/test_job_registry.py` — it's written as a
regression suite against the *real* extracted data (not synthetic
fixtures), so it will catch new parsing gaps the same way it caught three
real ones during development (see below).

## Known limitations / what to double check

- The draft-command heuristic (above) is best-effort, not a guaranteed
  match for RELION's exact branching logic — always review before running,
  which is the whole point of the editable box.
- `External`'s "Params" tab exposes RELION's generic
  `param1_label`/`param1_value` ... `param10_label`/`param10_value` slots
  verbatim (that's genuinely how RELION's own External job works — you
  name your own flags there).
- Warp/M column names in the custom import job are unverified against your
  specific install (see `relion_tomo_bridge`'s README) — send a real sample
  file to get an exact mapping instead of the current pass-through.
- This extraction pipeline was built and tested against one specific
  RELION checkout; job internals do change across releases, so re-running
  the extractor (above) periodically is worth doing, and the test suite
  will flag most breakage immediately.
- Job history persists run *summaries* (command, status, timestamps) per
  project, not full stdout/stderr transcripts — reopening a job from
  history after the backend has restarted shows its last known status but
  not its old live output. Runs from the current backend session stream
  normally either way.

## What was caught by the test suite during development

Three real parsing bugs were found and fixed by `backend/tests/test_job_registry.py`
before this was called done, each kept as a named regression test:

1. `std::string("")`-style default values leaking raw C++ into a draft
   command (`test_no_leftover_cpp_syntax_in_draft_or_defaults`).
2. Five tomography jobs (TomoSubtomo, TomoReconPart, TomoAlign,
   TomoCtfRefine, and others) losing their entire "standard" tab because
   their fields are added via a shared `addTomoInputOptions()` /
   `placeTomoInput()` helper rather than inline — fixed by expanding those
   helper calls using their own real definitions
   (`test_standard_and_advanced_fields_are_disjoint_and_known`).
3. Three jobs (DynaMight, ModelAngelo, External) with no hard-coded
   binary — RELION runs a user-configured executable path instead — fixed
   by resolving a `{joboptions.<key>}` placeholder against real field
   values (`test_executable_path_placeholder_resolves_from_field_values`).
