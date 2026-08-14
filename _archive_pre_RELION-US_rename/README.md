# relion_tomo_bridge

A portable, browser-based companion to RELION-5's tomography pipeline —
NOT a fork or patch of RELION itself. See `docs/ARCHITECTURE.md` for why
this is a separate layer rather than a modified RELION GUI, and for the
reasoning behind each design choice.

## What's here

```
converters/    star_io.py, imod_bridge.py, warp_bridge.py, deepetpicker_bridge.py
gui/           app.py — Streamlit front end (streamlit run gui/app.py)
slurm/         sbatch templates + submit.py, for Rivanna/Afton
tests/         pytest unit tests for every converter (22 pass, 1 skips without IMOD installed)
docs/          ARCHITECTURE.md — full design rationale + references
examples/      (empty — add sample STAR/.coords files here as you get them)
```

## Quick start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 -m pytest tests/ -v          # confirm everything works in your environment
streamlit run gui/app.py             # local GUI
```

On Rivanna/Afton, heavy jobs go through `slurm/submit.py` (see
`slurm/template_python_job.sbatch` / `template_relion_job.sbatch`) rather
than running interactively, per your cluster-submission policy.

## Status / what still needs your input

- **IMOD bridge**: fully implemented and tested. The `.mod` <-> coordinate
  functions need `point2model`/`model2point` on PATH (`module load imod`
  on the cluster) — they're not available in the environment these were
  developed in, so those two functions are unit-tested with a
  environment-aware skip; everything else (`.xf`/`.tlt` I/O) is tested
  directly.
- **Warp/M bridge**: the column-diffing and mapping machinery is
  implemented and tested, but `DEFAULT_COLUMN_MAP` is intentionally empty
  — recent Warp/M versions are moving toward RELION-5's own STAR
  conventions, so you may need little to no renaming, but I didn't want to
  hard-code column names I couldn't verify against your actual install.
  Send me one real `.tomostar` or particle STAR export and I'll fill in a
  verified mapping.
- **DeepETPicker bridge**: verified against the DeepETPicker README
  (`.coords` = `class_id x y z`, voxels) and fully implemented/tested.
  DeepETPicker also ships its own `coords_to_relion4.py` — prefer that
  directly if you just need a one-off conversion; this module is for
  wiring `.coords` -> particles.star into this GUI/SLURM flow.
- **SLURM templates**: partition names (`standard`/`parallel`/`largemem`/
  `gpu`/`dev`) and the `lmod` module system are confirmed against UVA
  Research Computing's current Rivanna/Afton FAQ. Your allocation account
  name and the exact RELION/IMOD module version strings are placeholders —
  fill in `ACCOUNT_NAME` and run `module spider relion` / `module spider imod`
  on a login node to get the exact string for your install.

## Testing

```bash
python3 -m pytest tests/ -v
```

22 tests pass in this development environment; 1 additional IMOD `.mod`
round-trip test auto-skips here since `point2model`/`model2point` aren't
installed in this sandbox (it will run on any machine with IMOD).
