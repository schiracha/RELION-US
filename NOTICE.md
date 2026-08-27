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
`starfile`, `mrcfile`, `pillow`, `pytest`, `playwright`).

## No affiliation

RELION-US is an independent project. It is not produced, endorsed, or
supported by the MRC Laboratory of Molecular Biology, the RELION authors, or
the developers of any other software named in this repository. All product
and company names mentioned are the property of their respective owners and
are used only to identify the software they refer to.
