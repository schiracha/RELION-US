# Copyright, licensing, and third-party material

RELION-US is licensed under the **GNU General Public License, version 2 or
(at your option) any later version** — see `LICENSE` for the full text.

GPL-2.0-or-later was chosen because this repository redistributes verbatim
GPL-licensed RELION source (see below), and GPL-2.0-or-later is the license
those excerpts carry.

## Bundled RELION source (GPL-2.0-or-later)

`data/job_definitions_raw.json` contains, for each of the 32 RELION job
types, material extracted verbatim from RELION's own source:

- the complete `getCommands<Job>Job()` C++ function body (`commands_source`),
  extracted from `src/pipeline_jobs.cpp`;
- every field's default value and help string, extracted from
  `src/pipeline_jobs.cpp` and `src/gui_jobwindow.cpp`.

This material is:

    Copyright (C) MRC Laboratory of Molecular Biology
    Author: Sjors H. W. Scheres and the RELION authors
    https://github.com/3dem/relion

and is distributed under the GNU General Public License, version 2 or later,
as stated in the headers of the files it was taken from. Those headers also
require that the copyright notice be preserved in any redistribution; this
file and `LICENSE` serve that purpose.

RELION-US does **not** link against, compile, or modify RELION. It reads
RELION's source as text at extraction time, and at runtime invokes RELION's
own command-line programs as ordinary subprocesses.

Regenerate the extracted data from your own RELION checkout with
`data/extract_job_definitions.py` — see "Provenance and re-running the
extraction" in `README.md`.

## Bundled WinBox.js (Apache-2.0)

`frontend/vendor/winbox.bundle.min.js` and `frontend/vendor/winbox.min.css`
are WinBox.js by Nextapps GmbH, distributed under the Apache License 2.0.
The license text is retained verbatim at
`frontend/vendor/WINBOX_LICENSE.txt`; upstream is
https://github.com/nextapps-de/winbox. It is vendored rather than loaded from
a CDN because many HPC login nodes have no outbound internet access.

Apache-2.0 material may be included in a GPL-2.0-**or-later** work under the
"or later" option (Apache-2.0 is compatible with GPLv3, not with GPLv2
alone); the combined work is distributed under GPL-3.0 terms in that case.

## Analyze popup, design and technique (CNIO_Relion_Tools, GPL-3.0)

The Analyze popup (Menu ▸ Tools ▸ Analyze) reproduces the tab layout and
several data-processing techniques from `relion_analyse.py`, part of

    CNIO_Relion_Tools
    cryoEM-CNIO organization
    https://github.com/cryoEM-CNIO/CNIO_Relion_Tools
    Licensed under the GNU General Public License, version 3.0

No source from that project is copied into this repository. It is built on
Dash, Plotly, and dash_cytoscape, none of which this project depends on (see
"Bundled WinBox.js" above and `frontend/app.js`'s own charting-code comments
on staying dependency-free for offline/HPC use) — every chart and graph in
the Analyze popup is this app's own hand-rolled SVG/canvas rendering.

What was ported is the *shape* of the analysis: which views to offer
(pipeline graph, micrograph/particle scatter with export, 2D/3D
classification convergence, per-class FSC, angular-distribution heatmaps,
3D refinement), which RELION output files back each one, and — for the
STAR-file merge and export features specifically — the column-join and
file-write technique. See the in-code comments in `backend/analyze.py` and
`frontend/app.js` at each ported function for the specific correspondence.

RELION-US is not produced, endorsed, or supported by CNIO or the authors of
CNIO_Relion_Tools.

## Tomogram/particle-pick visualizer, design and technique (DeepETPicker, GPL-3.0)

The tomogram/particle-pick visualizer ("🔍 Visualize" in the top bar)
reproduces the *interaction model* of DeepETPicker's own picker GUI —
three linked orthogonal slice views, click/wheel navigation, and the
pick-overlay sizing rule — in a browser rather than a desktop Qt/pyqtgraph
app, so it works on a remote HPC login node with only a browser tab. It is
part of

    DeepETPicker
    cbmi-group organization
    https://github.com/cbmi-group/DeepETPicker
    Licensed under the GNU General Public License, version 3.0

No source from that project is copied into this repository; `backend/viz.py`
and `frontend/app.js`'s viewer code are this app's own implementation,
verified against `github.com/cbmi-group/DeepETPicker`'s `main.py` /
`utils/utils.py`. What was ported is the *technique*: the tri-view layout
(XY main panel, ZY/XZ side panels sharing one crosshair), the
particle-overlay rule (a pick appears on every slice within ±(diameter/2)
of its centre, with radius `√(r² − Δ²)`), and the default percentile-based
contrast stretch. See `docs/ARCHITECTURE.md`'s "Tomogram / particle-pick
visualizer" section and the in-code comments at the top of `backend/viz.py`
for the specific correspondence, and citation:

    Liu G, Niu T, Qiu M, Zhu Y, Sun F, Yang G. DeepETPicker: Fast and
    accurate 3D particle picking for cryo-electron tomography using weakly
    supervised deep learning. Nat Commun. 2024;15:2090.
    DOI: 10.1038/s41467-024-46041-0

RELION-US is not produced, endorsed, or supported by the cbmi-group or the
authors of DeepETPicker.

## Third-party file formats

The import bridges read and write file formats belonging to other packages —
IMOD (`.mod`, `.xf`, `.tlt`), Warp/M (`.tomostar`, `wrp*` STAR columns),
DeepETPicker (`.coords`), and AreTomo2 (`.aln`). These implementations were
written for this project from published format documentation and each
package's own open-source converters; no source code from those packages is
copied into this repository, and none of them is bundled, linked, or
redistributed here. IMOD command-line tools, where used, are invoked as
external subprocesses that the user installs separately.

## Python dependencies

Installed from PyPI at the user's own discretion via
`backend/requirements.txt`; none is vendored into this repository. Their
licenses are their own (`fastapi`, `uvicorn`, `pydantic`, `pandas`,
`starfile`, `mrcfile`, `pillow`, `scipy`, `pytest`, `playwright`).

## Colorblind-safe chart palette (Okabe & Ito, 2008)

The multi-series charts (Progress tab, CTF QC tab, Analyze popup) use the
qualitative color palette published by Masataka Okabe and Kei Ito,
*"Color Universal Design (CUD) - How to make figures and presentations
that are friendly to Colorblind people"* (2008),
https://jfly.uni-koeln.de/color/ — a fixed list of 8 RGB values, not
software; only the color values themselves are reused
(`frontend/app.js`'s `COLORBLIND_SAFE_PALETTE`), no code or text from that
publication.

## No affiliation

RELION-US is an independent project. It is not produced, endorsed, or
supported by the MRC Laboratory of Molecular Biology, the RELION authors, or
the developers of any other software named in this repository. All product
and company names mentioned are the property of their respective owners and
are used only to identify the software they refer to.
