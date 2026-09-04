# RELION-US — architecture & scope

RELION-US ("RELION - User Supported Frontend") is a browser-based companion to
RELION — not a fork or patch of RELION itself, and not a wrapper around
RELION's own compiled GUI. It's a separate front end that reads RELION's
own source to build accurate job forms, then drives RELION's real
command-line programs as subprocesses, with format-conversion bridges for
IMOD, Warp/M, DeepETPicker, and AreTomo2 folded in as four more entries in the same
Jobs list.

## Why a companion tool instead of patching RELION itself

RELION's own GUI (`relion`, built from `src/apps/maingui.cpp` and the
`gui_*` sources in [3dem/relion](https://github.com/3dem/relion)) is a Qt5
C++ application compiled together with the processing engine. Patching it
means rebuilding the RELION binary (CMake + Qt5 + FFTW + CUDA toolchain) for
every change, re-merging the patch on every upstream release, and running
only wherever that exact binary was built — none of which suits a front end
meant to be portable and quick to iterate on.

RELION-US is therefore a **separate, lightweight layer that sits next to a
normal RELION install**: it reads and writes the same STAR files RELION
uses (RELION's interchange format — the GUI is a job scheduler over STAR
files and `relion_*` command-line programs), and drives `relion_*` binaries
as subprocesses, exactly as the real GUI would. That gets an alternative,
more visual front end without touching upstream code, zero merge conflicts
with upstream RELION releases, and something that runs identically on a
laptop and, launched as a job, on any SLURM cluster.

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
  below) rather than hand-typed field lists — so the forms and the draft
  commands track whatever the installed RELION release actually defines,
  and the real C++ builder is always available for cross-reference.

## Components

```
relion_us/
├── backend/
│   ├── main.py              # FastAPI app: REST + one websocket per job run
│   ├── job_registry.py      # raw extraction -> API-ready job definitions,
│   │                        #   RELION's own tab groups for the Inputs tab,
│   │                        #   draft-command heuristic (see below)
│   ├── program_help.py      # runs <program> --help to list the CLI options
│   │                        #   RELION's GUI never exposes (Advanced section)
│   ├── job_catalog.py       # curated display metadata (names, categories)
│   ├── job_runner.py        # executes the approved command exactly as given;
│   │                        #   per-project run history persistence
│   ├── pipeline_bridge.py   # two-way default_pipeline.star sync via the real
│   │                        #   relion_pipeliner binary (see below); never
│   │                        #   writes RELION's pipeline format itself
│   ├── project_manager.py   # RELION-project detection, project switching,
│   │                        #   history load/save (see "Change Project" below),
│   │                        #   per-project settings incl. pipeline sync on/off
│   ├── custom_jobs.py       # wires the 4 converters in as Job types
│   ├── viz.py               # tomogram/pick VIEWER (not a job): mrcfile mmap ->
│   │                        #   PNG slices + pick JSON (see "Visualizer" below)
│   ├── progress.py          # live per-iteration charts + class thumbnails for
│   │                        #   iterative jobs (see "Live job progress" below)
│   ├── auth.py              # optional password gate: hashing, sessions, the
│   │                        #   Run-RelionUS --set-password/--enable-auth CLI
│   │                        #   (see "Password protection" below)
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
│   └── login.html             # self-contained login page (auth.py) -- no
│                              #   dependency on app.js/style.css, since it has
│                              #   to render while everything else is gated
├── data/
│   └── extract_job_definitions.py  # parses real RELION source -> job_definitions_raw.json
├── slurm/                    # generic sbatch templates + submit.py, any SLURM cluster;
│                              #   not yet wired into the job popups, see below
├── docs/                     # this file
├── Run-RelionUS              # launch helper (no install script -- see README.md
│                              #   for building the Python environment yourself);
│                              #   also the --set-password/--enable-auth/etc CLI
├── run_tests.sh              # tiered test runner: backend suite always, browser
│                              #   suites by tier (see "Testing" in README.md)
└── test_*.py                 # Playwright browser smoke tests (job list, Change
                              #   Project + recents, Command Center,
                              #   abort/overwrite, Progress tab + theme + file
                              #   pickers, orthogonal viewer, option placement,
                              #   adopting a RELION-built project)
```

### Job popup window and top bar

The frontend is a small vanilla JS app using WinBox.js for the popup window
and a raw websocket per job run for live stdout/stderr.

**Job popup sizing/rounding/single-instance.** `openJobPopup` (`app.js`)
tracks the one open job popup in a module-level `currentJobWinbox`, closed
(not just covered) right before the next one mounts, so its websocket and
progress polling tear down instead of streaming into a hidden window — a
`win.close()` from elsewhere (e.g. Overwrite's close-then-reopen) already
clears it via `onclose`, so this never double-closes. `width`/`height` are
percentage strings (`"94%"`/`"92%"`) — WinBox's own size parser (`V()` in the
vendored bundle) accepts percentages natively, resolved against the viewport.
Rounded corners are `border-radius` on `.job-popup-window.winbox` *and*
`.wb-body` separately (`style.css`) rather than `overflow: hidden` on the
outer `.winbox`: WinBox's resize-handle elements sit just outside its edges
(negative offsets, for a larger grab target), and `overflow: hidden` there
would clip them out of the clickable area. `.wb-header` has no background of
its own — the outer radius alone rounds the top corners; `.wb-body`'s own
opaque background needs its own bottom-corner radius or it squares off the
window's bottom two corners. One consequence worth knowing for anyone adding
UI: a popup this size visually covers the sidebar and Command Center behind
it, so opening a *different* job from the sidebar requires closing/collapsing
the current popup first — a real click can't reach through it. Browser tests
that need to simulate "open a second job while the first is still open"
therefore use `dispatch_event("click")` (fires the handler directly,
bypassing DOM hit-testing) rather than a literal mouse click — see
`test_jobs.py`.

**Top bar color.** `#topbar` overrides `--panel`/`--panel-alt`/`--text`/
`--text-dim`/`--border`/`--accent-dim` to fixed values, rather than a fixed
blue background plus per-child color rules: every child selector already
reads those custom properties for its own background/text/border/hover
color (`--panel`, `--text`, etc.), and CSS custom properties cascade down
the DOM, so overriding them once at `#topbar` makes the whole bar's buttons
and labels legible against the fixed blue automatically, in both themes,
without a second copy of every rule.

**No page-scale control.** No in-app zoom/scale slider: the CSS `zoom`
property doesn't compose with a touch browser's native pinch-to-zoom (the
two nest instead of one replacing the other), so the browser's own zoom is
used directly instead. `#menuWrap` (the top bar's Menu button and dropdown —
Settings, Tools ▸ Analyze, and the password-protection "Log out" item, see
"Password protection" below) owns `margin-left: auto`, pinning trailing top
bar controls to the right edge.

### Where a job's options live

One tab, two different questions, answered in order:

- **Inputs tab, RELION's own groups — everything RELION's own GUI shows.**
  All of a job's JobOptions, grouped under RELION's own tab names and in
  RELION's own order (`standard_groups`, from the extracted `tab_layout`), as
  collapsible sections. A test asserts the placement is total and unique:
  every extracted option appears in exactly one section, so no field RELION
  offers is unreachable here.
- **Inputs tab, Advanced section — what the GUI does not show.** A
  collapsible `<details>` section appended after every one of RELION's own
  groups (so it always lands past Running, and Other if the job has one).
  Collapsed by default; the frontend loads its content lazily, the first time
  it's opened (a `toggle` listener on the `<details>` element), rather than
  on every popup. Its content is command-line options the *program* accepts
  but the GUI never exposes, discovered by running the installed binary with
  `--help` (see `backend/program_help.py`), minus every flag the form above
  already covers. This is the "additional arguments" case: expert flags you
  would otherwise find in a usage dump or the source. Because the query
  includes the popup's current MPI-procs value (RELION's parallel binary can
  accept flags the serial one doesn't), re-opening it after changing MPI
  procs re-fetches rather than reusing a stale answer.

The split is by *provenance*, not by how advanced an option feels, and not by
being a separate tab: a separate tab would imply it's a peer of
Inputs/Progress/Outputs/Errors, when it is really more Inputs the GUI
doesn't have a field for.

#### RELION's Running tab

`nr_mpi`, `nr_threads` and `other_args` are added by the shared tail of
`RelionJob::initialise()` and placed by `JobWindow::setupRunTab()`, not by any
job's own `initialise<Name>Job()` or per-job window layout — so a per-job scan
missed them entirely and they were absent from every job. The extractor now
reads them, along with the per-job `has_mpi` / `has_thread` flags from
`initialise()`'s own dispatcher, so a job gets exactly the ones RELION gives it
(Import neither, Ctffind MPI only, MaskCreate threads only).

The queue-submission options from the same tab (`do_queue`, `queuename`,
`qsub`, `qsubscript`, `min_dedicated`) are deliberately **not** included:
RELION-US runs the command as a subprocess and does not reproduce RELION's
qsub path, so those controls would do nothing. `slurm/` is the cluster route.

**MPI is wrapping, not a flag.** RELION's `prepareFinalCommand()` prefixes
`$RELION_MPIRUN -n N` (default `mpirun`, `DEFAULTMPIRUN` in `pipeline_jobs.h`)
when procs > 1 and the command names an `_mpi` binary; the binary swap itself
happens in each job's own builder. Both names are extracted from that branch
(`program_guess` / `program_mpi`) rather than derived by appending `_mpi` —
they differ by more than a suffix for some jobs, and a guessed binary name is a
job that fails at launch.

**Additional arguments** are appended verbatim at the very end, unquoted,
exactly as `command += " " + joboptions["other_args"].getString();` does.

#### Browse buttons on file fields

Every `filename`/`inputnode` field (`renderField` in `app.js`) gets a Browse
button next to its text input — this is RELION's own way of marking "this
option is one file, offer a file picker" (as opposed to `text`, which
`movie_files`/`mdoc_files`/`mtf_file` in TomoImport use for the same kind of
value typed as free text with no picker). It's the same `pickFileDialog()`
server-side picker the tomogram viewer uses (see "Browse buttons" under the
viewer, below), so the backend machine's filesystem is what gets browsed, not
the browser's — and it applies regardless of file type: STAR files, MRC
maps, image stacks, FASTA sequences, checkpoints, even executables (RELION's
own `pattern` for things like `fn_ctffind_exe` is a bare `*`, so the picker
shows everything).

The one thing gating the button is `isBrowsableFilePattern()`, which exists
to skip a handful of options in `job_definitions_raw.json` that are mislabeled
`inputnode` but are actually plain numeric fields — e.g. Manualpick's
`blue_value`, whose `pattern` is literally `"0.1"` (its default value,
mis-extracted into the pattern slot). A real file pattern always either names
a glob/extension (`"*.mrc"`, `"STAR Files (*.star)"`), names one fixed file in
parentheses with no wildcard at all (`"STAR files (postprocess.star)"`, a
file a prior job produces under that exact name), or is blank (browse with no
filter, e.g. External's `fn_exe`) — so requiring the pattern to be blank or
contain `*` or `(` cleanly excludes the numeric artifacts without excluding
any genuine file field.

`extensionsFromPattern()` parses the actual extensions out of the pattern —
handling both the common `"Label (*.ext)"` / `"Label (*.{a,b})"` forms and the
handful of patterns that are a bare glob with no label or parens at all
(`"*.{mrc,gain}"`, `"*.*"`, `"ResMap*"`) — rather than hardcoding an
extension list. An empty result (no parseable extension, e.g. plain `"*"`)
means no filter: `pickFileDialog()` already treats an empty extensions list
as "show everything," which is the correct behaviour there.

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

**Source-verified flag pairings.** Most RELION options are appended as
`command += " --flag " + joboptions["key"]...`, and for ~200 of them the flag is
not `--` + key (`--i` for `input_star_mics`, `--Box` for `box`, `--j` for
`nr_threads`). The extractor records each pairing with the `if (...)` condition
guarding it (`option_flags`). The draft uses a pairing when it is unconditional,
or when the condition only tests that option's own value — RELION's common
"emit when set" guard, which the draft already implements by skipping empty
values. A pairing guarded by a *different* option is a real branch
(Topaz vs LoG picking, EM vs gradient refinement) and stays out of the draft:
emitting both halves of a branch produces a command that contradicts itself.
Parsing that condition correctly requires handling RELION's brace-less
`else if (x) command += ...;` one-liners — treating those as unconditional is
exactly how a branch-only flag lands in every draft.

One more condition is treated as satisfied rather than as a real branch:
a condition of exactly `!is_continue` (nothing combined with it — Class2D's
`nr_classes` -> `--K`, e.g.). RELION-US never models a "continue this job"
run (there is no Continue mode in this app — see the module docstring in
`job_registry.py`), so `is_continue` is always false in every draft it
builds, making a bare `!is_continue` vacuously true here. This is
deliberately narrow: `_self_guarded()` only special-cases the condition
being *exactly* that string. A condition merely containing `!is_continue`
alongside something else (`"!is_continue && else"`, seen on Class3D's/
Autorefine's `fn_ref`) is left alone, because the extra term can still guard
on a genuinely different option — confirmed against real RELION source for
Motioncorr's `fn_motioncor2_exe`, whose own condition extracts to the
identical-looking bare `"else"` but actually means "only when NOT using your
own MotionCor2 build" (`do_own_motioncor == false`), not "when this field is
set." Telling those apart requires reading the real branch, which is exactly
what `DRAFT_FLAG_MAP` is for (next section) — it's how `fn_ref`/`fn_img`
themselves get mapped despite their condition not being bare `!is_continue`.

**The `flags_used` shortcut used to bypass a real condition (fixed).** A
field is only worth calling "unconditional" if RELION's own source agrees.
`_build_draft_command()` used to check the blunt `flags_used` list (does
`"--<key>"` merely appear anywhere in the function, ignoring any `if` around
it) *before* ever consulting the condition-aware `option_flags` pairing above
— so any field whose real flag happens to equal `"--" + key` (the common
case) took that shortcut and got emitted unconditionally, even when its real
append line sits behind an `if` on a *different* option. Confirmed for real
via a full audit of every job's `option_flags` against `flags_used`: **72
fields** across the job set have this exact shape, including the one a user
actually hit — Class3D's/Autorefine's `helical_nr_asu`/`helical_twist_initial`/
`helical_rise_initial` were passed to `relion_refine` even with "Do helical
reconstruction?" unchecked, because their flags (`--helical_nr_asu`, etc.)
equal `"--" + key` and so never reached the real `do_helix`/
`do_apply_helical_symmetry` condition check at all.

Fixed by reordering: `option_flags.get(key)` is now consulted *first*,
whenever it exists, regardless of whether its flag matches `--<key>`. The
blunt `flags_used` fallback only runs when there's no `option_flags` entry
for that key at all (the extractor found no clean `command += " --flag " +
joboptions["key"]` line to read a condition from). This alone would have
just moved all 72 fields into "unmapped" — technically honest, but a
regression in usefulness for the 6-of-8 helical fields that are genuinely
resolvable, and a much bigger loss for GPU (below). Instead, a small
runtime evaluator (`_evaluate_condition()`) replays the SAME field values
RELION's own `getCommands*Job()` would read for the narrow, safely-parseable
shape this covers well: a chain of `&&`-joined clauses, each either
`joboptions["X"].getBoolean()` (optionally negated) or the `!is_continue`
invariant. This is not a guess — it's reading the identical `joboptions[...]`
values a user already submitted, the same way RELION's own C++ would. The
moment a condition contains an `||`, the literal token `else` (RELION's
brace-less "other branch" marker), a numeric/string comparison, or anything
else this evaluator doesn't recognize, it returns `None` and the field falls
back to exactly today's "unmapped" behavior — no guessing beyond what's
provably safe. A condition that evaluates to `False` is silently omitted
(RELION itself wouldn't emit it either right now), not flagged "unmapped":
there's nothing there for the user to fix. Gating-only booleans that never
become a flag on their own (`do_helix`, `do_apply_helical_symmetry`,
`do_local_search_helical_symmetry`, `use_gpu`) are added to `DRAFT_SUPPRESS`
so they don't show up as spurious "unmapped" noise either — same reasoning
as `use_direct_entries` above.

**GPU acceleration (fixed).** Reordering alone didn't fix GPU, because
`option_flags` had *no entry at all* for `gpu_ids` on any of the 6 jobs that
gate it behind "Use GPU acceleration?" (Autopick, Class2D, Inimodel, Class3D,
Autorefine, MultiBody). The reason: RELION wraps the value in escaped quotes
— `command += " --gpu \"" + joboptions["gpu_ids"].getString() + "\"";` — so
the extractor's `OPTION_FLAG_RE`, which expected the flag's string literal to
close right after the flag name, never matched (the character right after
`--gpu ` is a literal backslash, not the closing `"`). Fixed by allowing an
optional `\"` before the closing quote in the regex (see
`extract_job_definitions.py`'s `OPTION_FLAG_RE`); re-running the extractor
against the same RELION checkout picked up `gpu_ids` → `--gpu` (condition
`joboptions["use_gpu"].getBoolean()`) for exactly those 6 jobs, purely
additive — nothing else in `job_definitions_raw.json` changed. Combined with
the reordering fix above, `_evaluate_condition()` now correctly gates `--gpu`
on the checkbox.

One more piece: RELION emits `--gpu ""` (letting the job auto-allocate GPUs)
even when "Which GPUs to use" is left blank, as long as the checkbox is
checked — an intentional, meaningful empty value, unlike every other text
field this app skips when blank. `_build_draft_command()` special-cases
exactly this (`key == "gpu_ids" and flag == "--gpu"`) to pass the value
through rather than skip it. Two GPU-using jobs are deliberately **not**
fixed by this: Autopick's `--gpu` only exists inside its Topaz branch
(condition contains a bare `else`) and Motioncorr's is gated on the *other*
branch of `do_own_motioncor` (same `else` shape) — both stay honestly
unmapped rather than guessed at, consistent with the rest of this section.

**Default program.** `program_guess` skips two kinds of literal that are not
what a fresh job runs: the `_mpi` half of each `if (nr_mpi > 1)` pair, and
anything inside a continue-only branch (Autopick's first literal is
`relion_manualpick`, from its "continue manually" path).

#### Draft-command overlays for RELION-5's Python tomo tools

The `--<key>`-matches-the-flag rule holds for the ~27 core RELION programs,
whose CLI flag names equal their internal option keys. It does not apply to
RELION-5's newer **Python tomo tools** (`relion_python_tomo_import`,
`_pick`, `_denoise`, `_exclude_tilt_images`) and DynaMight, which use
hyphenated multi-word flags (`--tilt-image-movie-pattern`,
`--nominal-pixel-size`, `--output-directory`) that share no spelling with
the snake_case option key (`movie_files`, `angpix`). Two consequences shape the
current code:

1. **Flag names may contain hyphens.** `extract_job_definitions.py`'s flag
   regex is `--[A-Za-z][A-Za-z0-9_-]*`. A regex ending at the first hyphen
   (`--[A-Za-z0-9_]+`) records `--tilt`, `--nominal`, `--dose` instead of the
   real flags for these 5 jobs.

2. **`program_guess` is not reliable for branched builders.** It picks the
   first `command = "..."` literal in the job's `getCommands*Job()`, which for
   `getCommandsTomoImportJob()` is the `do_coords == true` coordinate importer
   (`relion_tomo_import_coordinates`) — even though `do_coords` **defaults to
   false** and the real default program is the SerialEM tilt-series importer
   (`relion_python_tomo_import SerialEM`).

Both are handled by a small, **source-verified data overlay** in `job_catalog.py`
(`DRAFT_PROGRAM_OVERRIDE`, `DRAFT_FLAG_MAP`, `DRAFT_SUPPRESS`), transcribed
verbatim from `getCommandsTomoImportJob()` / `getCommandsTomoExcludeTiltImagesJob()`
and cited by source line — the same "curated overlay verified against RELION
source" pattern as `JOB_DIRNAME`. A mapped flag is authoritative (always
emitted, bypassing the name-matching `flags_used` test); `DRAFT_SUPPRESS` keeps
the non-default (`do_coords`) branch's options out of the default draft. This
is deliberately **not** a reimplementation of RELION's per-job command
branching: jobs with genuinely multi-command / mode-branched builders
(`TomoPickTomograms`, `TomoDenoiseTomograms`) are left as program-name-only
drafts with every field flagged unmapped and the real source shown, rather
than risk a subtly-wrong reconstruction. `TomoImport` and
`TomoExcludeTiltImages`, whose default builders are single clean commands, are
drafted in full.

#### Draft-command overlay for tomography's shared "optimisation set" input group

Seven jobs (`Inimodel`, `Class3D`, `Autorefine`, `TomoSubtomo`,
`TomoCtfRefine`, `TomoAlign`, `TomoReconPart`) share one input group for their
tomo-mode particle/reference data: `in_optimisation` (an optimisation-set
STAR file bundling everything) as an alternative to filling in
`in_particles` / `in_tomograms` / `in_trajectories` directly, toggled by
"OR: use direct entries?" (`use_direct_entries`). RELION builds these flags
in a shared helper, `RelionJob::getTomoInputCommmand()`
(`src/pipeline_jobs.cpp` ~6328-6430), called from each job's own
`getCommands*Job()` rather than inlined there — so the extractor's
`commands_source`/`flags_used` (which only capture the calling function's own
body) never see a flag for any of these four keys, and the generic rule
silently dropped whichever one the user filled in, no matter which job or
which of the two input modes. `fn_ref` and `fn_img` (the classic-SPA
counterpart of the same "which particles/reference" input, on the same four
non-Tomo-only jobs) hit the same failure for an unrelated reason: their real
flags (`--ref`, `--i`) are extracted correctly into `option_flags`, but their
condition text is `"!is_continue && else"` / `"!is_continue && else && else"`
— not the bare `!is_continue` the self-guard special-case above allows — so
they were rejected as an unverified branch too.

All of these are now curated `DRAFT_FLAG_MAP`/`DRAFT_SUPPRESS` entries in
`job_catalog.py`, verified against `getTomoInputCommmand()` directly rather
than the calling job's own source. The flag differs by whether the caller
passes `is_for_refine=true` (Inimodel/Class3D/Autorefine) or `false`
(the four Tomo* jobs):

| field | refine callers | non-refine callers |
|---|---|---|
| `in_optimisation` | `--ios` | `--i` |
| `in_particles` | `--i` | `--p` |
| `in_tomograms` | `--tomograms` | `--t` |
| `in_trajectories` | `--trajectories` | `--mot` |

Mapping every key unconditionally (rather than branching on
`use_direct_entries` in code) is safe: RELION's own GUI presents
`in_optimisation` and the `in_particles`/`in_tomograms`/`in_trajectories`
trio as mutually exclusive, both default to empty, and the draft already
skips empty values — so whichever mode the user isn't using stays absent from
the command on its own. `in_particles` sharing `--i` with `fn_img` (the
classic-SPA field on the same job) is safe for the identical reason: a job is
filled in as either SPA or tomo, never both. `use_direct_entries` itself is
`DRAFT_SUPPRESS`-ed on all seven jobs — it never becomes a flag on its own,
only chooses which of the two branches above applies.

#### Execution model: run from the project root (matches RELION)

The runner executes the approved command with `cwd` set to the **project
root**, exactly like RELION — so project-root-relative inputs (`frames/*.mrc`)
and the command's `--o <JobDir>/jobNNN/` output path resolve the same way
RELION's own GUI resolves them. To make that work, the draft includes the
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

#### Output-value suffix: `--o <JobDir>/jobNNN/` isn't always a bare directory

For most jobs, RELION's `--o`/`--output-directory` value is the bare job
directory — which is what the draft above already emits. But several jobs
append a literal suffix to `outputname` to form a **file rootname prefix**,
not just a folder, confirmed by reading each job's `getCommands*Job()` in
`src/pipeline_jobs.cpp`. Missing this used to produce output files like
`_it000_class001.mrc` instead of `run_it000_class001.mrc` — RELION-US always
emitted the bare directory, for every job, unconditionally.

The five classic iterative-refinement jobs set `fn_run = "run"` in their
DEFAULT (non-continuation) branch — RELION-US never models a "continue this
job" run (see the `!is_continue` note above), so "run" is always correct:
`Class2D` (~3183), `Inimodel` (~3466), `Class3D` (~3860), `Autorefine`
(~4351), `MultiBody` (~4736-4744, the `else`/non-continue branch). Two more
jobs append a fixed, unconditional literal, verified by reading each function
in full: `Maskcreate` → `"mask.mrc"` (~4942), `Postprocess` → `"postprocess"`
(~5340).

These seven are a curated table, `job_catalog.DRAFT_OUTPUT_SUFFIX` (consumed
via `draft_output_suffix()` in `_build_draft_command()`), appended after the
subdir whenever one is defined. Deliberately **not** included, because their
suffix is mode-branched rather than a single safe default: `Joinstar`
(depends on which of `fn_part`/`fn_mic`/`fn_mov` is filled in, ~5069-5137),
`Localres` (only appends `"relion"` in the `do_relion_locres` branch — the
default ResMap branch uses a different program entirely, ~5510), `Select`
(the `class_ranker` branch appends bare `outputname` plus two *extra* fixed
flags, not a suffix change, ~2926), and MultiBody's second "analyse" command
(conditional on further GUI state, not reproduced — left for hand-editing).

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

**Recent projects.** Every successful open, switch or init records the
directory in a per-*user* cache (`project_manager.remember_project()`), which
the Change Project dialog shows as a quick-switch list. Three deliberate
choices:

- It lives under `XDG_CONFIG_HOME`/`~/.config/relion_us/`, not in a project's
  `.relion_us/` marker: the list has to outlive any single project and survive
  switching away from one. On a shared cluster filesystem that also means each
  user gets their own list rather than their group's.
- `exists` and `is_project` are **recomputed on every read**, never cached. A
  folder can be deleted, or become a real RELION project the first time RELION
  writes `default_pipeline.star` into it, with this app uninvolved. A stale
  entry is returned flagged rather than dropped, so a project the user
  remembers doesn't silently vanish from the list.
- Paths are resolved before comparing, so the same directory reached by a
  different route is one entry, not several.

Writing the cache never raises: a read-only or full home directory must not
stop someone opening a project, which is the task they actually asked for.

### Adopting a project RELION's own GUI built

`default_pipeline.star` is read (never written) so an existing project opens as
a continuation rather than a blank slate — see
`project_manager.read_relion_pipeline()`.

- **Numbering.** `job_runner._next_job_number()` takes the max of this app's own
  history, its in-memory runs, and RELION's own numbers
  (`rlnPipeLineJobCounter - 1` plus every process's number), then skips forward
  past any job directory already on disk. Without this the app restarted at
  job001 in a project already at job012 and drafted `--o` into existing results.
  The counter is the number RELION would hand out *next*, so everything below it
  is spoken for — including jobs deleted from the pipeline whose directories
  survive.
- **Command Center import.** `_relion_pipeline_entries()` turns each process
  into a row with `source: "relion"`. Sorting is by job number, not timestamp:
  imported jobs have none, and a project's counter only ever increases, so the
  number is the one chronological key that works across both tools.
- **Reopening.** `read_relion_job_options()` reads the job's own `job.star`
  (`joboptions_values`: `rlnJobOptionVariable` / `rlnJobOptionValue`), whose
  keys are the same option keys these forms use. Values are merged *over* the
  job type's defaults, so an option RELION's file doesn't mention still gets a
  sane value.
- **Read-only.** Abort / resume / delete / status changes are refused on
  imported jobs, in the API (`_reject_relion_run`, 409) as well as the UI:
  this app has no `relion_pipeliner` verb that would let it keep RELION's own
  record consistent afterwards, so acting on a job RELION owns would leave that
  file describing something untrue. Two deliberate carve-outs: **alias and note
  edits** are allowed, since both live in `.relion_us/` and never reach
  RELION's pipeline at all; and **Overwrite** is allowed when pipeline sync is
  on, because the overwrite branch reuses the same process row via
  `set_process_status` (matched by directory, not by who registered it). It is
  still refused with sync off, where there is no way to update RELION's record.
  Browsing outputs and reading progress are always fine — those only read the
  job's directory.
- **Run ids carry the job number, not the directory.** `relion:job005`, not
  `relion:Class2D/job005`: an encoded `/` in a URL path segment is rejected
  before the route matches, and RELION's numbering is project-wide unique
  anyway.

Two non-obvious parsing details, both found by testing against a realistic
project rather than a hand-made one:

- `pipeline_general` is a STAR **list** block, which `starfile` returns as a
  plain dict rather than a DataFrame — code that assumes the DataFrame API
  silently reads no counter at all.
- RELION appends a sub-label to a job's type for many jobs (`label += ".movies"`,
  `".em"`, `".topaz"` — 35 sites in `pipeline_jobs.cpp`), so a real project
  records `relion.class2d.em` where `job_catalog` holds `relion.class2d`.
  `internal_name_for_label()` matches on the longest base prefix. The same bug
  had been quietly disabling the SPA/Tomo auto-detect on every real project.

### Two-way pipeline sync (`pipeline_bridge.py`)

Adoption (above) is read-only: it lets RELION-US see a project RELION's GUI
built, but a job run in RELION-US still didn't exist as far as
`default_pipeline.star` was concerned. `pipeline_bridge.py` closes that loop,
governed by a per-project, **on-by-default** setting
(`project_manager.pipeline_sync_setting()` / `set_pipeline_sync()`, stored in
`.relion_us/settings.json`), toggled via **⇄ RELION sync** in the top bar. Both
GUIs staying interoperable out of the box is worth more than the narrow
"opened a colleague's project, didn't mean to touch its pipeline" case an
off-by-default would guard against — and that case is still one click away, per
project. `relion_pipeliner` not being on `PATH` independently makes this a
no-op regardless of the setting.

The standing rule from adoption still holds almost everywhere: **this app does
not compute or rewrite `default_pipeline.star`'s contents.** It delegates to
RELION's own `relion_pipeliner` binary, the same program RELION's own GUI
shells out to internally. The two narrow exceptions — a fixed empty skeleton
for a project that has no pipeline file yet (`_ensure_pipeline_bootstrapped`,
because `relion_pipeliner` cannot create one without orphaning the lock), and a
single status token to mark a process "Running" (`set_process_status`, because
`--check_job_completion` only promotes processes already in that state) — are
each documented in full in `pipeline_bridge.py`'s own module docstring. Neither
touches the node, edge, or process tables that RELION's command-building logic
is the authority for:

- `write_job_star()` builds a `job.star` — `data_job` (`_rlnJobTypeLabel`,
  `_rlnJobIsContinue`, `_rlnJobIsTomo`) plus a `data_joboptions_values` loop —
  from the job's field values, using the same booleans-as-`"Yes"/"No"`
  convention as RELION's `JobOption::getBoolean()`.
- `register_job()` writes that to a temp file and calls
  `relion_pipeliner --addJobFromStar <path> [--setJobAlias <alias>]` with
  `cwd` set to the project directory. That binary — not this code — decides
  the job number, creates `<JobDir>/jobNNN/`, computes the input/output node
  graph by actually running the job's real command-builder, and appends the
  process to the pipeline. `register_job()` diffs the pipeline before/after
  the call to find which process is new, and returns its process name and job
  number.
- `job_runner.start_subprocess_job()` calls this *before* picking an output
  directory. If registration succeeds, RELION's allocated
  `<JobDir>/jobNNN` becomes authoritative — the draft command's `--o` is
  rewritten to match if this app's own proposed number differed (possible if
  RELION's GUI created a job in the same project in between). If registration
  fails (`PipelineBridgeError` — binary missing, timeout, non-zero exit), the
  run falls back to this app's own numbering, exactly as if sync were off; the
  job still runs, it just isn't in RELION's pipeline.
- `pipeline_control_args()` appends `--pipeline_control <job_dir>/` (or the
  hyphenated `--pipeline-control` some of RELION's newer Python tomo tools
  expect) to the command, mirroring `RelionJob::prepareFinalCommand()`. This is
  what makes the running program write a `RELION_JOB_EXIT_*` file into its own
  job directory on exit — RELION's completion signal, not this app's.
- On completion, `job_runner._run_subprocess()`'s `finally` block calls
  `relion_pipeliner --check_job_completion`, which reads any
  `RELION_JOB_EXIT_SUCCESS` / `_FAILURE` / `_ABORTED` file and flips that
  process's status in the pipeline immediately, rather than waiting for
  RELION's own GUI to poll and notice.
- Every `relion_pipeliner` call runs off the event loop
  (`asyncio.to_thread`) because it can block for a while: `PipeLine::read()`
  waits on the project's `.relion_lock` mutex — an atomic-mkdir lock RELION's
  own GUI also takes — retrying for up to a minute before giving up.
  `pipeline_bridge.py` sets a 120s subprocess timeout, comfortably above that.
- Custom jobs (the four converters) are never registered — they have no
  RELION job type label to write into `job.star`, and `_register_in_relion_pipeline`
  short-circuits on an unknown `internal_name`.

**Overwrite is a different case from a fresh run.** RELION's own Overwrite
(`gui_mainwindow.cpp`'s `cb_toggle_overwrite_continue`) reuses the job's
*existing* pipeline entry — it never adds a new one. `start_subprocess_job()`'s
`overwrite_run_id` branch mirrors that: it never calls
`_register_in_relion_pipeline()` (which always allocates a *new* number via
`--addJobFromStar`, the wrong semantics for reusing a slot). It does still,
when sync is on, (a) apply `pipeline_control_args()` so the re-run's
`relion_` binary writes the exit-status file `--check_job_completion` reads —
this used to be skipped for every Overwrite, which is why an overwritten job
could sit stuck in RELION's own GUI — and (b) defensively re-verify the
command's `--o` path against the run's actual `cwd` the same way a fresh
run's stale prospective number gets corrected, since Overwrite trusts the
(user-editable) command box's existing text rather than rebuilding it.

**`run.out` / `run.err`.** RELION's own GUI always tees a job's stdout/stderr
into these two files inside the job directory — not something the RELION
binaries write themselves, but shell redirection RELION's GUI appends to the
command before running it (`RelionJob::prepareFinalCommand`,
`src/pipeline_jobs.cpp` ~line 760: `one_command += " >> " + outputname +
"run.out 2>> " + outputname + "run.err";`, unconditional whenever the command
doesn't already contain a redirect). RELION-US streams stdout/stderr live
over a websocket via asyncio pipes instead of shell-redirecting the command
itself (which would swallow that live view) — `job_runner._run_subprocess()`
now tees each line to `run.out`/`run.err` as it's read, in append mode to
match RELION's own `>>` (so an Overwrite's output accumulates on top of the
previous attempt's, not replaces it). Best-effort: a logging failure never
takes down the job itself.

**A synced job used to appear twice in the Command Center.** `list_runs()`
merges three sources — RELION's own `default_pipeline.star` (read-only
placeholders, `_relion_pipeline_entries()`), this app's persisted history,
and any still-tracked in-memory runs — into one dict keyed by `run_id`. Once
a job started here is *also* registered with RELION's pipeline, the same
job exists under two different `run_id`s: this app's own (a uuid) and the
synthetic `"relion:jobNNN"` placeholder `_relion_pipeline_entries()`
generates from every row in `default_pipeline.star`, unconditionally. Keyed
by `run_id`, those never collide, so the job doubled — a live repro (10 jobs,
sync on) produced 20 Command Center rows, half of them blank
`source: "relion"` placeholders sitting right next to this app's own richer
entry for the identical job. Fixed by tracking which job numbers this app
already has its own record for, and skipping the RELION-side placeholder for
any of them — `_relion_pipeline_entries()`'s placeholders now only surface
jobs genuinely run outside this app entirely (a legacy project adopted from
disk, or a job launched from RELION's own GUI), which is what they were
always meant to represent.

`backend/tests/fake_relion_pipeliner.py` stands in for the real binary in
tests — it implements just the two subcommands above against a simplified
pipeline format, explicitly documented in its own docstring as a test double
rather than a reimplementation (it doesn't compute the real node graph or take
the lock). `test_pipeline_bridge.py` covers job.star writing, registration
(numbering, alias, unknown job type, missing binary), completion status
flips, the per-project setting, and the runner's fallback behavior when
`PipelineBridgeError` is raised.

### SPA / Tomo / All jobs-list toggle

The Jobs sidebar has a three-way toggle above the search box to declutter
the list — "SPA", "Tomo", "All" — for users who only work in one pipeline
day to day. **It is a display filter only.** It never restricts which jobs
can be opened or run: a non-empty search always searches the full 35-job
catalog regardless of the toggle (see `applyJobFilters()` in
`frontend/app.js`), so every job stays one search away no matter what's
selected. "All" shows the unfiltered catalog.

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

**Auto-switching based on the loaded project** works when there's a signal
to use:
`project_manager.detect_pipeline_hint()` reads `default_pipeline.star`'s
`pipeline_processes` block (via the same `starfile` wrapper `star_io.py`
already uses) and checks which known SPA-only/Tomo-only
`rlnPipeLineProcessTypeLabel` values that project has actually run,
returning `'spa'`, `'tomo'`, `'mixed'`, or `'unknown'`. `GET /api/project`
exposes this as `pipeline_hint`; the frontend auto-applies it on project
load/switch only when it's unambiguous (`'spa'` or `'tomo'`) — a brand-new
project (`'unknown'`, no `default_pipeline.star` yet) or one that's run
both types (`'mixed'`) leaves the toggle wherever it was, falling back to a
manual switch. The user's last manual choice also persists across reloads via
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

## Format bridging

RELION-5's tomography STAR schema (`rlnTomoName`, `rlnCoordinateX/Y/Z`,
`rlnTomoParticleId`, per-tomogram optics groups, etc.) is documented in the
RELION-5 tomography paper and the ReadTheDocs pages linked below, and
`star_io.py` targets that. Warp/M's and DeepETPicker's output column names
drift between versions and installs, so `warp_bridge.py` and
`deepetpicker_bridge.py` keep the field mapping isolated in one place
(`warp_bridge.DEFAULT_COLUMN_MAP`) rather than hard-coding names that can't
be verified against the version in front of them.

### Format facts each bridge depends on

Each of these was checked against the upstream tool's own documentation or
source. They are the assumptions that break if an upstream format changes, so
they are worth re-checking after a major version bump of any of these tools.

- **IMOD** (bio3d.colorado.edu/imod/doc). `.xf` is one line per tilt image,
  `A11 A12 A21 A22 DX DY`; `.tlt` is one angle in degrees per line, same image
  order. `model2point`/`point2model` are PIP programs accepting `-input` /
  `-output`, plus `-scat` for scattered points; default `model2point` output
  is `X Y Z` only, which is what `model_to_coordinates` parses. IMOD model
  coordinates are 0-based pixels and the Z value carries a −0.5 half-pixel
  offset.

  **Axis caveat.** IMOD tomograms exist in "rotated" (depth = Z, what RELION
  expects) and "flipped" (`trimvol -yz`, Y↔Z swapped, handedness inverted)
  orientations, and a model built on a raw `tilt` reconstruction has depth in
  Y. The bridge copies X, Y, Z verbatim and does not try to infer which one
  it was given — see `Swap Y and Z` below.

- **DeepETPicker** (github.com/cbmi-group/DeepETPicker). `.coords` is
  `class_id x y z`, four whitespace-separated columns, in voxels of the
  tomogram it was run on. Its own `utils/coords_to_relion4.py` also accepts a
  bare three-column `x y z` file (class_id defaults to 1), so `read_coords`
  accepts both rather than being stricter than the tool it reads.

- **Warp/M** (warpem.github.io). Two distinct export paths:
  `ts_export_particles` (Warp 2.0 / WarpTools) already writes a RELION-5
  optimisation set with native `rln*` columns and needs no bridge at all,
  while `.tomostar` and older particle exports use Warp's `wrp*` columns and
  do need mapping. Warp also separates reconstruction pixel size (`--angpix`)
  from export pixel size (`--output_angpix`); getting that wrong parses fine
  and puts particles in the wrong place.

- **AreTomo2** (`aretomo_bridge.py`). The `.aln` global block is
  `SEC ROT GMAG TX TY SMEAN SFIT SCALE BASE TILT` (verified against the
  AreTomo manual and the teamtomo/alnfile parser). SEC is a 0-based index into
  the post-dark-removal stack; TX/TY are pixels of the aligned stack, and the
  `.aln` records no pixel size. Only ROT/TX/TY/TILT/SEC are consumed — the
  remaining columns are per-section fit metrics that carry no geometry.

  The bridge writes IMOD `.xf` + `.tlt` and hands off to RELION's IMOD
  tilt-series import rather than writing RELION's tilt-series STAR directly.
  The `.xf` mapping (`θ = −ROT`, shift negated and rotated into the
  transformed frame) has two independent corroborations — AreTomo's own
  `-OutImod` export and teamtomo/alnfile — so it is the better-verified
  route. Writing `rlnTomoZRot`/`rlnTomoXShiftAngst` directly would need a
  sign convention this bridge has no second source to check against.

### Coordinate transforms

`coord_transform.py` holds one implementation of the axis operations the
coordinate importers need, so IMOD and DeepETPicker cannot drift apart:

- **Swap Y/Z** — the fix for an IMOD flipped or raw-`tilt` tomogram (above).
- **Mirror an axis** — reflection about the volume centre for 0-based
  coordinates, `(size − 1) − coord`. Note the `−1`: a plain `size − coord`
  sends coordinate 0 to `size`, one voxel outside the volume, and shifts every
  coordinate by a full voxel.

A mirror requested without the corresponding tomogram dimension raises rather
than guessing, following the same rule as everywhere else here: fail loudly
rather than emit plausible-looking wrong coordinates.

There is no contrast-inversion option, because these are coordinate and
alignment importers — there is no density to invert. It would belong on a
tomogram/map importer.

### Command Center lineage

`list_runs` attaches `input_links` to every run — `[{path, run_id, job_name}]`
— from two different sources depending on who ran the job, merged into the
same shape so the timeline's "↳ from jobNNN" chips and the network view (next
section) work identically regardless of source:

- **This app's own runs**: `_attach_input_lineage` (`job_runner.py`) —
  best-effort attribution from file paths. Each run's `detected_inputs` (file
  paths gathered from its field values by `_detect_inputs`) are matched
  against every other run's own output directory; a file living under an
  earlier job's directory is attributed to that job. This is a display
  convenience, not RELION's real computed graph — a file that merely lives
  under a job's output dir is attributed to it whether or not that job
  actually produced it.
- **Jobs RELION itself ran**: `_relion_pipeline_entries` (`job_runner.py`),
  from `read_relion_pipeline`'s `producers` map (`project_manager.py`) — see
  below. This *is* RELION's real computed graph, not a guess, because
  `_detect_inputs` never runs on a RELION-imported job (its `detected_inputs`
  is hardcoded `[]`); without reading RELION's own edge tables, a project
  built entirely in RELION's GUI would show zero lineage anywhere in the
  Command Center, in exactly the project where showing it matters most.

`read_relion_pipeline`'s `producers: {process_name: [producer_process_name]}`
comes from chaining two of `default_pipeline.star`'s five tables: first
`pipeline_output_edges` (`process -> node`) builds `node_producer: {node:
process}` — which process wrote each named output file — then
`pipeline_input_edges` (`node -> process`) is walked and each edge's `node`
looked up in `node_producer` to find that consumer's own producer process.
Both tables were verified against `PipeLine::write()` (`src/pipeliner.cpp`,
same source as `pipeline_general`/`pipeline_processes` — see "Adopting a
project" above): RELION computes them from each job's own real
`getCommands<Job>Job()` (`inputNodes`/`outputNodes`), so this reads RELION's
actual answer for "what fed what" rather than reconstructing it.

### Command Center network view

`renderNetwork()` (`app.js`) draws `ccRuns`' `input_links` as a lineage DAG:
oldest jobs at the top, every job directly beneath every job it took input
from, connected by a curved branch. Layout is two passes over plain arrays —
no graph/diagramming library, to stay consistent with the no-CDN,
vendored-only frontend:

1. **Row** (`computeLineageRows`): each run's row = 1 + the deepest row among
   its parents, roots (no tracked parents) at row 0 — a small memoized
   recursive walk over `parentsOf`, with a cycle guard that can't actually
   trigger against a real pipeline but keeps a malformed one from hanging the
   tab.
2. **Column**, assigned row by row top-down: within a row, runs are ordered
   by the *average column of their already-placed parents* (falling back to
   job number for row 0, or when no parent has a column yet), so a branch
   renders near its source instead of at an arbitrary position — this is
   what keeps job011/job012 directly under job010 rather than scattered
   across the row.

Pixel coordinates for the SVG connectors are never computed by hand — after
the DOM lays out (rows/nodes are plain flexbox), each node's position is read
back via `offsetLeft`/`offsetTop`/`offsetWidth`/`offsetHeight` relative to
`#ccNetworkRows` (the nearest `position: relative` ancestor, so these are
already in the right coordinate space with no manual scroll-offset
arithmetic), and each edge is a cubic Bézier between whichever of the two
nodes is visually higher and whichever is lower (by `offsetTop`, not by
which one is the "parent" — see "Newest/oldest at the top" below for why
that distinction matters), bottom-center of the upper one to top-center of
the lower one. This stays correct regardless of how wide any job's name or
type text renders, at the cost of one forced-reflow read after layout —
negligible at Command Center row counts.

The `.hidden` class on `#ccNetworkView` is removed *before* `renderNetwork()`
runs (`renderCommandCenterViews`'s toggle-then-render order): `offsetLeft`/
`offsetTop` all read 0 on a `display: none` ancestor, so measuring while
still hidden would silently produce a graph with every edge collapsed to a
single point.

The overlay `<svg id="ccNetworkEdges">` is a sibling of `#ccNetworkRows`,
both children of `#ccNetworkCanvas` — `position: absolute; inset: 0` on the
SVG is what makes it cover exactly the same box `#ccNetworkRows` occupies.
That only holds because `#ccNetworkCanvas` itself carries no padding: `inset:
0` on an absolutely positioned element is measured from its containing
block's *padding* edge, which ignores that block's own padding, so any
padding on `#ccNetworkCanvas` would put the SVG out of alignment with
`#ccNetworkRows` (an ordinary flow child, which padding *does* push in) by
that same amount. The view's breathing-room padding lives on `#ccNetworkView`
(the scrolling viewport) instead. `test_legacy_project.py`'s and
`test_network_branching.py`'s `edges_touch_nodes()` checks compare real
`getBoundingClientRect()` pixels rather than `offsetTop`/`offsetLeft` against
the SVG path's own `d` coordinates, since the latter is tautological —
`renderNetwork()` computes one from the other, so that comparison can only
catch it disagreeing with itself, never the overlay disagreeing with the
screen.

**Keeping edges glued to their boxes.** Edges are a one-time read of the
DOM's layout, not a live binding — so anything that moves the boxes without
changing `ccRuns` has to explicitly trigger a re-render, or the lines stay
drawn at their old coordinates while the boxes move out from under them.
Two things move the boxes without RELION-US knowing at the call site:
closing/opening the Jobs sidebar (`#sidebar` has a `.15s` CSS
`transition: margin-left`, so the canvas keeps resizing for a beat after the
click) and a browser window resize. Both are covered by one mechanism —
`ensureNetworkResizeObserver()` attaches a `ResizeObserver` to
`#ccNetworkCanvas`, which stretches to fill `#ccNetworkView`'s available
width (`min-width: 100%` in style.css) and so resizes on either cause,
`renderNetwork()`-ing again (debounced to one `requestAnimationFrame`) each
time it fires, so it keeps landing on the correct layout for the sidebar's
whole transition rather than a mid-transition snapshot. The sidebar toggle
handler also calls `renderNetwork()` directly, belt-and-suspenders, so the
boxes and lines never visibly disagree even for a frame.

**Newest/oldest at the top.** A second, independent direction toggle from
the Timeline's (`ccNetworkDirection`, its own `localStorage` key) — sharing
the Timeline's would have meant Network inherited the Timeline's
newest-first default the first time someone opened it, when the whole point
of this view is oldest-at-top, branching down to what used it. The shared
`#ccDirectionBtn` (only one of Timeline/Network is ever visible at once, so
one button serves both) just reverses which end of the already-computed
`rows` array is appended first; row/column assignment itself never changes.
Because edge attachment (above) is by on-screen position rather than by
parent/child, the lines don't need to know direction flipped at all. The
button's label always states the *current* setting rather than the action a
click performs — `"Newest first ↑"` / `"Oldest first ↓"` (no leading verb
like "Sort:"; a bare imperative reads as an action to take, exactly the
ambiguity the label exists to avoid), the same state-vs-action split as
`themeBtn` (label = current state, `title` = what clicking does). The arrow
follows the flow of time rather than which end of the list is on top in a
given view: newest-first points up (further back in time as you read down),
oldest-first points down (forward in time as you read down) — consistent
between Timeline and Network even though the two views put opposite ends on
top by default.

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
  and returns a PNG. The volume is never loaded whole.
- **Three linked orthogonal views**, matching DeepETPicker's tri-view: XY as
  the large main panel, ZY to its left, XZ below it. All three are cuts through
  one crosshair position (`state.x/y/z`), so a click in any panel moves the
  other two, and a wheel over a panel steps along that panel's own axis. Each
  panel's plane, slice axis and screen->voxel mapping live in one `PANELS`
  table in `app.js` rather than in three parallel branches — the click,
  crosshair and pick maths all read from it.
  - **Only the panels that moved are refetched.** A click in XY changes x and
    y, which changes the ZY and XZ cuts but not XY's own; refetching all three
    would triple the mmap+PNG work for no visible change.
  - **One isotropic scale for all three panels** (`layoutStage()`), computed
    from the volume dims, with panel sizes set in pixels rather than `fr`
    units. The panels have to match to the pixel or the crosshair does not line
    up across their borders, and a side view stretched to fill its box would
    misrepresent the volume's aspect.
  - **The ZY panel is served transposed** (`GET /api/viz/slice?transpose=1`).
    The natural x-axis slice is `[z, y]`; the left-hand panel needs Y running
    vertically so it shares the main panel's vertical axis. Transposing the
    small 2D array server-side is cheaper and less error-prone than rotating
    the PNG and its overlay canvas in the browser.
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
  offers **Load anyway / Reload files / Cancel** — "load anyway" exists so a
  legitimate naming difference isn't a dead end. Cancel loads the volume
  without the mismatched picks.
- **Safety.** Every path is resolved against the active project directory and
  must stay inside it; the viewer only ever reads.
- **Layout.** The three views take the whole left side; every input and control
  lives in a fixed-width rail on the right, with compact inputs and an icon-only
  browse button. The images are the reason the window is open, so the controls
  are sized not to compete with them.
- **Browse buttons.** Both inputs have a server-side file picker
  (`pickFileDialog()` in `app.js`) rather than an `<input type="file">` — the
  backend often runs on a different machine than the browser (an HPC login
  node), so the browser's own filesystem is the wrong one. It reuses the
  existing `POST /api/project/browse` endpoint, which already returns files
  alongside folders, filters by the extensions `viz.py` accepts, resumes in the
  folder the field currently points at, and returns a project-relative path
  (what the viewer's API expects, and the idiom RELION itself stores).

Endpoints: `POST /api/viz/inspect`, `GET /api/viz/volume-info`,
`GET /api/viz/slice` (`?transpose`), `POST /api/viz/picks`. New deps: `mrcfile`, `pillow`.

## Invariants worth preserving

Non-obvious rules the code depends on. Each one guards a failure mode that
produces *plausible-looking but wrong* results rather than an error, so they are
easy to undo accidentally during a refactor.

**Data correctness**

- **Tomogram-name matching is boundary-aware, never a bare substring test.**
  `viz._names_match()` compares filename stems and accepts a substring only at a
  separator boundary, because `TS_1`/`TS_10`/`TS_11` naming is normal and a bare
  `in` test would overlay one tomogram's particles onto another.
- **A no-match returns no picks.** If no `rlnTomoName` matches the loaded
  tomogram, `load_picks` returns an empty list rather than falling back to the
  whole table — every tomogram's particles drawn on one tomogram is visually
  indistinguishable from a correct overlay.
- **The axis mirror is `(size - 1) - coord`.** Reflection about the centre of a
  0-based axis. `size - coord` sends voxel 0 to `size`, one voxel outside the
  volume. Code, docstrings, field help and tests all state the same formula.
- **Contrast percentiles use `nanpercentile` plus an explicit finite check.**
  NaN comparisons are always False, so a plain `hi <= lo` guard does not fire on
  a NaN-containing volume and the slice renders all black.
- **`aln_to_imod` refuses to write on a row-count mismatch.** IMOD and RELION
  pair `.xf` line N with stack image N positionally, so one dropped `.aln` row
  mis-pairs every subsequent transform. The row count is cross-checked against
  the header's `RawSize` minus dark frames.
- **Every path is resolved against the project directory and must stay inside
  it** — in the viewer, the file picker, and the custom-job runners alike.
- **The recent-projects cache stores paths only, and recomputes
  `exists`/`is_project` on every read.** Caching those flags would show a
  deleted project as live, or a real RELION project as "not a project".

**Job execution**

- **Jobs run from the project root**, with project-root-relative paths and
  `--o <JobDir>/jobNNN/`, matching RELION's own contract (see *Execution model*).
- **Custom-job runners write into their job directory**, never the project root:
  `custom_jobs._resolve_out()` takes the job dir. Outputs written elsewhere are
  invisible to the Outputs tab, Clean and Delete, and successive imports would
  silently share one `particles.star`.
- **Importers that overwrite an existing file back it up first**, and the backup
  never clobbers an earlier one — it falls back to `.bak2`, `.bak3`, … so a
  second run cannot destroy a hand-curated original.
- **Subprocesses are launched in their own session and killed as a group**
  (`start_new_session=True` + `os.killpg`). RELION jobs spawn MPI children; a
  bare `terminate()` on the shell leaves them running.
- **Abort is honoured before the process exists.** `abort_run` accepts `pending`
  runs and sets `abort_requested`, which the launcher checks and declines to
  spawn — otherwise cancelling mid-spawn orphans a process group.
- **`_run_subprocess` reaps in a `finally`.** A raising output pump would
  otherwise strand the run as "running" forever and leak its sibling task.
- **The run websocket races a reader task against the output queue.** Starlette
  surfaces a disconnect only from `receive()`, never `send()`, so a
  send-only loop parks on `queue.get()` forever and leaks a subscriber per popup.
- **`StarDocument.write()` always emits the block name**, including for
  single-block files — an anonymous `data_` header can't be looked up by name in
  RELION-5.

**Performance premises**

- **The visualizer never loads a volume whole.** `mrcfile.mmap` plus a strided
  in-plane sample for the contrast estimate; fancy-indexing full-resolution
  slices would pull gigabytes for an unbinned 4096² tomogram.
- **`job_definitions_raw.json` (~500 KB) is `lru_cache`d.** It is consulted on
  every job open *and* every draft recompute, and drafts recompute as the user
  types.
- **Slice and contrast sliders debounce.** Each change is a server-side
  mmap + PNG encode; an undebounced drag fires ~60 per second.
- **Table work is vectorized, not `.iterrows()`** — pick tables reach 10⁵ rows.

**Frontend rules**

- **No native `alert()`/`confirm()`.** They block the page (including the
  Playwright suites); use `errorDialog()` / the custom modal helpers.
- **Run disables itself once a run exists**, so a double-click can't start a
  second job and orphan the first websocket.
- **Destructive actions close their popup only after the request succeeds**, so
  a failure doesn't discard the user's edited command.
- **The auto-detected pipeline hint is not persisted** — it shares a
  `localStorage` key with the user's explicit SPA/Tomo/All choice and would
  overwrite it.

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

**Cost.** The feature is bounded in both memory and storage:

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

## Password protection

Off by default, and deliberately not real security — see `backend/auth.py`'s
module docstring for the full threat-model reasoning (no TLS is set up
here, so the password crosses the network in plain text like everything
else this app sends; this is a deterrent against casual access on a shared
lab/cluster network, not a hardened login).

**Storage.** `project_manager.config_root() / "auth.json"` — per-*user*,
alongside the recent-projects cache (`project_manager.recents_path()`; both
now go through the shared `config_root()` helper), not per-project, since
the whole point is protecting the app before it even shows a project. Only
a salted PBKDF2-SHA256 hash is stored (stdlib `hashlib.pbkdf2_hmac`, no new
dependency for bcrypt/argon2 — the iteration count is tuned for "a real cost
per guess," not for defending a password worth targeting with a GPU), never
the password itself, and the file is chmod'd `0600` best-effort on save.

**Sessions are stateless**, not a server-side table: the cookie is
`{expiry}.{HMAC-SHA256(expiry, session_secret)}`, so validity is just
recomputing the HMAC and comparing (`hmac.compare_digest`) plus an expiry
check — no session store to leak, clean up, or lose on a backend restart. A
random `session_secret` is stored alongside the password hash and rotated
on every `set_password()` call, which is what invalidates *every* existing
session everywhere at once on a password change, without tracking them
individually.

**Enforcement, two layers, because one doesn't reach everything:**
- `@app.middleware("http")`'s `auth_gate` (`main.py`) covers every HTTP
  request — page loads, static assets, all `/api/*` routes. A handful of
  paths are exempt so there's somewhere to log in from at all:
  `/login.html`, `/api/auth/status`, `/api/auth/login`, `/favicon.ico`.
  Unauthenticated API/websocket-prefixed paths (`/api/*`, `/ws/*`) get a
  plain 401; anything else (a page, a static asset) gets a 302 to
  `/login.html`, since without a valid session nothing should render at all.
- The `/ws/runs/{run_id}` websocket is checked separately, before
  `.accept()`, because Starlette routes a "websocket"-scope connection
  around `@app.middleware("http")` entirely — that decorator only ever sees
  "http"-scope requests, so an unprotected websocket would have been the one
  hole in an otherwise fully gated app. Closing before `.accept()` (rather
  than accepting then closing) is what makes Starlette reject the connection
  at the HTTP-upgrade handshake itself (a clean `403`, confirmed against a
  real client in testing) instead of completing the handshake and closing
  a second later.

**The CLI is the only way in or out of this.** There is deliberately no
in-browser "change password" or "turn this on" control — anyone who can
already reach a shell on the machine running the backend can read/edit
project files directly anyway, so gating password changes behind browser
auth would add friction, not protection. `Run-RelionUS --set-password /
--enable-auth / --disable-auth / --auth-status` all shell out to
`python3 backend/auth.py <command>` and exit without starting the server;
`--auth`/`--no-auth` instead set `RELION_US_FORCE_AUTH=1`/`0` for that one
process only (`auth.is_enabled()` checks the env override before the
persisted config) — handy for a one-off session the opposite of however
it's normally configured, without touching the stored setting. Forcing "on"
with no password ever set falls back to disabled rather than locking
everyone out, since there would be nothing to check a login attempt
against.

**First-run prompt** lives in `Run-RelionUS` itself (bash), not in the
Python backend: it checks `auth.py config-exists` (has *any* config file
ever been written — declining still writes one, recording "asked, said no",
so the prompt is genuinely one-time) and, only if stdin/stdout are both a
TTY, asks interactively before the server starts. A non-interactive launch
(cron, systemd, sbatch, anything with redirected stdin) skips the prompt
without writing a config file at all, so the first person to run it by hand
later still gets asked, rather than an automated first launch silently
deciding "no" forever.

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
