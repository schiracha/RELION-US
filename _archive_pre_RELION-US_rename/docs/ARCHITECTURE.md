# relion_tomo_bridge — architecture & scope

## Why a companion tool instead of patching RELION itself

RELION's own GUI (`relion`, built from `src/apps/maingui.cpp` and the `gui_*`
sources in the [3dem/relion](https://github.com/3dem/relion) repo) is a
monolithic Qt5 C++ application that is compiled together with the whole
processing engine. Patching it directly means:

- rebuilding the entire RELION binary from source (CMake + Qt5 + FFTW +
  CUDA toolchain) every time you want to test a change,
- re-merging your patch by hand on every upstream release (RELION moves
  fast — 5.0 landed the whole new tomography pipeline), and
- your changes only run wherever you've built that exact binary, which
  fights the "portable" goal directly.

Instead, this project is a **separate, lightweight layer that sits next to a
normal RELION install**: it reads and writes the same STAR files RELION
uses (RELION's actual interchange format — the GUI itself is just a job
scheduler over STAR files and `relion_*` command-line programs), and drives
`relion_*` binaries as subprocesses. That gets you:

- a friendlier, more visual front end without touching upstream code,
- something that runs identically on your laptop and, launched as a job, on
  Rivanna/Afton,
- zero merge conflicts with upstream RELION releases,
- reuse of existing community tooling instead of reinventing STAR parsing —
  see below.

If at some point you *do* want a literal GUI patch (e.g. changing a dialog
inside `relion` itself), that's a different, much heavier project (Qt5 +
C++ + full rebuild) and should be scoped separately.

## What already exists — don't reinvent these

- **[`starfile`](https://pypi.org/project/starfile/)** (PyPI) — a mature
  Python library for reading/writing RELION-style STAR files (including the
  multi-block "optimisation set" files RELION-5's tomography pipeline uses)
  into pandas DataFrames. Written by Alister Burt, who is also a co-author
  on the RELION-5 tomography paper (see References). This project wraps it
  rather than writing a STAR parser from scratch.
- **IMOD command-line tools** (`point2model`, `model2point`, `imodtrans`,
  etc., part of the IMOD package you already have on the cluster/workstation)
  — used as subprocesses for anything involving `.mod` files, rather than
  reimplementing IMOD's binary model format.
- **RELION's own `relion_tomo_import_tomograms` / `relion_python_*`
  programs** — called directly for anything RELION already knows how to do;
  this project only fills gaps (format bridging, GUI ergonomics, SLURM
  submission), not core processing.

## Components

```
relion_tomo_bridge/
├── converters/        # pure-Python + subprocess bridges, no GUI dependency
│   ├── star_io.py          # thin wrapper over `starfile`, RELION-5 tomo aware
│   ├── imod_bridge.py      # IMOD .xf/.tlt/.mod <-> RELION tilt-series STAR
│   ├── warp_bridge.py      # Warp/M metadata <-> RELION-5 STAR
│   └── deepetpicker_bridge.py  # DeepETPicker coordinates -> particles.star
├── gui/                # Streamlit app; local process, talks to converters/
│   └── app.py
├── slurm/              # sbatch templates + a submit helper
│   ├── template_relion_job.sbatch
│   ├── template_python_job.sbatch
│   └── submit.py
├── tests/              # unit tests against synthetic STAR/IMOD files
├── examples/           # tiny synthetic fixtures used by the tests/docs
└── docs/
```

### Why Streamlit for the GUI

You said "portable" and "friendlier." Streamlit gives a browser-rendered UI
(so it looks the same on any OS, no Qt install, no compiled GUI toolkit)
that you launch with one command (`streamlit run gui/app.py`), works
identically over an SSH port-forward if you ever want to check a job from
off-campus, and is trivial to extend since each panel is just a Python
function. The trade-off: it's a **job submission/monitoring/conversion
front end**, not a 3D density-map viewer — for map/tomogram visualization
you still want ChimeraX/napari, which the GUI can shell out to launch.

### Division of labor: local vs. Rivanna/Afton

Per your SLURM policy: anything that's a trivial local file operation
(renaming, small STAR edits, launching the GUI itself) runs directly.
Anything that pulls from the web, takes more than a few seconds, or runs
iteratively — all of the `relion_*` processing jobs, and any of the
converters when run over a full dataset rather than a handful of test
files — goes through `slurm/submit.py`, which fills in an `.sbatch`
template and calls `sbatch`. The GUI's "Run on cluster" button is just a
wrapper around that same submit helper, so there's one code path whether a
job is launched by hand or from the GUI.

## Format-bridging honesty note

RELION-5's tomography STAR schema (`rlnTomoName`, `rlnCoordinateX/Y/Z`,
`rlnTomoParticleId`, per-tomogram optics groups, etc.) is documented in the
RELION-5 tomography paper and the ReadTheDocs pages linked below, and
`star_io.py` targets that. Warp/M's and DeepETPicker's exact output column
names can drift between versions and installs, so `warp_bridge.py` and
`deepetpicker_bridge.py` are built with the field-mapping isolated in one
place and clearly marked `# CONFIRM AGAINST YOUR OUTPUT` — point them at one
real example file from your Warp/M project and your DeepETPicker run and
we'll lock in the exact column names rather than guessing. I did not want
to hard-code column names I couldn't verify.

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
  (`src/apps/maingui.cpp`, `src/pipeline_jobs.h`)
- RELION documentation: https://relion.readthedocs.io/
- IMOD tomogram import reference:
  https://relion.readthedocs.io/en/release-4.0/STA_tutorial/ImportTomo.html
- DeepETPicker source: https://github.com/cbmi-group/DeepETPicker
