"""
job_catalog.py — the authoritative RELION job-type table this app is built
from.

Every (internal_name, label_new, display_name, description) row below is
copied directly from src/pipeline_jobs.h (PROC_*_DIRNAME / PROC_*_LABELNEW
defines and their trailing comments, lines ~306-371 in the RELION checkout
this was built against: github.com/3dem/relion, cloned 2026-08-14) — not
invented. `label_new` is RELION's own job-type identifier string (the same
one written into a real job's job.star as rlnJobTypeLabel), included so
job.star files this app writes stay recognizable to actual RELION tooling
if you ever want to open a project in both.

`internal_name` is the token RELION's own source uses in both
`RelionJob::initialise<internal_name>Job()` / `getCommands<internal_name>Job()`
(src/pipeline_jobs.cpp) and `JobWindow::initialise<internal_name>Window()`
(src/gui_jobwindow.cpp) — it's the join key into data/job_definitions_raw.json.

Three custom (non-RELION) entries are appended at the bottom: the IMOD /
Warp-M / DeepETPicker import bridges built earlier in this project. They
follow the same job-popup UI as every RELION job type, but their "command"
is a direct Python call into backend/converters/ rather than a relion_*
subprocess (see job_runner.py).
"""

CATEGORIES = [
    "Import & Conversion",
    "Motion Correction",
    "CTF",
    "Particle Picking",
    "Tilt Series / Tomogram Reconstruction",
    "Extraction",
    "Classification & Refinement",
    "Post-processing & Analysis",
    "Other",
]

# internal_name -> (label_new, display_name, category, description)
JOB_CATALOG = {
    "Import":                    ("relion.import", "Import", "Import & Conversion",
                                   "Import any file as a Node of a given type"),
    "TomoImport":                ("relion.importtomo", "Import Tomo Tilt Series", "Import & Conversion",
                                   "Import for tomography GUI"),
    "Motioncorr":                ("relion.motioncorr", "Motion Correction", "Motion Correction",
                                   "Motion-correct movies/tilt-series frames"),
    "Ctffind":                   ("relion.ctffind", "CTF Estimation", "CTF",
                                   "Estimate CTF parameters from micrographs for either entire micrographs and/or particles"),
    "Ctfrefine":                 ("relion.ctfrefine", "CTF Refinement", "CTF",
                                   "Defocus and beamtilt optimisation"),
    "TomoCtfRefine":             ("relion.ctfrefinetomo", "CTF Refinement (Tomo)", "CTF",
                                   "CTF refinement (defocus & aberrations) for tomography"),
    "Manualpick":                ("relion.manualpick", "Manual Picking", "Particle Picking",
                                   "Manually pick particle coordinates from micrographs"),
    "Autopick":                  ("relion.autopick", "Auto-picking", "Particle Picking",
                                   "Automatically pick particle coordinates from micrographs, their CTF and 2D references"),
    "TomoPickTomograms":         ("relion.picktomo", "Pick Particles (Tomo)", "Particle Picking",
                                   "Pick particles in tomograms"),
    "TomoAlignTiltSeries":       ("relion.aligntiltseries", "Align Tilt Series", "Tilt Series / Tomogram Reconstruction",
                                   "Tilt series alignment for tomogram reconstruction"),
    "TomoReconstructTomograms":  ("relion.reconstructtomograms", "Reconstruct Tomograms", "Tilt Series / Tomogram Reconstruction",
                                   "Reconstruction of tomograms for particle picking"),
    "TomoDenoiseTomograms":      ("relion.denoisetomo", "Denoise Tomograms", "Tilt Series / Tomogram Reconstruction",
                                   "Denoise tomograms"),
    "TomoExcludeTiltImages":     ("relion.excludetilts", "Exclude Tilt Images", "Tilt Series / Tomogram Reconstruction",
                                   "Exclusion of bad tilt-images from tilt-series"),
    "Extract":                   ("relion.extract", "Particle Extraction", "Extraction",
                                   "Window particles, normalize, downsize etc from micrographs"),
    "TomoSubtomo":               ("relion.pseudosubtomo", "Extract Pseudo-subtomograms", "Extraction",
                                   "Creation of pseudo-subtomograms from tilt series images"),
    "Select":                    ("relion.select", "Subset Selection", "Classification & Refinement",
                                   "Interactively select classes/particles"),
    "Class2D":                   ("relion.class2d", "2D Classification", "Classification & Refinement",
                                   "2D classification (from input particles)"),
    "Inimodel":                  ("relion.initialmodel", "Initial Model (SGD)", "Classification & Refinement",
                                   "De-novo generation of 3D initial model (using SGD)"),
    "Class3D":                   ("relion.class3d", "3D Classification", "Classification & Refinement",
                                   "3D classification (from input particles, a 3D reference, and possibly a 3D mask)"),
    "Autorefine":                ("relion.refine3d", "3D Auto-refine", "Classification & Refinement",
                                   "3D auto-refine (from input particles, a 3D reference, and possibly a 3D mask)"),
    "MultiBody":                 ("relion.multibody", "Multi-body Refinement", "Classification & Refinement",
                                   "Multi-body refinement"),
    "TomoAlign":                 ("relion.framealigntomo", "Frame Align / Polish (Tomo)", "Classification & Refinement",
                                   "Frame alignment and particle polishing for subtomography"),
    "TomoReconPart":             ("relion.reconstructparticletomo", "Reconstruct Particle (Tomo)", "Classification & Refinement",
                                   "Calculation of particle average from the individual tilt series images"),
    "Motionrefine":              ("relion.polish", "Bayesian Polishing", "Post-processing & Analysis",
                                   "Motion fitting / Bayesian polishing of particle trajectories"),
    "Maskcreate":                 ("relion.maskcreate", "Mask Creation", "Post-processing & Analysis",
                                   "Create masks from input maps"),
    "Joinstar":                  ("relion.joinstar", "Join STAR Files", "Post-processing & Analysis",
                                   "Combine STAR files"),
    "Subtract":                  ("relion.subtract", "Particle Subtraction", "Post-processing & Analysis",
                                   "Subtract projections of parts of the reference from experimental images"),
    "Postprocess":               ("relion.postprocess", "Post-processing", "Post-processing & Analysis",
                                   "Post-processing (from unfiltered half-maps and possibly a 3D mask)"),
    "Localres":                  ("relion.localres", "Local Resolution", "Post-processing & Analysis",
                                   "Local resolution estimation (from unfiltered half-maps and a 3D mask)"),
    "DynaMight":                 ("dynamight", "DynaMight", "Post-processing & Analysis",
                                   "Modelling continuous heterogeneity"),
    "ModelAngelo":                ("modelangelo", "ModelAngelo", "Post-processing & Analysis",
                                   "Automated atomic model building"),
    "External":                  ("relion.external", "External Job", "Other",
                                   "Run non-RELION programs from within the pipeline"),
}

# Naming reconciliation between gui_jobwindow.cpp's JobWindow::initialise<X>Window()
# and pipeline_jobs.cpp's RelionJob::initialise<X>Job() (see data/extract_job_definitions.py
# GUI_TO_JOB_NAME_ALIASES for where this was first identified and verified by diff).
assert set(JOB_CATALOG) == {
    "Import", "TomoImport", "Motioncorr", "Ctffind", "Ctfrefine", "TomoCtfRefine",
    "Manualpick", "Autopick", "TomoPickTomograms", "TomoAlignTiltSeries",
    "TomoReconstructTomograms", "TomoDenoiseTomograms", "TomoExcludeTiltImages",
    "Extract", "TomoSubtomo", "Select", "Class2D", "Inimodel", "Class3D",
    "Autorefine", "MultiBody", "TomoAlign", "TomoReconPart", "Motionrefine",
    "Maskcreate", "Joinstar", "Subtract", "Postprocess", "Localres", "DynaMight",
    "ModelAngelo", "External",
}, "JOB_CATALOG must exactly match the 32 job types extracted from RELION source"

# Custom (non-RELION) import bridges built earlier in this project. Same
# category/description shape as the table above, so they render identically
# in the Jobs list, but see job_runner.py for how they execute (direct
# in-process Python calls into backend/converters/, not a subprocess).
CUSTOM_JOBS = {
    "ImodImport": {
        "label_new": "custom.imod_import",
        "display_name": "Import from IMOD (.mod)",
        "category": "Import & Conversion",
        "description": "Convert an IMOD .mod point model into a RELION particles.star",
    },
    "WarpImport": {
        "label_new": "custom.warp_import",
        "display_name": "Import from Warp/M",
        "category": "Import & Conversion",
        "description": "Diff and harmonize a Warp/M STAR export against RELION-5's column conventions",
    },
    "DeepETPickerImport": {
        "label_new": "custom.deepetpicker_import",
        "display_name": "Import from DeepETPicker",
        "category": "Import & Conversion",
        "description": "Convert DeepETPicker .coords picks into a RELION particles.star",
    },
}

# --------------------------------------------------------------------------
# SPA vs. tomography pipeline classification — for the Jobs-list SPA/Tomo/All
# toggle. This is a *display filter only*: it never restricts which jobs can
# be opened or run (every job stays reachable via search, or the "All" view,
# no matter what's selected — see frontend/app.js's applyJobFilters(), which
# makes a non-empty search override this filter entirely).
#
# Are SPA/tomography flags available in the project's own STAR files? No —
# checked directly against RELION's own source (github.com/3dem/relion,
# checkout used to build this app, src/pipeliner.cpp PipeLine::write(),
# ~lines 2192-2205): the `pipeline_general` block of default_pipeline.star
# holds only rlnPipeLineJobCounter, nothing describing project "type". The
# closest real signal is per-job: each entry in the `pipeline_processes`
# block carries rlnPipeLineProcessTypeLabel, which is the same string as
# this table's label_new column. See project_manager.detect_pipeline_hint()
# for how that per-job signal is used for the optional auto-switch.
#
# So classification below is a per-job-type heuristic, grounded in what
# actually is verifiable from RELION's source rather than guessed:
#
#   1. Every internal_name RELION itself prefixes with "Tomo" in
#      pipeline_jobs.h is tomography-specific by construction.
#   2. Where a Tomo-prefixed job is the direct sibling of a non-Tomo job
#      doing the analogous step (see each pair's own description text
#      above: Ctfrefine/TomoCtfRefine, Motionrefine/TomoAlign,
#      Autopick+Manualpick/TomoPickTomograms, Extract/TomoSubtomo), the
#      non-Tomo original is single-particle-only — RELION built a whole
#      separate Tomo job rather than reusing it.
#
# Everything else (Import, Motioncorr, Ctffind, and the classification/
# refinement/post-processing jobs from Select through External) is treated
# as shared: RELION-5's tomography pipeline explicitly funnels pseudo
# -subtomograms through the *same* Class2D/Class3D/Inimodel/Autorefine/
# Postprocess/Localres/Maskcreate/etc. jobs SPA particles use — that's the
# documented purpose of "pseudo-subtomograms" (Burt et al. 2024, FEBS Open
# Bio 14(11):1788-1804, PMID 39147729: making tomography particles look
# like ordinary particles.star rows so the same downstream jobs work
# unmodified) — and Motioncorr/Ctffind process movies/tilt-images the same
# way regardless of which pipeline consumes their output afterward.
#
# The three custom bridges are tomography-only in *this app's* scope, per
# each bridge module's own docstring: imod_bridge.py's picks-on-tomograms
# scope, warp_bridge.py's explicit "RELION-5 tomography STAR files" scope,
# and DeepETPicker's own title, "3D particle picking for cryo-electron
# tomography" (Liu et al. 2024, Nat Commun 15:2090, PMID 38453943).
#
# If this grouping doesn't match how you actually use a given job, it's a
# plain set below — edit it directly; nothing else in the app depends on
# the specific assignment beyond which button shows that job by default.
PIPELINE_SPA_ONLY = {
    "Manualpick", "Autopick", "Extract", "Ctfrefine", "Motionrefine",
}
PIPELINE_TOMO_ONLY = {
    "TomoImport", "TomoCtfRefine", "TomoPickTomograms", "TomoAlignTiltSeries",
    "TomoReconstructTomograms", "TomoDenoiseTomograms", "TomoExcludeTiltImages",
    "TomoSubtomo", "TomoAlign", "TomoReconPart",
    "ImodImport", "WarpImport", "DeepETPickerImport",
}


def pipeline_type(internal_name: str) -> str:
    """'spa' | 'tomo' | 'shared' — see PIPELINE_SPA_ONLY / PIPELINE_TOMO_ONLY
    above for the classification and its rationale. Jobs not in either set
    are 'shared' (used by both pipelines, or not pipeline-specific)."""
    if internal_name in PIPELINE_SPA_ONLY:
        return "spa"
    if internal_name in PIPELINE_TOMO_ONLY:
        return "tomo"
    return "shared"


# Guards against a typo silently dropping a job out of every filtered view,
# or a job appearing in both sets (which pipeline_type() would resolve
# arbitrarily via the SPA-first check above — better to fail loudly).
_ALL_INTERNAL_NAMES = set(JOB_CATALOG) | set(CUSTOM_JOBS)
assert PIPELINE_SPA_ONLY <= _ALL_INTERNAL_NAMES, "PIPELINE_SPA_ONLY has an unknown job name"
assert PIPELINE_TOMO_ONLY <= _ALL_INTERNAL_NAMES, "PIPELINE_TOMO_ONLY has an unknown job name"
assert PIPELINE_SPA_ONLY.isdisjoint(PIPELINE_TOMO_ONLY), "a job can't be both SPA-only and Tomo-only"
