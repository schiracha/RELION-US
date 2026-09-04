# RELION-US

**RELION — User Supported Frontend.** A browser-based job manager for
RELION, built as a *companion* to RELION rather than a modified RELION GUI.

> This app is still being built, and I'm not what I'd call a
> programmer — I know bash, some Python, and some Fortran (yikes!). So it's
> being vibe coded, and it gets built when I have time and tokens available.
> Please feel free to help, test, and send fixes. Pre-beta: use at your own
> risk, and be vigilant about your own resources and what you're working on.

RELION-US reads RELION's own source (`pipeline_jobs.cpp`/`.h`,
`gui_jobwindow.cpp`) to build accurate forms for all 32 RELION job types,
single-particle and tomography, then runs them through one consistent popup
UI with **an editable command box you approve before anything runs**. It also
adds four import bridges (IMOD, Warp/M, DeepETPicker, AreTomo2), six IsoNet2
job types, and in-browser replacements for the three RELION steps that only
ship as a desktop GUI.

It's a normal web page, so it's portable: run the backend on your workstation
or an HPC login node and open it from any browser on the network — no Qt, no
X11 forwarding, no display server.

**Contents:** [Why](#why-this-exists) · [Install](#installing-it) ·
[Running it](#running-it) · [Security](#security-https-and-the-password) ·
[Using it](#using-it) · [Job types](#what-jobs-are-available) ·
[The draft command](#how-the-draft-command-is-built) ·
[Working alongside RELION](#working-alongside-relions-own-gui) ·
[Command Center](#command-center-job-history) ·
[Viewer](#tomogram--particle-pick-viewer) ·
[Progress & Analyze](#live-progress-and-analysis) ·
[SLURM](#slurm-cluster-submission) ·
[Limitations](#known-limitations--what-to-double-check) ·
[Testing](#testing) · [License](#license)

## Why this exists

RELION's own GUI is a compiled Qt5/C++ application that assembles each job's
command internally and hands it straight to the shell. RELION-US puts that
command in front of you first and lets you edit it: whatever is in the command
box when you click **Run** is executed exactly as written, nothing added or
removed. The draft that pre-fills it is built from the standard inputs (see
[How the draft command is built](#how-the-draft-command-is-built)) — always
check it, and the job's real RELION C++ source is one tab away for
cross-referencing.

Jobs run **from the project directory**, exactly like RELION, so
project-root-relative paths behave the way RELION's own GUI expects. You can
change project directories on the fly without restarting, and the app
re-reads the new project's environment.

`docs/ARCHITECTURE.md` has the full design rationale, including why this is a
separate layer rather than a modified RELION GUI, and confirmation that
RELION-US never runs RELION's own compiled GUI binary (only its command-line
programs).

## Installing it

Python 3.10 or newer, and `backend/requirements.txt` installed into it.
There's no install script — build the environment with whatever Python tooling
you already use, so this stays portable across distributions rather than
assuming one package manager. A plain venv is the least assumption-laden
option:

```bash
python3 -m venv relion-us
source relion-us/bin/activate
pip install -r backend/requirements.txt
```

`conda`/`mamba` works just as well:

```bash
conda create -n relion-us python=3.11 && conda activate relion-us
pip install -r backend/requirements.txt
```

If `python3 -m venv` fails because the `venv` module isn't installed (common
on minimal installs), install it from your distribution's package manager
first — `sudo apt install python3-venv` on Debian/Ubuntu, pacman's `python`
package on Arch, or the equivalent — then retry.

**Distribution notes:**

- **Ubuntu 24.04** — no extra steps; it ships Python 3.12.
- **RHEL / CentOS Stream** (CentOS Linux proper is EOL — treat it as RHEL) —
  check `python3 --version` first. RHEL 8 ships Python 3.6 and RHEL 9 ships
  3.9, both below the 3.10 floor, so the venv above would be created against
  an interpreter `pip install` then fails on. Install a newer one and use it
  explicitly: `sudo dnf install python3.11` (RHEL 9: available directly via
  AppStream; RHEL 8: needs EPEL or Software Collections first), then
  `python3.11 -m venv relion-us`. Nothing else in the app is
  RHEL/CentOS-specific.

**Optional, feature by feature.** RELION's command-line programs need to be on
`PATH` to actually run jobs; `relion_pipeliner` for
[RELION sync](#-relion-sync); IMOD's `point2model`/`model2point` for the
`.mod` half of the IMOD bridge; a conda environment with `isonet.py` for the
IsoNet2 jobs; `sbatch`/`squeue`/`sacct`/`scancel` for
[SLURM submission](#slurm-cluster-submission). Each is checked at the point of
use and reported plainly if missing — nothing is required just to start the
app.

No CDN dependency: WinBox.js (the popup-window library) and xterm.js are
vendored under `frontend/vendor/`, specifically because many HPC login nodes
and workstations have no outbound internet access.

## Running it

With the environment active:

```bash
./Run-RelionUS         # binds 127.0.0.1:8420 (localhost only)
```

Then open `http://localhost:8420/`. `./Run-RelionUS --help` lists the
`--host`/`--port` options.

**On a remote server or HPC login node**, launch it there and port-forward
over SSH — the default bind is already right for this:

```bash
# 1. On the remote machine (HPC login node or server), in an SSH session:
./Run-RelionUS

# 2. On your laptop, in a SEPARATE terminal, leave this running. <host> is
#    whatever you'd normally type after `ssh` to reach that machine, i.e.
#    user@hostname or user@ip.address (e.g. jdoe@login1.cluster.edu), or
#    just hostname if your SSH config already sets the username:
ssh -L 8420:localhost:8420 <host>

# 3. On your laptop, open in a browser:
http://localhost:8420/
```

To reach it directly from another machine without a tunnel, opt in explicitly
with `--host 0.0.0.0` — read [Security](#security-https-and-the-password)
first and turn on `--tls`, because that's the point at which it starts to
matter.

**You don't have to run it from inside a project directory.** If you `cd` into
an existing RELION project first, it's picked up automatically; otherwise it
starts in a default project folder and you switch to the real one with
**📁 Change Project** in the top bar at any time.

**Running it as a plain command.** Typing the full path every time gets old;
put a symlink somewhere already on your `PATH` and `Run-RelionUS` works from
any directory:

```bash
# Just for you (if you already keep a personal bin/ on PATH):
ln -s "$(pwd)/Run-RelionUS" ~/bin/Run-RelionUS

# For every user on this machine:
sudo ln -s "$(pwd)/Run-RelionUS" /usr/local/bin/Run-RelionUS
```

## Security: HTTPS and the password

Two separate things, and they protect against different attacks. **Turn on
both** if this is reachable from any machine other than the one running it.

### HTTPS (encrypts the connection)

This is the one that matters most, and the one to set up first. Without it
every byte crosses the network in the clear — the password, your project
paths, job commands, and anything the Terminal popup shows. How well the
password is hashed on disk is irrelevant to that: someone on the path reads
the password itself, not the hash.

```bash
./Run-RelionUS --make-cert    # generate a certificate and key (once)
./Run-RelionUS --tls          # serve HTTPS with it
```

`--make-cert` writes a 4096-bit key and a self-signed certificate into the
config directory (key mode `0600`), records their paths so plain `--tls` finds
them later, and prints the certificate's SHA-256 fingerprint. The certificate
names this host plus `localhost`/`127.0.0.1`/`::1`, so it works whether you
reach the app directly or through an SSH tunnel.

**Your browser will warn once that the certificate isn't trusted.** That is
expected and does not mean the connection is unencrypted — traffic is TLS 1.3
either way. What a self-signed certificate can't do is *prove which server you
reached*, which is the one thing a certificate authority sells. Two ways to
close that gap:

- Compare the fingerprint the browser shows against the SHA-256 that
  `--make-cert` printed. If they match, there's no one in the middle. Do this
  the first time and the exception is remembered.
- Or use a real certificate — from your institution, or Let's Encrypt — and
  skip the warning entirely:
  ```bash
  ./Run-RelionUS --tls-cert /path/fullchain.pem --tls-key /path/privkey.pem
  ```

With a real CA-issued certificate you can also add `--hsts`, which tells
browsers to refuse plain HTTP to this host for a year. It's off by default,
and **refused outright with a self-signed certificate** — `--hsts-force`
overrides that, but read the next section before you use it.

### Recovering from HSTS

Worth understanding before turning `--hsts` on, because this is the one
setting here you cannot undo from the machine running the app.

`Strict-Transport-Security: max-age=31536000` is cached **by the browser**,
not held by the server. Dropping `--hsts`, restarting, even deleting the
certificate changes nothing — the browser already has the policy and will
honour it for a year.

With a self-signed certificate it's worse than inconvenient. HSTS
deliberately removes the click-through on certificate warnings ([RFC 6797
§12.1](https://datatracker.ietf.org/doc/html/rfc6797#section-12.1) — "there
is no such recourse"), so the browser refuses plain HTTP to that host *and*
refuses to let you accept the untrusted certificate. Both doors, for a year,
in every browser that saw the header. That's why `--make-cert` certificates
are refused: `Run-RelionUS` checks whether the certificate is self-signed
(issuer equals subject) and won't arm HSTS if it is.

There's a server-side escape in principle — serve `max-age=0` over HTTPS and
the browser drops the pin — but it needs a handshake the browser will
complete, which under an active pin with an untrusted certificate it won't.
So it works with a real certificate and is unavailable in exactly the
self-signed case where you'd need it.

If you've already locked yourself out, clear the HSTS entry per browser:

| Browser | How |
|---|---|
| Chrome / Edge | Open `chrome://net-internals/#hsts`, put the hostname in **Delete domain security policies**, click Delete |
| Firefox | History ▸ find the site ▸ **Forget About This Site**; or close Firefox and delete `SiteSecurityServiceState.txt` from the profile folder |
| Safari | Quit Safari, delete `~/Library/Cookies/HSTS.plist`, reopen |

A different hostname for the same machine (say the IP instead of the name)
is unaffected, since HSTS is stored per host — a quick way back in while you
sort the browser out.

A TLS-terminating reverse proxy (nginx/Caddy) in front of the app also works
and is a perfectly good choice if you already run one. The app reads
`X-Forwarded-Proto`, so the session cookie is still marked `Secure` in that
setup.

**Alternative: an SSH tunnel.** If you'd rather not manage certificates at
all, leave the default `--host 127.0.0.1` and tunnel — SSH provides the
encryption, and this is the setup the [Running it](#running-it) section
describes:

```bash
ssh -L 8420:localhost:8420 <host>
```

### The password (proves who is connecting)

Encryption stops eavesdropping; it doesn't stop the person on the next
workstation opening the page. Even at the localhost-only default bind, anyone
who can already reach this machine's localhost — another user on a shared HPC
login node — can open jobs, run them, delete history, and get an interactive
shell through the Terminal popup, with no login at all.

Every time `Run-RelionUS` starts at an interactive terminal without protection
already on, it offers to set one up. Decline and it asks again next time
rather than staying quiet forever. Otherwise it's terminal flags, on the
machine running the backend — deliberately with no in-browser way to change
it, since anyone who can already reach a shell there can edit project files
directly anyway:

```bash
./Run-RelionUS --set-password   # set/change it (hidden input, twice to confirm)
./Run-RelionUS --enable-auth    # require it from now on, every run
./Run-RelionUS --disable-auth   # stop requiring it (password kept, not deleted)
./Run-RelionUS --auth-status    # what's set, how it's hashed, and TLS state
./Run-RelionUS --auth           # force it ON for just this one run
./Run-RelionUS --no-auth        # force it OFF for just this one run
```

How the password is protected:

- **Stored as a salted [scrypt](https://datatracker.ietf.org/doc/html/rfc7914)
  hash** (n=2¹⁵, r=8, p=1 — RFC 7914's own interactive-login parameters,
  ~32 MB and ~0.12 s per guess), never in the clear. scrypt is *memory*-hard,
  which is what makes bulk GPU or ASIC guessing expensive rather than merely
  slow. It's stdlib, so this adds no dependency.
- **Upgrades itself.** An instance set up before this used PBKDF2-SHA256; that
  hash still verifies and is quietly re-hashed to scrypt the next time you log
  in successfully. Nobody has to reset a password, and `--auth-status` shows
  which one you're on.
- **Locked out after repeated failures** — 5 wrong guesses from one address
  within 5 minutes closes the door for 5 minutes, with a looser global
  backstop so rotating source addresses doesn't walk around it. The hashing
  cost only prices *offline* guessing against a stolen config file; 0.12 s per
  try is no obstacle to a script hammering the login endpoint, so that needs
  its own control.

  **If you lock yourself out**, the correct password is refused too — that's
  deliberate, or the lockout would be trivially bypassable. Two ways back:
  wait 5 minutes, or **restart the backend** (Ctrl-C and relaunch), which
  clears it instantly. The counters live in memory in the server process and
  are never written to disk. There is deliberately no `--clear-lockout` flag:
  a CLI invocation is a *separate* Python process with its own empty
  counters, so it would report success and change nothing in the running
  server. Restarting is the mechanism, not a workaround for a missing one.
- **Minimum 8 characters**, and the handful of passwords every guessing script
  tries first are refused. No composition rules — length is what actually
  predicts guessing cost, and "must contain a symbol" mostly produces
  `Password1!`, which is in every wordlist.
- **Session cookie** is `HttpOnly`, `SameSite=lax`, and `Secure` whenever the
  connection is HTTPS. It's a signed, stateless token, so a backend restart
  doesn't log everyone out. Sessions last 30 days.
- **Changing the password logs out every existing session at once**, on every
  device — there's no separate "log everyone out" step.

The password gates every page, every API call, and both websockets (live job
output and the Terminal shell), not just the initial page load. A **🔒 Log
out** item appears under **☰ Menu** once you're in.

### What this still isn't

- **One shared password, not user accounts.** There's nothing here to audit
  who did what — it only answers "did whoever's asking know the password".
- Responses carry `X-Content-Type-Options`, `X-Frame-Options: DENY` and
  `Referrer-Policy: same-origin`, and the app sets no CORS policy at all, so
  another origin can't drive the API. But anyone who *is* logged in can run
  arbitrary shell commands — that is the app's whole purpose. Treat access to
  it as equivalent to shell access on that machine.

## Using it

### Top bar

- **☰ Jobs** — show/hide the job list sidebar.
- **📁 Change Project** — switch which RELION project directory the app points
  at, without restarting. Every project you open or create is remembered, so
  the dialog opens with a **Recent projects** list: one click browses to a
  project, a double-click switches straight to it, and ✕ drops it from the
  list (the folder itself is never touched). A project that has since been
  deleted stays listed but struck through rather than quietly disappearing.
  Otherwise type a path and hit Go, or click into subfolders in the browser
  below. That browser lists folders on the *machine running the backend*, not
  your browser's machine — which is the point when the backend is on a cluster
  login node. If the folder doesn't look like a RELION project (no
  `default_pipeline.star`, and not opened here before), you're asked whether to
  start a new project there or pick a different folder.
- **🔍 Visualize** — the [tomogram / particle-pick
  viewer](#tomogram--particle-pick-viewer). Not a job; it writes nothing.
- **🌙 Dark / ☀ Light** — theme switch. Dark is the default, and your choice is
  remembered. Chart colours are mode-specific and validated against each
  background rather than naively inverted, so they stay legible either way.
- **⇄ RELION sync** — per-project two-way sync with RELION's own pipeline; see
  [below](#-relion-sync). Hidden entirely if `relion_pipeliner` isn't on
  `PATH`.
- **☰ Menu** — **⚙ Settings**, **🗑 Trash**, **🖥 Terminal**, **🛠 Tools ▸ 📊
  Analyze**, and **🔒 Log out** when password protection is on.

### Jobs list (left sidebar)

Every RELION job type grouped by RELION's own categories, with the import
bridges tagged `(custom)` and IsoNet2 under its own **IsoNet (Beta)**
category. An **All / SPA / Tomo** toggle filters the list; the app guesses
which to preselect from the job types the project has actually run, but never
gates what you can run.

Click a job to open it in its own popup — nearly window-filling with rounded
corners, and only one open at a time (opening a different job closes whichever
was already open, rather than stacking windows).

### Inside a job popup

- **Inputs tab** (opens first) — **every option RELION's own GUI shows for
  that job**, in RELION's own groups and order (I/O, Reference, CTF,
  Optimisation, Sampling, Helix, Compute, Running), as collapsible sections
  extracted from `gui_jobwindow.cpp` and `pipeline_jobs.cpp` rather than
  guessed. I/O starts open; the rest are one click away. Nothing RELION shows
  is hidden behind a different tab.

  Any field taking a single file — STAR files, MRC maps, image stacks, FASTA
  sequences, executables, whatever RELION's form asks for — gets a **…**
  browse button opening the same server-side file picker the viewer uses,
  filtered to that field's own extensions. It browses the backend's
  filesystem, not your browser's.

  - **Running section** — MPI procs, threads, and **Additional arguments**,
    RELION's own Running tab. Setting MPI procs above 1 does what RELION does:
    prefixes `$RELION_MPIRUN -n N` (default `mpirun`) and switches to that
    job's `_mpi` binary, with both binary names read out of the job's own C++
    source rather than guessed by appending a suffix. Additional arguments are
    appended verbatim at the end, as RELION appends them.
  - **Advanced section** (past Running) — command-line options the program
    accepts that [RELION's GUI never exposes](#the-advanced-section).
- **Progress tab** (iterative jobs only) — [live charts and class
  images](#live-progress-and-analysis).
- **CTF QC tab** (CTF Estimation only) — every micrograph's or tilt image's
  CTF fit numbers, with thumbnails, once the job finishes.
- **Outputs tab** — browse and download individual files, or a `.zip` of any
  selection. Click a `.star` filename to preview its contents inline instead
  of downloading it first.
- **Errors tab** — fills in live if the run writes to stderr; the tab badge
  shows a running error count.
- **RELION Source tab** — the *actual*, unmodified C++ `getCommands<Job>Job()`
  function for this job type, so you can check the draft or edited command
  against RELION's real logic by eye.
- **Command box** — pre-filled with the draft, fully editable. **Recompute
  draft** regenerates it from the current form values; or just hand-edit it.
- **Run** — executes exactly the string in the command box (RELION and IsoNet2
  jobs) or calls the converter directly (import bridges), streams output live
  over a websocket, and keeps the full transcript if you close and reopen.

Every run leaves `run.out`/`run.err` in its job directory, matching RELION's
own GUI convention, even though RELION-US streams output live rather than
shell-redirecting it.

There's no in-app page-scale control — use your browser's own zoom.

### ⚙ Settings

Per-user defaults, not per-project: default MPI procs / threads / GPU IDs /
additional arguments prefilled into every job popup; default SLURM
account, partition, time limit and memory; the Progress tab's refresh interval
and default "images every N iterations"; and a default folder for the project
browser to start in.

### 🗑 Trash

Deleting a job offers to move its output directory to `Trash/` rather than
removing it, mirroring RELION's own Delete-moves-to-Trash model. **Menu ▸ 🗑
Trash** lists everything there and restores a job back to its original
`<JobType>/jobNNN` slot, history entry included. Emptying the trash is a
separate, separately-confirmed action, and it's the only genuinely
irreversible one.

### 🖥 Terminal

A real interactive shell in the current project directory, in a popup. Handy
for `module load`, a quick `ls`, or anything the UI doesn't cover. Note that
this is exactly why the password option exists — see
[above](#security-https-and-the-password).

## What jobs are available

**All 32 RELION job types**, single-particle and tomography, extracted from
RELION's own source.

**Four import bridges** (`backend/converters/`) — `Import from IMOD (.mod)`,
`Import from Warp/M`, `Import from DeepETPicker`, and `Import from AreTomo2
(.aln)`. They use the same popup layout, live output, and Errors tab as any
RELION job; they just have no command box, since they call directly into
Python rather than spawning a subprocess. What to double-check before trusting
each one:

- **IMOD** — fully implemented and tested. The `.mod` ↔ coordinate functions
  need `point2model`/`model2point` on `PATH` (`module load imod` on a
  cluster); the `.xf`/`.tlt` I/O is unit-tested directly and needs no IMOD
  install.
- **Warp/M** — the column-diffing and mapping machinery is implemented and
  tested, but `DEFAULT_COLUMN_MAP` is intentionally empty. Warp 2.0's
  `ts_export_particles` already writes a RELION-5 optimisation set with native
  `rln*` columns, so that output needs no renaming at all; `.tomostar` and
  older particle exports use Warp's `wrp*` columns and do need a mapping.
  Rather than guess at names that have changed across Warp versions, run the
  job once to see the column diff, then fill in `DEFAULT_COLUMN_MAP` for your
  version.
- **DeepETPicker** — verified against the DeepETPicker README and its own
  `utils/coords_to_relion4.py` (`.coords` = `class_id x y z` in voxels; a bare
  3-column `x y z` file is also accepted, matching what DeepETPicker itself
  accepts). Fully implemented and tested. DeepETPicker ships that converter
  too — prefer it for a one-off conversion; this module is for wiring
  `.coords` → `particles.star` into the Jobs list and live-output flow.
- **AreTomo2** — reads AreTomo2's `.aln` global alignment block (`SEC ROT GMAG
  TX TY SMEAN SFIT SCALE BASE TILT`, verified against the AreTomo manual and
  the teamtomo/alnfile parser) and writes IMOD-style `.xf` + `.tlt`, which
  RELION-5's IMOD tilt-series import reads. It hands off through IMOD files
  rather than writing RELION's tilt-series STAR directly because the `.xf`
  mapping is independently corroborated by AreTomo's own `-OutImod` export.
  Dark (excluded) frames are reported. `TX`/`TY` are in pixels of the aligned
  stack and the `.aln` records no pixel size, so supply it downstream. If you
  still have AreTomo's own `-OutImod` output, prefer it.

**Coordinate flips.** The IMOD and DeepETPicker importers have `Swap Y and Z`
and per-axis `Mirror` options (`backend/converters/coord_transform.py`). The
Y/Z swap is the fix for IMOD's "flipped" (`trimvol -yz`) versus "rotated"
(`trimvol -rx`) tomogram convention — a model built on a flipped or raw-`tilt`
volume has depth in Y, not Z. Mirroring needs the tomogram dimension for that
axis, and fails loudly if you don't supply it rather than silently producing
wrong coordinates.

**Six IsoNet2 job types** under **IsoNet (Beta)** —
[IsoNet2](https://github.com/IsoNet-cryoET/IsoNet2)'s `prepare_star` →
`deconv` → `make_mask` → `denoise`/`refine` → `predict` chain for
missing-wedge correction and denoising of reconstructed tomograms. Unlike the
import bridges, these run `isonet.py` as a real subprocess in a conda
environment (SLURM submission included), with folder browse buttons for their
directory inputs and options cross-checked against IsoNet2's own GUI and
tutorial. **Denoise (Train)** and **Refine (Train)** get a live loss curve on
their Progress tab; **Predict**'s output MRCs link straight into the viewer.

### In-browser replacements for RELION's desktop-only steps

A few real RELION steps unconditionally open a desktop window with no headless
mode at all — `relion_manualpick` is a compiled FLTK canvas,
`relion_tomo_exclude_tilt_images` calls `napari.Viewer()`, and the Select
job's interactive branch shells out to `relion_display --gui`. All are
unusable from a browser-driven backend, so RELION-US replaces the interaction
itself with an in-browser equivalent, while still registering the job under
its real RELION type label (`relion.manualpick` / `relion.picktomo` /
`relion.excludetilts` / `relion.select`) — so it shows up correctly in
RELION's own GUI and its output is valid input to any real downstream RELION
job.

- **Manual Picking** (SPA) and **Manual Picking (Tomo)** — Run validates the
  input micrographs/tomograms; a **🔍 Open Picker** button then opens the
  viewer with picking on. Double-click to add a pick, right-click to delete
  one. Picks save into the job's own directory as you go, so Extract or
  TomoSubtomo can read them before you close the picker.
- **Exclude Tilt Images** — a plain per-tilt-series checklist (**🔍 Open
  Reviewer**) in place of napari's widget. Every image starts kept, matching
  napari's own starting state; uncheck the bad ones. Save always re-derives
  from the tilt series' original input, never from a previous save, so
  re-checking a previously-excluded image genuinely re-includes it and nothing
  accumulates across sessions.
- **Subset Selection (Select)** — RELION's Select job is a six-way branch.
  Five of them (select-on-value, discard-on-statistics, split, automated
  class-ranker, filament selection) are ordinary command-line calls RELION-US
  drafts normally. The sixth — interactively browsing class averages, or a
  plain list of micrographs/particles, and choosing what to keep — is the
  `relion_display --gui` one, and it's the one replaced here. Leave every mode
  checkbox unchecked, fill in one of the three inputs, and Run opens the picker
  lifecycle instead of building a subprocess command:
  - **Select classes from job** (a `_optimiser.star`/`_model.star` from a
    prior Class2D/Class3D run) — a **🎯 Select Classes** button opens a
    thumbnail grid, one card per class with its share of particles,
    resolution, and particle count; click to toggle. Saving writes
    `particles.star` (every selected class's particles, cross-referenced from
    the source job's own `_data.star`, optics block preserved verbatim) and,
    for a Class2D source only, `class_averages.star` — a real RELION quirk
    (Class3D has no separate class-averages output) reproduced rather than
    "fixed".
    - **Re-center images?** translates each selected class average to its
      centre of mass and writes a new `class_averages.mrcs`. The centre-of-mass
      computation matches RELION's exactly; the sub-pixel wrap-around shift
      uses `scipy.ndimage.shift` as a close stand-in for RELION's B-spline
      interpolation, not a bit-identical reproduction.
    - **Re-group particles?** rebalances the selection into the requested
      number of groups using the source `model.star`'s own group table, sorted
      by refined intensity-scale correction and bucketed by optics group,
      including RELION's real "at least 10 particles per group" minimum.
  - **Select from micrographs** / **Select from particles** — the same picker
    showing a flat list instead of classes: keep the checked rows, preserve
    every other column and the optics block, saved as
    `micrographs.star`/`particles.star`.

All of these share a **▶ Continue** / **⟳ Overwrite** / **✓ Done** lifecycle.
The job stays "Running" while you pick or review (there's no single moment
picking is "finished"), Continue resumes non-destructively, Overwrite clears
what's saved here and starts fresh in the same job slot, and Done marks it
complete — including in RELION's own pipeline, if sync is on. Nothing is
written until you explicitly save, and saving always re-derives from the
original input, so re-saving a different selection never accumulates.

## How the draft command is built

For each active field, if a `--<field_key>` flag literally appears in that
job's real `getCommands<Job>Job()` source (extracted, not guessed), the draft
emits `--<field_key> <value>` — a bare flag for booleans, only when true. This
is correct for the large majority of RELION options, because RELION's own
convention is overwhelmingly "the flag is named after the internal option
key". It is **not** a full reimplementation of RELION's C++ logic, which has
real per-job branching.

Where a flag isn't simply `--` + the option key, the pairing is read out of the
job's own builder too (`command += " --i " + joboptions["input_star_mics"]…`),
so `--i`, `--Box`, `--j` and ~80 others are drafted correctly rather than
reported as unmapped. A curated, source-verified override list fills in the
handful of cases those two rules can't reach — including the tomography jobs
whose optimisation-set / reference-map / direct-entry inputs map to RELION's
real `--ios`/`--i`/`--ref`/`--tomograms`/`--trajectories`/`--p`/`--t`/`--mot`
flags. `docs/ARCHITECTURE.md`'s "Draft command heuristic" section has the full
list.

A field whose flag happens to match its key isn't emitted just because the
name matches — the draft also checks the real, source-extracted condition
gating it in RELION's own code, evaluating straightforward checkbox-gated
conditions (`do_helix`, `do_apply_helical_symmetry`, and similar `&&`-chains)
against the values you actually submitted.

**Fields the draft can't place are left out and listed as "unmapped fields"**
(hover the Recompute button's tooltip) rather than guessed at — check the
RELION Source tab for those and add them by hand if needed. A condition too
complex to evaluate safely (an `||`, a brace-less `else` branch, a numeric
comparison) falls back to unmapped for the same reason. Pairings RELION only
emits inside a branch depending on a *different* option — Autopick's
`--particle_diameter` in Topaz mode versus `--LoG_diam_min` in LoG mode — are
deliberately left out, since emitting both would produce a command that
contradicts itself.

Other things the draft gets right:

- **GPU acceleration** (`--gpu`) for every job that supports it (2D/3D
  classification, 3D initial model, 3D auto-refine, multi-body refinement,
  Topaz picking), including RELION's own "auto-allocate" convention of passing
  `--gpu ""` when the box is ticked but "Which GPUs to use" is blank. Topaz-mode
  and MotionCorr GPU use are genuinely mode-branched in RELION's source rather
  than simple checkbox gates, and are known gaps — add `--gpu` by hand there.
- **Output rootname suffixes.** A few job types don't take a bare output
  directory for `--o`; RELION appends a literal suffix to form a file prefix.
  2D/3D classification, 3D initial model, 3D auto-refine and multi-body all use
  `run` (so files are `run_it000_…`); Mask creation and Post-processing use
  `mask.mrc` / `postprocess`.
- **Configurable executables.** DynaMight, ModelAngelo and External don't
  hard-code a binary — RELION runs whatever path you set in their "Location of
  X executable" field, and the draft resolves it from that field's value.

### The Advanced section

RELION's GUI exposes a subset of what each program actually accepts. The rest —
expert and developmental flags — are what its "Additional arguments" box exists
for, and finding them normally means running the binary with no arguments and
reading the usage dump.

The Advanced section at the bottom of the Inputs tab does that for you. The
first time you open it, it runs the job's program with `--help`, parses
RELION's own usage format, subtracts every flag the form above already covers,
and lists what's left with its default, section, and help text. Filter the
list, fill in a value, and **Add** appends it to the command box, where you can
still edit or delete it.

Three things worth knowing:

- It asks **your installed binary**, so the list reflects your RELION build,
  local patches included — not whichever checkout the job definitions came
  from. If MPI procs is above 1 it asks the `_mpi` binary, which can accept
  flags the serial one doesn't.
- If the program isn't on the backend's `PATH`, the tab says so plainly rather
  than showing an empty list. You can still type anything into the command box
  or Additional arguments.
- RELION-5's Python tomo tools are Typer-based and don't print RELION's usage
  format, so the tab shows their raw `--help` output as-is.

Each program's help is read once per backend session and cached against the
binary's path, size and modification time, so rebuilding RELION or switching
versions picks up new options without a restart.

## Working alongside RELION's own GUI

### Opening a project built in RELION's GUI

Point RELION-US at an existing project and it reads RELION's own
`default_pipeline.star`:

- **Its jobs fill the Command Center**, tagged `RELION`, with RELION's own job
  numbers, aliases, types and statuses. They carry no timestamps — RELION's
  pipeline file records none, and a directory's mtime is not a start time.
- **Opening one shows the settings it actually ran with**, read from that job's
  own `job.star`, the same file RELION's GUI reads to reopen a job. A job from
  RELION 3.0 or earlier (which wrote `run.job` in a different format) opens
  with the job type's defaults and says so.
- **Its Outputs and Progress tabs work.** An old classification's
  `run_it###_model.star` files are still there, so you get its resolution curve
  and class images without re-running anything.
- **New jobs continue the project's numbering.** RELION-US takes
  `rlnPipeLineJobCounter` and the existing process list into account, and skips
  any number whose directory is already on disk. In a project sitting at job011
  your next job is job012 — not job001 on top of somebody's Import.

Jobs RELION itself ran are **read-only** here: abort, resume and delete are
refused on them, because there's no `relion_pipeliner` verb that would let this
app keep RELION's own record consistent afterwards. Renaming and notes are the
exception — both are stored in `.relion_us/`, never in RELION's pipeline, so
they can't leave it describing something untrue. Overwrite is allowed when sync
is on (see below).

### ⇄ RELION sync

**On by default**, per project — the button in the top bar toggles it, and it's
hidden entirely if `relion_pipeliner` isn't on `PATH`. With it on, every job
you run here is also registered in `default_pipeline.star`, so it shows up in
RELION's own GUI too:

- Registration goes through **RELION's own `relion_pipeliner
  --addJobFromStar`**, the same binary RELION's GUI would use in your place.
  RELION-US writes the job's settings to a `job.star` and hands it over; that
  binary decides the job number, creates the job directory, works out the
  input/output node graph, and appends the process to the pipeline. RELION-US
  only reads the result back. The node and edge tables are never computed here.
- The run then executes in the directory RELION allocated — renumbering the
  draft command's `--o` if RELION picked a different slot than the one this app
  proposed, which can happen if RELION's own GUI created a job in between —
  with `--pipeline_control <job_dir>/` appended so the running program reports
  its own completion the way RELION expects.
- On completion, RELION-US calls `relion_pipeliner --check_job_completion` so
  the process's status (Succeeded/Failed/Aborted) updates immediately rather
  than waiting for RELION's GUI to notice.
- **Overwrite** applies `--pipeline_control` the same way a fresh run does, so
  an overwritten job's completion is picked up by RELION's GUI. It reuses the
  existing pipeline entry rather than registering a second one, matching what
  RELION's own Overwrite does.
- If `relion_pipeliner` is busy — RELION's GUI is mid-write and holding the
  project's `.relion_lock` — registration waits (up to about two minutes) for
  the lock rather than skipping it. If it still can't register or confirm
  completion, the run itself is unaffected; a note in that job's output says so
  and tells you to run `relion_pipeliner --check_job_completion` yourself, or
  just open RELION's GUI, to catch the pipeline file up.
- Jobs run here **before** you turned sync on are not added retrospectively.
  Sync only covers what happens from that point on.
- Turn it **off** for a project that genuinely shouldn't gain RELION-US rows in
  its pipeline — a colleague's project you only meant to look at, say.

**What RELION-US writes to that file itself.** Almost everything above
delegates to `relion_pipeliner`; the node and edge tables are never computed
here. Two narrow exceptions, both deliberate and both explained in
`backend/pipeline_bridge.py`:

1. **An empty skeleton for a brand-new project**, written once and only if the
   file doesn't exist. `relion_pipeliner` can't create it from nothing — it
   reads the pipeline first, and reading a missing file exits *while still
   holding the lock*, orphaning `.relion_lock`. RELION's own GUI writes the
   same fixed skeleton on first launch instead of reading; so does this.
2. **One status token, to mark a job "Running"**, under the same
   `.relion_lock` mutex `relion_pipeliner` takes. `--check_job_completion` only
   promotes processes already marked Running, and no CLI path reaches that
   status short of re-running the job.

Neither is a substitute for closing a native RELION GUI that already has the
project open — a live GUI holds its own in-memory copy of the pipeline that no
on-disk write can coordinate with.

**Deleting** a synced job doesn't touch `default_pipeline.star` at all, since
`relion_pipeliner` has no verb for removing one process. The entry stays in
RELION's file; RELION-US keeps a local hide-list so it doesn't reappear as a
ghost row in the Command Center.

## Command Center (job history)

The main panel lists every job run in the current project, in three togglable
views:

- **Table** — sortable by job name/number, type, status, or start time.
- **Timeline** — newest-first or oldest-first, a card per job, linking each to
  the jobs its inputs came from.
- **Network** — a lineage graph, oldest jobs at top, with every job that used
  another job's output drawn beneath it and connected by a branch line. A job
  whose output fed two later jobs shows two branches side by side.

For a project built in RELION's own GUI, that lineage isn't guessed from file
paths — it's read straight from `default_pipeline.star`'s own
`pipeline_input_edges`/`pipeline_output_edges` tables, the graph RELION itself
computed when each job ran. For this app's own jobs it's best-effort, detected
from paths in the command that exist on disk and live under an earlier job's
directory; the timeline labels these "Inputs from:" either way.

Clicking a job reopens its popup, showing the options it ran with, its live or
final status, and its Outputs/Errors/RELION Source tabs. For a run from the
current backend session this reconnects to the live stream; for one from a
previous session it shows the saved status and its output files, since the live
transcript itself isn't persisted — only the summary.

Each popup's toolbar mirrors RELION's own "Job actions" menu: collapse, close,
rename (RELION's *Alias*), edit note, **Overwrite** (re-runs into the same job
directory and number, so it stays one entry — matching how RELION reuses a job
slot), **Abort** (kills the whole process group, not just the shell), Mark
finished / Mark failed, **Delete**, and **Clean** / **Harsh Clean**.

Clean is a *review* flow, not a silent sweep: it lists every file with its
size, pre-checks a suggestion, and deletes only what you confirm. RELION's own
cleanup uses per-job-type glob patterns defined in its C++ source; this uses its
own review-based suggestion rather than mirroring them.

## Tomogram / particle-pick viewer

**🔍 Visualize** opens a viewer. It is *not* a job — it never appears in the
Command Center and writes nothing. Give it an optimiser STAR, a
`tomograms.star`, or an MRC (with or without a particles/coords STAR), by
typing a path or hitting **…**, and it loads one tomogram at a time.

**Three linked orthogonal views**, laid out the way DeepETPicker's picker is:
**XY** is the large main view, **ZY** to its left, **XZ** below it. All three
are cuts through one crosshair position, so:

- **click** (or click-drag) in any view to move the crosshair — the other two
  jump to that point;
- **scroll** over a view to step along its own axis (scroll the main view to
  walk through Z); hold **Shift** for 10-slice steps;
- or drive X/Y/Z directly with the sliders in the side panel.

The three panels share one isotropic scale, so a voxel is the same size in each
and the crosshair lines up across panel borders — the side views are as
tall/wide as the volume actually is, not stretched to fill a box.

Everything else lives in a narrow rail on the right so the images get the
window: the two file inputs, black/white-point contrast sliders (default is a
robust 0.5–99.5% percentile, since raw cryo-ET min/max is usually washed out),
pick diameter and line width, and toggles for the pick overlay and crosshair.

Picks are overlaid using DeepETPicker's own model — a particle is drawn on
every slice within ±(diameter/2) of its centre, with radius `sqrt(r² − Δ²)` so
the marker grows toward the particle's centre slice — in all three views at
once. If the tomogram's name doesn't match any `rlnTomoName` in the picks file,
you get a warning with **Load anyway / Reload files / Cancel**.

Both inputs have a **…** browse button listing files on the *machine running
the backend*, filtered to the relevant extensions, remembering the folder you
were last in, and filling the field with a project-relative path.

The volume is never loaded whole: the backend memory-maps the MRC and returns
one slice at a time as a PNG, and only the panels whose slice index actually
moved are refetched.

## Live progress and analysis

### Progress tab

Classification and refinement runs take a long time and report every few
iterations, so those jobs get a **Progress** tab that plots the report as it
arrives. It covers **Class2D, Class3D, 3D auto-refine, 3D initial model,
multi-body, and tomo Reconstruct Particle**; jobs with nothing to plot simply
don't show the tab.

Updated while the job runs:

- **Resolution by iteration** — current resolution and best class, both in Å on
  one axis, latest value labelled.
- **Particles per class** — a bar per class for the newest iteration.
- **Class images** — 2D class averages, or the central slice of each 3D class
  volume, captioned with class number, share of particles, and resolution.
- **Viewing-direction distribution** — an on-demand button (never auto-polled,
  since it parses a per-particle file that can run to tens of millions of
  rows) for the 3D job types.

It reads the files RELION already writes each iteration
(`run_it###_model.star`, `run_it###_classes.mrcs` / `run_it###_class###.mrc`),
so there's nothing to configure and nothing extra on disk.

**Keeping it cheap.** Every control is per job, in the tab itself:

- **Live progress** (on by default) — untick and the job stops being polled at
  all.
- **Images every N iterations** — class images refresh only on multiples of N
  (1 = every iteration). The charts still update every iteration; they're
  nearly free.
- **Keep all** (off by default) — on, earlier iterations' images are kept so you
  can compare how classes evolved. Off, only the newest set is held, so memory
  stays flat however long the run goes.

Nothing is cached to disk, thumbnails are 128 px greyscale rendered on demand,
and polling only happens while the job is actually running. Defaults for the
refresh interval and N come from **⚙ Settings**.

### 📊 Analyze (Menu ▸ Tools)

Reads across a run's whole iteration history rather than just the latest, with
tabs for **Pipeline**, **Micrographs**, **Particles**, **2D Classification**,
**3D Classification** and **3D Refine** — convergence curves, per-class
distribution over iterations, per-class FSC, and scatter plots of any two
columns in a particles or micrographs STAR. The micrographs view left-joins the
STAR you pick (typically CTFFind's `micrographs_ctf.star`) with each producing
MotionCorr job's `corrected_micrographs.star`, so CTF-derived and
motion-derived columns can be plotted against each other. You can export any
selection (or its complement) as a new STAR file alongside the source.

### Charts

Charts sit in a compact responsive grid — several side by side rather than one
full-width chart per row — and use a fixed colorblind-safe palette
([Okabe & Ito, 2008](https://jfly.uni-koeln.de/color/)) for anything with more
than two series, rather than an arbitrary hue rotation that sweeps through the
red/green region indistinguishable under the most common forms of colour
blindness. The same grid and palette are used by the Progress tab, the CTF QC
tab, and Analyze.

## SLURM cluster submission

Every RELION and IsoNet2 job popup has a **Submit to SLURM cluster** checkbox
next to the command box. Check it and the same command that would otherwise run
as a direct subprocess is wrapped into an sbatch script
(`slurm/template_relion_job.sbatch` / `template_python_job.sbatch`, with
literal `ACCOUNT_NAME`/`PARTITION_NAME`/etc. placeholders filled in from the
**Account**, **Partition**, **Time limit** and **Memory** fields that appear)
and submitted with `sbatch --parsable`.

The Command Center polls `squeue`, falls back to `sacct` once SLURM ages the
job out of `squeue`, and rolls the real SLURM state into the same
Running/Queued/Completed/Failed states a local job uses. **Abort** calls
`scancel`.

- **Job dependencies** — an optional **Depends on SLURM job ID** field adds
  `--dependency=afterok:<id>`, so this job only starts once another succeeds. A
  `<datalist>` suggests this project's currently queued/running SLURM job IDs
  so you don't have to copy-paste one. Chaining N jobs is just submitting each
  with this set to the previous job's ID.
- **Job arrays** — check **Submit as SLURM array** and list one item per line in
  **Array items**, plus an optional **Throttle** capping how many run at once
  (SLURM's `--array=0-N%throttle`). Each task gets its own item in an
  `$ARRAY_ITEM` shell variable your command can reference — most directly
  useful for the **External Job** type, whose command box is already free text.
  This is a generic "run the same command once per input line" primitive, not
  automatic splitting/merging of a real RELION job's own STAR file. The Command
  Center shows a live **K/N tasks** readout alongside the array's status.

Defaults for account, partition, time and memory come from **⚙ Settings**.

`slurm/submit.py` is a standalone command-line path for submitting a job or
converter as a batch job without the browser at all, using the same templates.
Those templates are intentionally generic, not written for any specific site:
partition names, account syntax and module names all vary between clusters, so
`ACCOUNT_NAME`/`PARTITION_NAME` are placeholders you fill in — run `sinfo` for
partition names and `module spider relion` / `module spider imod` for the exact
module strings on your install.

## Regenerating the job definitions

Every job's fields, defaults, help text, and real C++ command logic come from
`data/extract_job_definitions.py`, which parses a real RELION checkout
(`github.com/3dem/relion`, cloned 2026-08-14) rather than being hand-typed. To
regenerate `data/job_definitions_raw.json` against a newer RELION:

```bash
git clone --depth 1 https://github.com/3dem/relion.git /tmp/relion_src
python3 data/extract_job_definitions.py \
    /tmp/relion_src/src/pipeline_jobs.cpp \
    /tmp/relion_src/src/pipeline_jobs.h \
    /tmp/relion_src/src/gui_jobwindow.cpp \
    data/job_definitions_raw.json
```

Then re-run the test suite (`cd backend && python3 -m pytest -v`). It runs
against the *real* extracted data rather than synthetic fixtures, so parsing
gaps introduced by a new RELION release show up as failures.

## Known limitations / what to double check

- The draft-command heuristic is best-effort, not a guaranteed match for
  RELION's exact branching logic — always review the command before running,
  which is the whole point of the editable box.
- `External`'s "Params" tab exposes RELION's generic
  `param1_label`/`param1_value` … `param10_label`/`param10_value` slots
  verbatim, which is how RELION's own External job works — you name your own
  flags there.
- Warp/M column names are unverified against your specific install (see
  [the import bridges](#what-jobs-are-available)).
- The job definitions come from one specific RELION checkout. Job internals
  change across releases, so re-run the extractor after a RELION upgrade; the
  test suite flags most breakage immediately.
- Jobs RELION itself ran are read-only here — abort, resume and delete are
  refused on them.
- **RELION sync is on by default**, so a project you open here will start
  gaining RELION-US rows in its `default_pipeline.star` as you run jobs. That's
  usually what you want when you go back and forth between the two GUIs, but
  turn it off for a project you only meant to look at. It's also worth not
  running RELION's own GUI on the same project at the same time — a live GUI
  holds its own in-memory copy of the pipeline.
- Sync needs `relion_pipeliner` on `PATH` and, per registration, the project's
  `.relion_lock` free within about two minutes — if RELION's GUI is mid-
  operation on the same project, a registration can wait that long before the
  run starts.
- Job history persists run *summaries* (command, status, timestamps) per
  project, not full stdout/stderr transcripts. Reopening a job from history
  after the backend restarted shows its last known status, not its old live
  output — though `run.out`/`run.err` are still in the job directory, and the
  Outputs tab will show them. Runs from the current session stream normally.
- SLURM submission is verified against stub scheduler binaries in this
  project's own tests, not a real cluster — the array and dependency paths in
  particular are worth a careful first run before relying on them. Still on the
  list: auto-splitting and merging a real RELION job's own input STAR per array
  task, live per-task output streaming for arrays (each task's output file is
  reachable via the Outputs tab meanwhile), and a dedicated
  dependency-chain-builder UI.
- There is no CORS policy and no cross-origin access, on purpose: the frontend
  is same-origin, and anything that could drive `/api/runs` from another origin
  could run arbitrary shell commands here. If you ever serve the frontend
  separately, add that one origin explicitly — never `*`.

## Testing

```bash
./run_tests.sh              # backend suite only — seconds, run it always
./run_tests.sh viewer       # + tomogram viewer, recent projects, Progress
                            #   tab, theme, file picker ("progress" is an
                            #   alias for the same tier)
./run_tests.sh options      # + option placement, MPI/threads, Advanced section
./run_tests.sh jobs         # + job popups, Command Center, abort/overwrite
./run_tests.sh project      # + Change Project, recents, Create Folder
./run_tests.sh legacy       # + opening a project built in RELION's own GUI,
                            #   and the network view on a wide, branching,
                            #   long-job-name pipeline
./run_tests.sh auth         # + password protection (login/logout, the gate
                            #   on pages/API/websocket)
./run_tests.sh all          # everything — before you commit a milestone
```

The browser suites are tiered because each needs its own backend on its own
throwaway project, and running all of them to check a change that touched one
module costs minutes and tells you nothing. Pick the tier covering what you
changed; `run_tests.sh`'s header comment has the mapping. `all` is for a real
checkpoint, or for a change to something shared like the popup scaffolding in
`app.js`.

The runner creates a fresh project directory and picks a free port per suite,
waits for each backend to answer before starting, and tears everything down
afterwards, including on Ctrl-C. It redirects `XDG_CONFIG_HOME`, so a test run
never touches your real recent-projects list, and no project of yours is
touched — a suite that asserts "no jobs yet" would fail against a project that
has history, which is a false alarm rather than a bug.

The backend suite runs against real extracted RELION data and real converter
behaviour rather than synthetic fixtures, so a change in RELION's job
definitions or a regression in a format bridge shows up as a failure. One test
auto-skips unless IMOD's `point2model`/`model2point` are on `PATH` (the `.mod`
round-trip).

To run one suite by hand, point it at any live instance — each takes a base URL
(and the two that write fixtures also take a project directory):

```bash
python3 test_jobs.py http://127.0.0.1:8420
python3 test_viz_and_progress.py http://127.0.0.1:8420 /path/to/empty/project
```

Set `RELION_US_CHROMIUM` if Playwright can't find a usable Chromium itself (a
shared read-only install on a cluster, say); otherwise `playwright install
chromium` is all that's needed.

## License

RELION-US is released under the **GNU General Public License, version 2 or
later** (`LICENSE`).

That license follows the material this repository redistributes:
`data/job_definitions_raw.json` embeds the verbatim `getCommands<Job>Job()` C++
source and the field defaults and help strings for all 32 job types, extracted
from RELION (© MRC Laboratory of Molecular Biology, GPL-2.0-or-later) — the
same data the RELION Source tab shows you. `frontend/vendor/` bundles WinBox.js
(Apache-2.0) and xterm.js (MIT).

The Analyze popup ports the tab layout and technique — not source — of
`relion_analyse.py` from
[CNIO_Relion_Tools](https://github.com/cryoEM-CNIO/CNIO_Relion_Tools)
(cryoEM-CNIO, GPL-3.0); every chart in it is this app's own hand-rolled
SVG/canvas rendering, built on none of that project's Dash/Plotly/Cytoscape
stack. The tomogram/particle-pick viewer likewise ports the interaction
model — tri-view slice layout, pick-overlay sizing rule, percentile contrast
stretch — of [DeepETPicker](https://github.com/cbmi-group/DeepETPicker)'s own
picker GUI (cbmi-group, GPL-3.0), reimplemented for the browser instead of its
desktop Qt/pyqtgraph app; see "Tomogram / particle-pick visualizer" in
`docs/ARCHITECTURE.md` for the point-by-point correspondence.

`NOTICE.md` has the full attribution, what was taken from where, and the
third-party format and dependency situation. RELION-US is an independent
project, not endorsed by or affiliated with the RELION authors or the
developers of any other software named here.
