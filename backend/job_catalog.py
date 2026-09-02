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

import functools
import shlex
from dataclasses import dataclass, field
from typing import Callable, Optional

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
                                   "Motion-correct movies"),
    # See TOMO_VARIANT_OF below: real RELION has exactly one RelionJob class
    # for Motioncorr/Ctffind, with is_tomo a runtime flag inside it rather
    # than a separate class (unlike every other Tomo/non-Tomo pair in this
    # table, e.g. Ctfrefine/TomoCtfRefine right below, which really are two
    # distinct classes/labels). RELION-US still gives each its own menu
    # entry -- same label_new as its SPA sibling, since it registers as the
    # exact same real RELION job type -- for consistency with every other
    # pair here and with the sidebar's SPA/Tomo/All filter, rather than a
    # same-popup toggle a user could forget to flip.
    "TomoMotioncorr":            ("relion.motioncorr", "Motion Correction (Tomo)", "Motion Correction",
                                   "Motion-correct tilt-series frames"),
    "Ctffind":                   ("relion.ctffind", "CTF Estimation", "CTF",
                                   "Estimate CTF parameters from micrographs"),
    "TomoCtffind":               ("relion.ctffind", "CTF Estimation (Tomo)", "CTF",
                                   "Estimate CTF parameters for tilt-series"),
    "Ctfrefine":                 ("relion.ctfrefine", "CTF Refinement", "CTF",
                                   "Defocus and beamtilt optimisation"),
    "TomoCtfRefine":             ("relion.ctfrefinetomo", "CTF Refinement (Tomo)", "CTF",
                                   "CTF refinement (defocus & aberrations) for tomography"),
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
    # "(beta)": this job's draft command and options go beyond what
    # RELION's own GUI offers (see SYNTHETIC_OPTIONS' mask_path and the
    # two-command build+hmm_search draft, both source-verified but this
    # app's own addition, not a straight reproduction of RELION -- see
    # the "ModelAngelo" section above DRAFT_OVERRIDES for the full design).
    "ModelAngelo":                ("modelangelo", "ModelAngelo (beta)", "Post-processing & Analysis",
                                   "Automated atomic model building"),
    "External":                  ("relion.external", "External Job", "Other",
                                   "Run non-RELION programs from within the pipeline"),
}

# Naming reconciliation between gui_jobwindow.cpp's JobWindow::initialise<X>Window()
# and pipeline_jobs.cpp's RelionJob::initialise<X>Job() (see data/extract_job_definitions.py
# GUI_TO_JOB_NAME_ALIASES for where this was first identified and verified by diff).
assert set(JOB_CATALOG) == {
    "Import", "TomoImport", "Motioncorr", "TomoMotioncorr", "Ctffind", "TomoCtffind",
    "Ctfrefine", "TomoCtfRefine",
    "Autopick", "TomoPickTomograms", "TomoAlignTiltSeries",
    "TomoReconstructTomograms", "TomoDenoiseTomograms", "TomoExcludeTiltImages",
    "Extract", "TomoSubtomo", "Select", "Class2D", "Inimodel", "Class3D",
    "Autorefine", "MultiBody", "TomoAlign", "TomoReconPart", "Motionrefine",
    "Maskcreate", "Joinstar", "Subtract", "Postprocess", "Localres", "DynaMight",
    "ModelAngelo", "External",
}, (
    "JOB_CATALOG must exactly match the 31 real relion_* job classes this "
    "app runs as a subprocess, PLUS TomoMotioncorr/TomoCtffind -- two extra "
    "menu entries for the SAME two classes (see TOMO_VARIANT_OF below), not "
    "two more classes. Manualpick is deliberately NOT here -- relion_manualpick "
    "is a desktop FLTK GUI (see manualpicker.cpp/displayer.cpp), unusable as "
    "a headless subprocess on a remote/browser-driven backend. It's "
    "registered in CUSTOM_JOBS below instead, under its own real RELION type "
    "label (relion.manualpick) so it still shows up correctly in RELION's "
    "own pipeline/GUI, but its actual picking happens through the in-browser "
    "tomogram/micrograph viewer (viz.py) rather than a subprocess -- see "
    "custom_jobs.py."
)

# Motioncorr/Ctffind are ONE real RelionJob class each, with is_tomo a plain
# runtime flag inside it (set once by which GUI mode launched it -- `relion`
# vs `relion --tomo`) rather than a genuinely separate class the way every
# other Tomo/non-Tomo pair in JOB_CATALOG is (confirmed against RELION
# source: initialiseMotioncorrJob/initialiseCtffindJob in pipeline_jobs.cpp
# build a different JobOption set per is_tomo, but there's only the one
# function, one PROC_* constant, one label). {tomo_name: base_name} --
# job_registry.py resolves every raw_job()/DRAFT_OVERRIDES lookup to
# base_name (the actual RELION job class the options/overrides belong to)
# and sets field_values["is_tomo"] from whichever menu entry (internal_name)
# was actually picked, instead of a same-popup toggle a user could forget to
# flip. See job_registry._resolve_tomo_variant.
TOMO_VARIANT_OF = {
    "TomoMotioncorr": "Motioncorr",
    "TomoCtffind": "Ctffind",
}

# The real RELION type labels TOMO_VARIANT_OF's pairs share -- reading a
# REAL RELION-native project's default_pipeline.star, a process with one of
# these labels could be either variant; the label alone can't say (that's
# the whole reason TOMO_VARIANT_OF exists). internal_name_for_label()'s
# is_tomo parameter disambiguates it using the job's own job.star
# (_rlnJobIsTomo -- see project_manager.read_relion_job_is_tomo). Kept as
# its own set so job_runner.py's Command Center listing only pays for that
# extra job.star read on the jobs that actually need it, not every job in
# the project.
AMBIGUOUS_TOMO_LABELS = {JOB_CATALOG[base][0] for base in set(TOMO_VARIANT_OF.values())}

# Custom (non-RELION) import bridges built earlier in this project. Same
# category/description shape as the table above, so they render identically
# in the Jobs list, but see job_runner.py for how they execute (direct
# in-process Python calls into backend/converters/, not a subprocess).
#
# Manualpick/TomoManualPick are a different kind of "custom" job from the
# import bridges below: their label_new is a REAL RELION type label
# (relion.manualpick / relion.picktomo, the same one TomoPickTomograms uses
# for automated tomogram picking), not a custom.* one -- see
# job_runner.py's _register_in_relion_pipeline, which now checks CUSTOM_JOBS
# for a label_new the same way it checks JOB_CATALOG, and pipeline_bridge's
# module docstring for why registering under the real label matters: it's
# what lets `relion_pipeliner --addJobFromStar` (called on Run, same as any
# other job when two-way sync is on) recognize the job type, validate it,
# and compute its real output nodes -- so a picking job registered here
# shows up correctly in RELION's own pipeline/GUI and its output is a valid
# input to any real downstream RELION job (Extract, TomoSubtomo, ...),
# exactly as if a real relion_manualpick/relion_python_tomo_pick had
# produced it. The import bridges below use custom.* labels precisely
# because they AREN'T standing in for a real RELION job type -- registering
# those under a label relion_pipeliner has never heard of would just fail
# (harmlessly; _register_in_relion_pipeline's caller already falls back to
# this app's own numbering on any registration error).
CUSTOM_JOBS = {
    "Manualpick": {
        "label_new": "relion.manualpick",
        "display_name": "Manual Picking",
        "category": "Particle Picking",
        "description": "Manually pick particle coordinates from micrographs",
    },
    "TomoManualPick": {
        "label_new": "relion.picktomo",
        "display_name": "Manual Picking (Tomo)",
        "category": "Particle Picking",
        "description": "Manually pick particles in tomograms",
    },
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
    "AreTomoImport": {
        "label_new": "custom.aretomo_import",
        "display_name": "Import from AreTomo2 (.aln)",
        "category": "Import & Conversion",
        "description": "Convert an AreTomo2 .aln alignment into IMOD-style .xf/.tlt for RELION's IMOD import",
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
# Everything else (Import, and the classification/refinement/post-processing
# jobs from Select through External) is treated as shared: RELION-5's
# tomography pipeline explicitly funnels pseudo-subtomograms through the
# *same* Class2D/Class3D/Inimodel/Autorefine/Postprocess/Localres/
# Maskcreate/etc. jobs SPA particles use — that's the documented purpose of
# "pseudo-subtomograms" (Burt et al. 2024, FEBS Open Bio 14(11):1788-1804,
# PMID 39147729: making tomography particles look like ordinary
# particles.star rows so the same downstream jobs work unmodified).
# Motioncorr/Ctffind are NOT in this "shared" bucket despite also being one
# real RELION class each (see TOMO_VARIANT_OF) -- each now has its own
# dedicated Tomo menu entry, so (unlike Class2D etc, which have no such
# split) there's a real internal_name to classify SPA-only vs Tomo-only by.
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
    "Motioncorr", "Ctffind",
}
PIPELINE_TOMO_ONLY = {
    "TomoImport", "TomoCtfRefine", "TomoPickTomograms", "TomoManualPick",
    "TomoAlignTiltSeries", "TomoMotioncorr", "TomoCtffind",
    "TomoReconstructTomograms", "TomoDenoiseTomograms", "TomoExcludeTiltImages",
    "TomoSubtomo", "TomoAlign", "TomoReconPart",
    "ImodImport", "WarpImport", "DeepETPickerImport", "AreTomoImport",
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

# --------------------------------------------------------------------------
# Output-directory naming (Command Center: job history table/timeline) —
# copied directly from src/pipeline_jobs.h's PROC_*_DIRNAME defines
# (~lines 306-337 in the checkout this app is built against), the same way
# label_new above is copied from PROC_*_LABELNEW. This is what RELION's own
# GUI uses as the per-job-type output directory prefix (e.g. `MotionCorr/`),
# with a project-wide job number as the subfolder (e.g. `MotionCorr/job002/`)
# — RELION-US mirrors that convention for real jobs' default output
# directories, so "job name" (job number, or an alias once you rename one)
# behaves the way it does in real RELION: numeric order unless renamed.
#
# Note some SPA/Tomo sibling pairs share the same real RELION dirname (e.g.
# TomoImport and Import both use "Import/", TomoSubtomo and Extract both use
# "Extract/") — that's RELION's own convention, not a bug here; the job
# number subfolder still keeps every run's directory unique.
JOB_DIRNAME = {
    "Import": "Import",
    "TomoImport": "Import",
    "Motioncorr": "MotionCorr",
    # Same dirname/job-number sequence as its SPA sibling -- both register
    # under the SAME real RELION type label (see TOMO_VARIANT_OF), so this
    # is what RELION's own pipeliner would allocate for either one anyway.
    "TomoMotioncorr": "MotionCorr",
    "Ctffind": "CtfFind",
    "TomoCtffind": "CtfFind",
    "Ctfrefine": "CtfRefine",
    "TomoCtfRefine": "CtfRefine",
    "Manualpick": "ManualPick",
    "Autopick": "AutoPick",
    "TomoPickTomograms": "Picks",
    # Same dirname as TomoPickTomograms, deliberately: both register under
    # the same real RELION type label (relion.picktomo -- see CUSTOM_JOBS
    # above) and share its job-number sequence, matching how RELION itself
    # numbers every job in a project from one counter regardless of type
    # (see this table's own docstring above).
    "TomoManualPick": "Picks",
    "TomoAlignTiltSeries": "AlignTiltSeries",
    "TomoReconstructTomograms": "Tomograms",
    "TomoDenoiseTomograms": "Denoise",
    "TomoExcludeTiltImages": "ExcludeTiltImages",
    "Extract": "Extract",
    "TomoSubtomo": "Extract",
    "Select": "Select",
    "Class2D": "Class2D",
    "Inimodel": "InitialModel",
    "Class3D": "Class3D",
    "Autorefine": "Refine3D",
    "MultiBody": "MultiBody",
    "TomoAlign": "Polish",
    "TomoReconPart": "Reconstruct",
    "Motionrefine": "Polish",
    "Maskcreate": "MaskCreate",
    "Joinstar": "JoinStar",
    "Subtract": "Subtract",
    "Postprocess": "PostProcess",
    "Localres": "LocalRes",
    "DynaMight": "DynaMight",
    "ModelAngelo": "ModelAngelo",
    "External": "External",
    # Custom (non-RELION) jobs have no real PROC_*_DIRNAME -- given each
    # its own unambiguous prefix instead (not a real RELION directory name,
    # so no collision risk with the real ones above).
    "AreTomoImport": "AreTomoImport",
    "ImodImport": "ImodImport",
    "WarpImport": "WarpImport",
    "DeepETPickerImport": "DeepETPickerImport",
}

assert set(JOB_DIRNAME) == _ALL_INTERNAL_NAMES, "JOB_DIRNAME must cover every job type"


def job_dirname(internal_name: str) -> str:
    """RELION-style output-directory prefix for this job type (see
    JOB_DIRNAME above). Falls back to internal_name for any job type added
    here in the future before its real dirname is looked up."""
    return JOB_DIRNAME.get(internal_name, internal_name)

@functools.lru_cache(maxsize=1)
def _label_to_internal() -> dict[str, str]:
    """RELION's process type label -> this app's internal job name."""
    out = {label_new: name for name, (label_new, *_rest) in JOB_CATALOG.items()}
    out.update({meta["label_new"]: name for name, meta in CUSTOM_JOBS.items()})
    return out


def internal_name_for_label(type_label: str, is_tomo: bool = False) -> str | None:
    """Reverse of a job's `label_new`, for reading RELION's own
    `default_pipeline.star` (whose rows carry only the type label).

    RELION appends a sub-label to the base type for many jobs -- `label +=
    ".movies"`, `".em"`, `".topaz"`, 35 places in pipeline_jobs.cpp -- so a
    real project records "relion.class2d.em" where this catalog holds
    "relion.class2d". Longest matching base wins, so "relion.class2d" is not
    mistaken for a prefix of something more specific that also exists.

    is_tomo: which of a TOMO_VARIANT_OF pair to resolve a shared label
    (relion.motioncorr / relion.ctffind) to -- the label alone can't say,
    since real RELION uses the SAME one for both the SPA and Tomo form (see
    TOMO_VARIANT_OF's docstring). Read the job's own job.star
    (_rlnJobIsTomo) to get this -- see
    project_manager.read_relion_job_is_tomo. Ignored for every other label,
    which isn't ambiguous in the first place.
    """
    label = (type_label or "").strip()
    if not label:
        return None
    for tomo_name, base_name in TOMO_VARIANT_OF.items():
        if JOB_CATALOG[base_name][0] == label:
            return tomo_name if is_tomo else base_name
    table = _label_to_internal()
    if label in table:
        return table[label]
    candidates = [base for base in table if label.startswith(base + ".")]
    if not candidates:
        return None
    return table[max(candidates, key=len)]




# --------------------------------------------------------------------------
# Draft-command overrides for jobs the generic draft heuristic gets wrong
# --------------------------------------------------------------------------
#
# job_registry._build_draft_command() drafts a command by matching each
# option key against a `--<key>` flag literally present in the job's real
# RELION source (flags_used). That rule is correct for the ~27 core
# RELION programs, whose CLI flag names match their internal option keys.
# It breaks for a handful of jobs in various specific, source-verified ways
# -- collected here as ONE typed override per job type (JobDraftOverride)
# rather than several same-shaped tables each independently keyed by
# internal_name (the shape this table replaced: a separate DRAFT_FLAG_MAP,
# DRAFT_NEGATED_FLAGS, DRAFT_PROGRAM_OVERRIDE, DRAFT_SUPPRESS,
# DRAFT_VALUE_TRANSFORM, DRAFT_OUTPUT_FLAG and DRAFT_OUTPUT_SUFFIX, each
# needing its own accessor and its own "is this job in the table" check) --
# so a job's full set of exceptions lives in one place, and a new one has an
# obvious single spot to go instead of a new same-shaped table to invent.
#
# None of this is a mechanical reimplementation of RELION's per-job command
# branching (which this project deliberately avoids): every entry is a
# small, individually-cited, source-verified fact about one job, most of
# them confirmed for real against an installed RELION 5.0.1 (not just read
# off the source), and a genuinely branched job (TomoPickTomograms) is left
# out of `flags`/`value_transforms` entirely rather than risk a
# subtly-wrong reconstruction -- its default draft stays program-name-only
# (plus, here, just its --output-directory flag) with the real source shown
# for hand-editing. TomoDenoiseTomograms is a partial exception (issue
# #24): its cryoCARE:train/predict subcommand-select booleans are still a
# genuine branch (see program_extra below), but the flags each mode gates
# ARE plain single-flag toggles once the mode is known, so those five are
# mapped normally via `flags` instead of being left fully unmapped.


@dataclass(frozen=True)
class FlagOverride:
    """One option key's verified real CLI flag, replacing the generic
    `--<key>` rule for a job whose flag name genuinely differs, or whose
    self-guard the extractor couldn't see (see job_registry.py's
    _build_draft_command for how flags_used/option_flags normally resolve
    this without needing an override at all). Always emitted once
    `condition` (if any) holds -- see job_registry._evaluate_condition for
    the bare-identifier / joboptions[...] shapes a condition string can
    take."""
    flag: str
    # Needed only when this flag is shared by two mutually exclusive option
    # keys that both carry their own non-empty RELION default (so the
    # generic empty-value skip can't tell which one is active) -- see
    # Import's fn_in_raw/fn_in_other below.
    condition: Optional[str] = None
    # True for the rare flag that fires when its OWN checkbox is UNCHECKED,
    # not checked -- RELION guards these with a bare
    # `if (!joboptions["key"].getBoolean())`, the opposite of the field's
    # own display sense ("Use parallel disc I/O?" defaults to Yes;
    # unchecking it is what adds `--no_parallel_disc_io`). Every other flag
    # in this table fires on checked, matching RELION's overwhelmingly more
    # common convention, so this defaults to False.
    negated: bool = False
    # For the rarer case still: the SAME option's value needs to go out
    # under a DIFFERENT flag when `condition` is false, rather than being
    # omitted entirely -- e.g. TomoImport's dose_rate, which RELION emits as
    # either `--dose-per-movie-frame <value>` or `--dose-per-tilt-image
    # <value>` depending on a sibling checkbox (see its own entry below).
    # Only meaningful when `condition` is set; `flag` above is used when the
    # condition holds, this one when it doesn't.
    flag_if_condition_false: Optional[str] = None


@dataclass(frozen=True)
class JobDraftOverride:
    """Every source-verified draft-command exception for ONE job type, in
    one place. Every field defaults to "no override, use the generic
    rule" -- a job only needs an entry here for the fields it actually
    overrides, not all of them."""
    # Verified program string, for a job whose extracted program_guess
    # picked the wrong branch's literal `command = "..."` (currently only
    # TomoImport -- see its entry below for why).
    program: Optional[str] = None
    # Extra command-line tokens inserted immediately after the resolved
    # program name, BEFORE output_flag/subdir/the per-option loop -- for a
    # RELION subcommand-style positional token a click/typer-based CLI
    # requires in that EXACT position (confirmed for real:
    # tomography_python_programs/denoise/_cli.py's `typer.Typer()`
    # multi-command app routes on the FIRST positional argument; a --flag
    # appearing before the subcommand name is not tolerated). Distinct
    # from extra_output_args (runs AFTER output_flag/subdir) and
    # extra_flags (runs at the very end) -- those two don't care about
    # position, this one does. Returns [] for "nothing to add" (e.g. no
    # mode selected yet). Takes (field_values, output_subdir) -- the second
    # argument exists for the "exe-placeholder" jobs (DynaMight/ModelAngelo/
    # External), whose usual automatic --o/subdir insertion is skipped
    # entirely (see _build_draft_command), so this is the only hook of the
    # three that still sees output_subdir for them; ModelAngelo's own entry
    # below is the first (and so far only) user of it. Existing callers
    # that don't need it just ignore the second parameter.
    program_extra: Optional[Callable[[dict, str], list]] = None
    # RELION's output-directory flag for this job. `--o` is the default
    # every job gets when this is left None; only a genuine difference
    # (`--output-directory`, `--odir`) needs an entry.
    output_flag: Optional[str] = None
    # Literal suffix RELION appends to the output directory to form a file
    # ROOTNAME PREFIX rather than a plain folder (e.g. "run" turns
    # `Refine3D/job001/` into `Refine3D/job001/run`, so output files become
    # `run_it000_class001.mrc` instead of a wrong, un-prefixed
    # `_it000_class001.mrc`).
    output_suffix: Optional[str] = None
    # option_key -> its real flag (+ optional condition/negation).
    flags: dict[str, FlagOverride] = field(default_factory=dict)
    # Option keys belonging to a non-default branch, omitted from the
    # default draft entirely (and not counted as "unmapped" in the API
    # response, since they're deliberately omitted rather than un-handled).
    suppress: frozenset[str] = frozenset()
    # option_key -> {label: real CLI value}, for a "radio" field whose
    # stored value is a human-facing label (e.g. "No rotation (0)") that
    # RELION's own getCommands*Job() translates before putting it on the
    # command line, rather than using the label verbatim. A label not in
    # this map is treated as unmapped rather than passed through, so a
    # RELION version change that renames a label can't silently emit the
    # wrong value.
    value_transforms: dict[str, dict[str, str]] = field(default_factory=dict)
    # option_key -> a callable computing the real CLI value from this
    # field's OWN raw numeric value (already known non-empty/non-None by
    # the time this runs -- the ordinary empty-value skip happens first),
    # returning None to mean "correctly omit this flag" (RELION's own
    # guard on the COMPUTED value itself, e.g. helical_range_distance <=
    # 0) rather than "can't resolve". Distinct from value_transforms (a
    # label->string LOOKUP for radio fields) -- this is a numeric
    # COMPUTATION (clamp/divide) for slider/number fields.
    numeric_transforms: dict[str, Callable[[float], Optional[float]]] = field(default_factory=dict)
    # Extra command-line tokens to append right after the output
    # flag/subdir, computed from the CURRENT field values -- for a
    # compulsory argument RELION derives from OTHER fields rather than
    # reading directly off one JobOption (e.g. Import's --ofile and
    # --do_movies/--do_micrographs, both picked from is_multiframe's own
    # two-way branch; see _import_mode_and_ofile_args below). Returns []
    # for "nothing to add" (e.g. the branch this
    # computation covers isn't the one currently active). One of two slots
    # in this table that run code instead of holding data (see
    # extra_flags below for the other) -- reserved for exactly this
    # "compulsory value, computed from a couple of already-known fields,
    # not itself a JobOption" shape; anything bigger belongs in the
    # editable command box by hand instead, per this project's policy
    # against reconstructing real per-job branching. The two are kept
    # separate rather than merged into one hook because their POSITION in
    # the command differs and matters for readability (this one is
    # anchored right after the output flag/subdir; extra_flags has no
    # fixed position) -- not because they need different underlying
    # mechanics; both take the full field_values dict and return a flat
    # token list.
    extra_output_args: Optional[Callable[[dict], list]] = None
    # Extra command-line tokens computed from the FULL field_values dict
    # (not just one option's own value) and appended once per job, near the
    # end of the draft command -- for a value RELION computes from MULTIPLE
    # other fields with real conditional/branch logic that neither
    # FlagOverride (one option -> one flag) nor numeric_transforms (one
    # option's OWN value -> a computed value) can express, e.g. Extract's
    # --bg_radius (computed from bg_diameter, extract_size, and
    # conditionally rescale -- see _extract_bg_radius_flags below) or a
    # branch whose ELSE case is a hardcoded literal rather than any field's
    # value (Extract's --helical_nr_asu/--helical_rise fallback -- see
    # _extract_helical_nr_asu_rise_fallback_flags below). Returns [] for
    # "nothing to add right now" (the relevant branch isn't active).
    # Distinct from extra_output_args (see its own comment for why they're
    # separate hooks rather than one). Deliberately keyed per-JOB, not
    # per-option like flags/value_transforms/numeric_transforms: a value
    # computed from several fields at once (bg_radius has no single
    # "owning" option -- not bg_diameter alone, not do_norm alone) has no
    # natural single key to live under, so forcing one would be arbitrary;
    # a job needing several genuinely-independent computed-token groups
    # (as Extract does here, for #17 and #18 together) combines them in
    # its own small composed function instead (see _extract_extra_flags).
    # Takes (field_values, output_subdir) -- same reasoning as
    # program_extra's own second parameter above; ModelAngelo's hmm_search
    # second-command builder is the first user of it here too (it needs
    # output_subdir for -i/-o, which reuse the build step's own directory).
    extra_flags: Optional[Callable[[dict, str], list]] = None
    # A job whose real getCommands*Job() calls commands.push_back() MORE
    # THAN ONCE, where at least one extra command needs to run BEFORE the
    # primary one this table otherwise builds (e.g. Localres's ResMap mode,
    # which symlinks the two half-maps into the job directory before
    # invoking ResMap itself; TomoPickTomograms's optional particles-from-
    # star pass). Returns a list of COMPLETE, already-fully-formed shell
    # command strings (unlike extra_flags, which returns bare TOKENS
    # appended to the one command already under construction -- these are
    # independent commands with their own program and flags), joined with
    # real RELION's own " && " (see prepareFinalCommand, src/pipeline_jobs.
    # cpp ~730-765: multiple commands.push_back() calls are joined with "
    # && " into ONE final_command string -- this app's job_runner already
    # runs the whole draft through asyncio.create_subprocess_shell, so a
    # literal `&&` in the draft just works, exactly as it does for a command
    # AFTER the primary one -- see extra_flags' ModelAngelo entry for that
    # half of the same mechanism). [] for "nothing to prepend right now".
    # Takes (field_values, output_subdir).
    commands_before: Optional[Callable[[dict, str], list]] = None
    # For the rare job whose real PROGRAM depends on field_values, not a
    # single fixed string `program` above can express (e.g. Localres, whose
    # command is either the user-configured fn_resmap executable or
    # relion_postprocess, chosen by which of two mutually-exclusive
    # checkboxes is on -- program_guess can only ever guess one). Takes
    # field_values, returns the resolved program string, or None to fall
    # back to `program`/program_guess unchanged. When this returns non-None,
    # job_registry._build_draft_command skips its OWN generic nr_mpi>1 ->
    # program_mpi swap entirely (the override fully owns program resolution
    # for that branch) -- so a job needing an MPI variant for one of its own
    # branches must resolve that itself (see Localres's own entry: only its
    # do_resmap_locres branch overrides, which never has an MPI form of its
    # own, so the untouched do_relion_locres branch keeps the normal
    # program_guess/program_mpi behavior automatically).
    program_override: Optional[Callable[[dict], Optional[str]]] = None
    # For the rare job whose default --o/output_subdir insertion (RELION's
    # own convention for almost every job) is flat WRONG for one of its
    # branches -- e.g. Localres's ResMap mode, which never takes a --o flag
    # at all (ResMap is a third-party argparse program with no such option;
    # RELION's own getCommandsLocresJob only adds --o inside the OTHER,
    # do_relion_locres branch). Takes field_values, returns True to skip the
    # generic --o emission entirely for this draft. The branch that DOES
    # want the normal --o still gets it automatically (output_suffix above
    # stays a static per-job default, applied whenever this returns False/
    # is unset) -- only the branch that must NOT have --o needs this hook.
    suppress_output_flag: Optional[Callable[[dict], bool]] = None


def _import_mode_and_ofile_args(field_values: dict) -> list:
    """relion_import's --ofile is compulsory (src/apps/import.cpp:49) and
    has no default -- without it the binary refuses to run at all
    (confirmed for real, running an Import job against RELION 5.0.1). Its
    value is a literal RELION picks per branch, not a JobOption string this
    app can just read: do_raw's is_multiframe checkbox selects
    "movies.star" vs "micrographs.star" (pipeline_jobs.cpp ~1312-1324) -- a
    plain two-way pick on one already-known boolean, safe to compute here.

    is_multiframe ALSO selects one of --do_movies/--do_micrographs
    (pipeline_jobs.cpp ~1310-1320) -- and unlike --ofile, this one is not
    just missing a value, it's missing ENTIRELY without this fix: relion_import
    refuses to run at all without exactly one of --do_movies/--do_micrographs/
    --do_coordinates/--do_halfmaps/--do_other (confirmed for real, running
    the do_raw-branch draft against RELION 5.0.1 failed immediately with
    "ERROR: you can only use only one, and at least one, of the options
    --do_movies, --do_micrographs, --do_coordinates, --do_halfmaps or
    --do_other" -- an earlier version of this function computed --ofile but
    left is_multiframe itself unmapped, on the mistaken assumption that its
    only effect was picking the output filename).

    do_other's fn_out is derived from fn_in_other itself (basename, or a
    coords_suffix construction for the coordinate-import case) -- genuine
    per-node-type branch logic this app deliberately doesn't try to
    reconstruct (same policy as TomoImport's do_coords branch); that half
    (including --do_coordinates/--do_other themselves) is left for the user
    to add via the editable command box."""
    if not field_values.get("do_raw"):
        return []
    if field_values.get("is_multiframe"):
        return ["--do_movies", "--ofile", "movies.star"]
    return ["--do_micrographs", "--ofile", "micrographs.star"]


def _autopick_log_flags(field_values: dict, output_subdir: str) -> list:
    """Autopick's LoG-picking branch (pipeline_jobs.cpp ~2271-2293, `else if
    (joboptions["do_log"].getBoolean()) { ... }`) is reached via an else-if
    chain, so the extractor's per-option condition for every field inside it
    (log_diam_min, log_diam_max, log_maxres, log_adjust_thr, log_upper_thr)
    came out as the compound "else && joboptions[\"do_log\"].getBoolean()"
    -- job_registry._evaluate_condition correctly refuses to guess at the
    bare "else" term (see its own docstring), so ALL FIVE of these fields
    fell out as unmapped, silently dropping every actual LoG picking
    parameter from the draft. Confirmed for real: with do_log checked, the
    draft command was just "`which relion_autopick` --o AutoPick/job003/"
    -- no diameter, no threshold, nothing. log_invert (this job's OTHER
    do_log-gated field) already had a correctly-verified condition="do_log"
    override below; this covers the rest, plus two further literals that
    have no owning JobOption at all: the bare `--LoG` mode flag and
    `--shrink 0` (log_maxres's own value only feeds `--lowpass`, per the
    source's `--shrink 0 --lowpass " + joboptions["log_maxres"]` on one
    line). log_upper_thr is additionally gated on ITS OWN value (< 999.),
    not a boolean field -- FlagOverride's condition mechanism evaluates
    boolean gates only, so that comparison has to live here too."""
    if not field_values.get("do_log"):
        return []
    tokens = [
        "--LoG",
        "--LoG_diam_min", str(field_values.get("log_diam_min", "")),
        "--LoG_diam_max", str(field_values.get("log_diam_max", "")),
        "--shrink", "0",
        "--lowpass", str(field_values.get("log_maxres", "")),
        "--LoG_adjust_threshold", str(field_values.get("log_adjust_thr", "")),
    ]
    upper_thr = field_values.get("log_upper_thr")
    if upper_thr is not None and upper_thr != "":
        try:
            if float(upper_thr) < 999.0:
                tokens += ["--LoG_upper_threshold", str(upper_thr)]
        except (TypeError, ValueError):
            pass
    return tokens


def _div_by_3(value: float) -> float:
    """RELION's own conversion from a JobOption's raw 'degrees' UI value to
    the sigma it actually passes to relion_refine (e.g. Class3D's
    sigma_angles -> --sigma_ang, pipeline_jobs.cpp ~4003-4005:
    `floatToString(sigma_angles / 3.)`) -- no clamp, no positivity gate on
    its own; _clamp_0_90_then_third and _third_if_positive below both
    build on this."""
    return value / 3.0


def _clamp_0_90_then_third(value: float) -> float:
    """Class3D/Autorefine's range_tilt/range_psi/range_rot ->
    --sigma_tilt/--sigma_psi/--sigma_rot (pipeline_jobs.cpp ~4077-4098,
    Class3D): clamped to [0, 90] degrees, THEN divided by 3. Always
    emitted once the field's own FlagOverride.condition holds -- no
    positivity gate of its own, unlike helical_range_distance below."""
    return _div_by_3(min(max(value, 0.0), 90.0))


def _third_if_positive(value: float) -> Optional[float]:
    """Class3D/Autorefine's helical_range_distance -> --helical_sigma_
    distance (pipeline_jobs.cpp ~4099-4100, Class3D; ~4580-4583,
    Autorefine): RELION only emits this flag when the raw value is > 0
    (`if (val > 0.)`, a guard on the COMPUTED value itself, not a
    joboptions condition) -- returns None to mean "correctly omit", which
    _build_draft_command's caller must NOT count as unmapped."""
    return _div_by_3(value) if value > 0.0 else None


def _safe_float(value, default: float) -> Optional[float]:
    """Best-effort float parse for a slider/number field's current raw
    value, or `default` when the value itself is empty/None (the ordinary
    "field left blank" case, same convention as the empty-value skip
    elsewhere in this module) -- None (not an exception) when it's some
    other unparseable value, so callers can treat that as "can't
    confidently resolve" without a try/except of their own."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_bg_radius_flags(field_values: dict) -> list:
    """Extract's --bg_radius (pipeline_jobs.cpp ~2584-2600): not a
    passthrough of any single field -- RELION computes it from bg_diameter
    (falling back to 75% of extract_size when negative, its own JobOption
    default), halves it to a radius, and -- only when do_rescale is on --
    rescales it by rescale/extract_size, finally truncating to an int.
    do_norm's own "--norm" flag is a separate, plain FlagOverride (see the
    Extract entry below) -- kept OUT of this computation on purpose so it
    still appears even in the (rare, mid-edit) case this function bails
    out below; only the flags nothing else expresses live here.
    white_dust/black_dust are separate, already-correctly-mapped fields
    (their own option_flags entry has the right do_norm condition) and
    --scale (from do_rescale/rescale) is likewise already mapped
    elsewhere. Returns [] when do_norm is off, or when extract_size can't
    be read as a real positive number (the whole computation is
    meaningless without a box size -- a real, if narrow, reachable state:
    extract_size is compulsory but a draft recompute fires on every
    keystroke, so a momentarily-cleared field mid-edit reaches here as an
    empty string/None before the user finishes typing a new value)."""
    if not field_values.get("do_norm"):
        return []
    extract_size = _safe_float(field_values.get("extract_size"), 0.0)
    bg_diameter = _safe_float(field_values.get("bg_diameter"), -1.0)
    if extract_size is None or bg_diameter is None or extract_size <= 0:
        return []
    bg_radius = (0.75 * extract_size if bg_diameter < 0 else bg_diameter) / 2.0
    if field_values.get("do_rescale"):
        rescale = _safe_float(field_values.get("rescale"), 0.0)
        if rescale is None:
            return []
        bg_radius = bg_radius * rescale / extract_size
    return ["--bg_radius", str(int(bg_radius))]


def _extract_helical_nr_asu_rise_fallback_flags(field_values: dict) -> list:
    """Extract's helical-tube-extraction else-branch (pipeline_jobs.cpp
    ~2620-2629): when tubes ARE cut into segments (do_cut_into_segments),
    --helical_nr_asu/--helical_rise take their real field values -- already
    correctly mapped via this table's ordinary FlagOverride entries for
    those two keys (condition do_extract_helix && do_extract_helical_tubes
    && do_cut_into_segments). When NOT cutting into segments, RELION
    hardcodes the literal `--helical_nr_asu 1 --helical_rise 1` instead,
    ignoring the fields entirely (issue #18) -- mutually exclusive with the
    condition above (exactly one of the two can hold at a time), so this
    never double-emits alongside the already-mapped true-branch flags."""
    if (
        field_values.get("do_extract_helix")
        and field_values.get("do_extract_helical_tubes")
        and not field_values.get("do_cut_into_segments")
    ):
        return ["--helical_nr_asu", "1", "--helical_rise", "1"]
    return []


def _extract_coords_flags(field_values: dict) -> list:
    """Extract's coordinate-input flag (pipeline_jobs.cpp ~2524-2541, the
    do_reextract==false branch -- a fresh extraction, not a re-extraction):
    `FileName mylist = joboptions["coords_suffix"].getString();` is read
    into a local variable first, so the extractor's per-option scan (which
    only looks for `joboptions["key"]` directly beside a `command +=`)
    never sees it at all -- coords_suffix was completely unmapped,
    confirmed for real: a from-scratch Extract job with coords_suffix
    filled in produced a draft with no coordinate input whatsoever.

    Worse than a plain miss, though: which FLAG applies depends on the
    VALUE's own content, via RELION's own `mylist.contains("coords_suffix")`
    "attempt at backwards compatibility" check -- true for the OLD
    per-micrograph `coords_suffix_autopick.star` naming, splitting into
    `--coord_dir <dir>/ --coord_suffix <name-without-"coords_suffix">`;
    false for the current 2-column-list format RELION5's own AutoPick job
    now writes (its own source comment: "new version: no longer save
    coords_suffix nodetype, but 2-column list..."), which instead takes a
    single `--coord_list <path>` verbatim. Confirmed for real against a
    live AutoPick/job.../autopick.star (the ordinary, current-format case,
    with no "coords_suffix" substring in its path) -- needs --coord_list,
    not the old split form.

    beforeLastOf/afterLastOf (filename.cpp:177-203) both return the WHOLE
    string when the separator isn't found at all, not "" -- confirmed by
    reading their real implementation, since Python's rpartition("/") on a
    separator-less string gives ("", "", whole_string), the opposite
    convention for the "before" half. Unreachable through this app's own
    AutoPick -> Extract flow (AutoPick's own output paths always include a
    job subdirectory), but matched exactly anyway for a manually-typed or
    externally-imported coords_suffix path with no "/" in it at all."""
    if field_values.get("do_reextract"):
        return []
    mylist = str(field_values.get("coords_suffix") or "")
    if not mylist:
        return []
    if "coords_suffix" in mylist:
        if "/" in mylist:
            directory, _, name = mylist.rpartition("/")
        else:
            directory, name = mylist, mylist
        return ["--coord_dir", directory + "/", "--coord_suffix", name.replace("coords_suffix", "")]
    return ["--coord_list", mylist]


def _extract_extra_flags(field_values: dict, output_subdir: str = "") -> list:
    """Extract's DRAFT_OVERRIDES.extra_flags: combines the multi-field/
    branch-dependent groups the generic per-option rule and the other
    transform mechanisms can't express (issues #17 and #18, plus the
    coords_suffix local-variable miss) -- see each builder above for its
    own reasoning. --part_dir (this job's own output flag) is handled the
    ordinary automatic way via output_flag on this job's JobDraftOverride;
    output_subdir is used here only for --part_star below, which RELION
    computes as a full path rather than letting relion_preprocess combine
    part_dir+part_star itself.

    `command += " --extract";` (~2569) is a bare unconditional literal with
    no owning JobOption -- same shape as Ctffind's --is_ctffind4/Autopick's
    --LoG, so the generic per-option extractor can't find it either.
    Confirmed for real: relion_preprocess refused to run at all with
    "ERROR: Provide either --extract or --operate_on" -- every Extract job
    this app ever built silently omitted its own mode flag.

    `FileName fn_ostar = outputname + "particles.star"; command += "
    --part_star " + fn_ostar;` (~2547-2549) is likewise unconditional
    (after the reextract/fresh if-else closes, applying to both) and
    likewise invisible to the per-option extractor (no joboptions[] on
    either line). Confirmed for real: relion_preprocess ran to completion
    ("Done preprocessing!", real per-micrograph .mrcs stacks written) but
    never wrote the combined particles.star every downstream job (Class2D,
    the tutorial's very next step) needs -- a job that reports success
    while silently not producing the one output the rest of the pipeline
    depends on is a worse failure mode than an honest crash."""
    subdir = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    return (
        ["--extract", "--part_star", subdir + "particles.star"]
        + _extract_bg_radius_flags(field_values)
        + _extract_helical_nr_asu_rise_fallback_flags(field_values)
        + _extract_coords_flags(field_values)
    )


def _tomo_other_half(filename: str) -> Optional[str]:
    """Python port of FileName::getTheOtherHalf (src/filename.cpp:456-472,
    confirmed current): operates on the BASENAME only (a directory
    component containing "half1"/"half2" in its own name is left
    untouched), replaces ALL occurrences (str.replace with no count arg
    already does this, same as C++'s replaceAllSubstrings), case-sensitive.
    None if neither "half1" nor "half2" appears anywhere in the basename --
    real RELION hard-errors and refuses to build the command in that case;
    this app's policy is to omit the flags entirely instead (see
    _tomo_ref1_ref2_flags below), not guess."""
    directory, sep, basename = filename.rpartition("/")
    if "half1" in basename:
        new_basename = basename.replace("half1", "half2")
    elif "half2" in basename:
        new_basename = basename.replace("half2", "half1")
    else:
        return None
    return f"{directory}{sep}{new_basename}" if sep else new_basename


def _tomo_ref1_ref2_flags(field_values: dict, output_subdir: str = "") -> list:
    """TomoAlign/TomoCtfRefine's in_halfmaps -> --ref1/--ref2
    (getCommandsTomoAlignJob ~7328-7347, getCommandsTomoCtfRefineJob
    ~7189-7208, byte-identical logic, confirmed current): fn_half2 is
    derived from fn_half1 by FileName::getTheOtherHalf (a half1<->half2
    string swap on the basename only -- see _tomo_other_half above), not
    read from any field of its own. Emits nothing at all (never a
    half-complete --ref1 alone) when in_halfmaps is empty or the swap
    fails -- matching real RELION's own hard error in that case; this
    app's policy is to silently omit rather than guess. Shared verbatim by
    both jobs since the C++ logic is identical in both getCommands*Job()s.
    Values are shell-quoted (unlike Extract's earlier extra_flags
    builders, which only ever emitted computed numbers) since in_halfmaps
    is a real filesystem path that can contain spaces."""
    fn_half1 = str(field_values.get("in_halfmaps") or "").strip()
    if not fn_half1:
        return []
    fn_half2 = _tomo_other_half(fn_half1)
    if fn_half2 is None:
        return []
    return ["--ref1", shlex.quote(fn_half1), "--ref2", shlex.quote(fn_half2)]


def _tomo_denoise_subcommand_tokens(field_values: dict, output_subdir: str = "") -> list:
    """TomoDenoiseTomograms's mode selector (getCommandsTomoDenoiseTomogramsJob
    ~6842-6853, confirmed current): the constant base program
    (`which relion_python_tomo_denoise`) is followed by a Click/Typer
    subcommand token, "cryoCARE:train" or "cryoCARE:predict", picked by
    two independent booleans real RELION itself requires to be mutually
    exclusive (a hard error otherwise, i != 1). This app doesn't replicate
    that validation -- do_cryocare_train takes precedence if both are
    somehow true (matching the C++'s own check order), and [] (no
    subcommand at all) if neither is set yet, the ordinary
    not-yet-configured state. output_subdir is unused here -- this job
    gets its --output-directory the ordinary automatic way, unlike the
    exe-placeholder jobs program_extra's second parameter exists for."""
    if field_values.get("do_cryocare_train"):
        return ["cryoCARE:train"]
    if field_values.get("do_cryocare_predict"):
        return ["cryoCARE:predict"]
    return []


def _tomo_denoise_ntiles_flags(field_values: dict, output_subdir: str = "") -> list:
    """TomoDenoiseTomograms's --n-tiles (getCommandsTomoDenoiseTomogramsJob
    ~6900-6903, confirmed current): ntiles_x/y/z are three SEPARATE
    JobOptions combined into ONE --n-tiles flag with three space-separated
    values -- not expressible as three independent FlagOverride entries
    (each would emit its own repeated "--n-tiles <value>", not the single
    "--n-tiles <x> <y> <z>" relion_python_tomo_denoise expects). Gated on
    do_cryocare_predict and all three fields non-empty (mirrors the real
    condition's four-clause && -- the three .length() > 0 checks are just
    the ordinary non-empty check, done directly here), AND
    !do_cryocare_train -- same reasoning as denoising_tomo_name/
    care_denoising_model above: do_cryocare_train/do_cryocare_predict are
    independent checkboxes real RELION only guards with a hard error
    (~6821-6825), reachable-but-invalid in this app's UI, and
    _tomo_denoise_subcommand_tokens already gives do_cryocare_train
    precedence for the subcommand token, so --n-tiles (a predict-only
    flag) must not ride along under a "cryoCARE:train" subcommand."""
    if not field_values.get("do_cryocare_predict") or field_values.get("do_cryocare_train"):
        return []
    x, y, z = (field_values.get(k) for k in ("ntiles_x", "ntiles_y", "ntiles_z"))
    if x in (None, "") or y in (None, "") or z in (None, ""):
        return []
    # ntiles_x/y/z are plain user-editable text fields (not sliders), so
    # their raw string values are quoted before going on the command line
    # -- same as every other free-text field this job's ordinary
    # per-option loop emits (job_registry._build_draft_command's
    # shlex.quote(str(value)) call); extra_flags output isn't quoted by
    # its caller, so this builder is responsible for its own quoting.
    return ["--n-tiles", shlex.quote(str(x)), shlex.quote(str(y)), shlex.quote(str(z))]


_CTF_FIT_LETTER = {"No": "f", "Per-micrograph": "m", "Per-particle": "p"}
# JobOption::getCtfFitString (src/pipeline_jobs.cpp ~243-249): the exact
# label->single-character mapping RELION's own getCommandsCtfrefineJob
# uses to build --fit_mode. An unrecognized label (a future RELION version
# renaming a choice) resolves to None below, not a wrong guess.


def _ctfrefine_kmin_and_fit_mode_flags(field_values: dict, output_subdir: str = "") -> list:
    """Ctfrefine's do_aniso_mag/do_ctf/do_tilt branch (getCommandsCtfrefineJob
    ~6116-6151, confirmed current): each toggle's own self-contained flag
    is a plain FlagOverride (see the Ctfrefine DRAFT_OVERRIDES entry
    below) -- this covers the companion VALUE each one unconditionally
    appends, all three built from the SAME "minres" field under a
    different flag name, plus do_ctf's --fit_mode (four radio fields
    concatenated via JobOption::getCtfFitString, in the exact order
    phase/defocus/astig/"f" (Cs, fixed off)/bfactor -- getting this order
    wrong silently produces a wrong-but-plausible 5-character mode
    string). do_ctf and do_tilt are independent siblings inside the
    !do_aniso_mag else-branch (NOT else-if) -- both can fire at once, each
    appending its own kmin_* value. minres/do_phase/do_defocus/do_astig/
    do_bfactor are suppressed (see the DRAFT_OVERRIDES entry) -- fully
    expressed here instead, since none of them has one single owning
    flag. minres is parsed with _safe_float rather than passed through
    raw: extra_flags' output is appended to the draft command unquoted
    (job_registry._build_draft_command extends parts directly, unlike the
    shlex.quote'd generic per-option path), so a raw pass-through of an
    un-vetted field value could smuggle extra whitespace-separated tokens
    into the command; a clean float formats back to a plain numeral with
    no such risk."""
    minres = _safe_float(field_values.get("minres"), None)
    has_minres = minres is not None
    out: list = []
    if field_values.get("do_aniso_mag"):
        if has_minres:
            out += ["--kmin_mag", str(minres)]
        return out
    if field_values.get("do_ctf"):
        if has_minres:
            out += ["--kmin_defocus", str(minres)]
        letters = [
            _CTF_FIT_LETTER.get(field_values.get("do_phase")),
            _CTF_FIT_LETTER.get(field_values.get("do_defocus")),
            _CTF_FIT_LETTER.get(field_values.get("do_astig")),
            "f",
            _CTF_FIT_LETTER.get(field_values.get("do_bfactor")),
        ]
        if all(letter is not None for letter in letters):
            out += ["--fit_mode", "".join(letters)]
    if field_values.get("do_tilt") and has_minres:
        out += ["--kmin_tilt", str(minres)]
    return out


# ModelAngelo (issue #37; getCommandsModelAngeloJob/initialiseModelAngeloJob,
# src/pipeline_jobs.cpp ~5688-5800, RELION 5.0.1, re-confirmed against a
# fresh read of the same function -- full source transcribed below since
# it's short enough to check every line against, unlike this module's usual
# "cite line numbers" convention:
#
#   command = fn_modelangelo_exe
#   if p_seq or d_seq or r_seq: command += " build "; append -pf/-df/-rf
#     for whichever of the three is set (independently optional)
#   else: command += " build_no_seq "
#   command += " -v " + fn_map
#   command += " -o " + outputname
#   command += " -d " + gpu_id            # unconditional, even if empty
#   command += " " + other_args
#   commands.push_back(command)
#
#   if do_hhmer:
#     if fn_lib == "": error, refuse to build a command at all
#     command2 = fn_modelangelo_exe + " hmm_search "
#     command2 += " -i " + outputname     # SAME directory the build step used
#     command2 += " -f " + fn_lib
#     command2 += " -o " + outputname     # SAME directory again
#     command2 += " -a " + alphabet
#     command2 += " --F1 .. --F2 .. --F3 .. --E .."
#     command2 += " " + other_args
#     commands.push_back(command2)
#
# Two things this app's usual mechanisms can't express on their own:
#   1. output_subdir is needed for -o/-i, but ModelAngelo is an
#      "exe-placeholder" job (program comes from fn_modelangelo_exe, not a
#      fixed binary) -- _build_draft_command skips its whole automatic
#      --o/subdir/extra_output_args block for exe-placeholder jobs
#      entirely (see its own comment on why), so program_extra/extra_flags
#      had to gain output_subdir as an explicit second parameter (see
#      JobDraftOverride's own comments) just for this job.
#   2. ONE job emits TWO independent commands. Represented here as ordinary
#      shell syntax (`&&`) rather than inventing a "second command" concept
#      in the job runner -- the draft command already runs through
#      asyncio.create_subprocess_shell (job_runner.py), so a literal `&&`
#      Just Works, the same way a user chaining two commands by hand would
#      write it. RELION's own two commands.push_back() calls get joined the
#      same way under the hood (prepareFinalCommand), so this isn't new
#      behavior, just spelling out what RELION already does as visible
#      shell syntax instead of hiding it in two separate command slots.
#
# What's deliberately NOT replicated: real RELION's hard error when
# do_hhmer is checked but fn_lib is empty ("you need to provide a library
# to perform the HMM search against"). This app's policy elsewhere is to
# leave a genuinely-incomplete draft for the user to notice and fix, not to
# validate or block client-side -- consistent with every other job here.
# Also not replicated: other_args being appended to BOTH commands
# separately (RELION appends its own copy to each). This app's generic
# trailing-other_args logic in _build_draft_command runs once, after
# everything else -- so other_args ends up attached to the SECOND command
# only when do_hhmer is on. A user who needs it on the first command too
# can just add it there by hand; not worth a special case for a field that
# defaults to empty.
def _modelangelo_program_extra(field_values: dict, output_subdir: str) -> list:
    """`build`/`build_no_seq` subcommand-select, plus the compulsory -o
    (see this section's own header comment for the source). -v/-pf/-df/
    -rf/-d are ordinary FlagOverride entries in this job's DRAFT_OVERRIDES
    entry below since none of them need output_subdir; this hook exists
    only because -o does. Also appends -m <mask_path> when set: mask_path
    is NOT a real RELION field (see SYNTHETIC_OPTIONS below for why) --
    read directly here since the generic per-option loop was never told
    about it and wouldn't otherwise consider it."""
    if not output_subdir:
        return []
    subdir = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    seq_given = any(field_values.get(k) for k in ("p_seq", "d_seq", "r_seq"))
    tokens = ["build" if seq_given else "build_no_seq", "-o", shlex.quote(subdir)]
    mask = field_values.get("mask_path")
    if mask:
        tokens += ["-m", shlex.quote(str(mask))]
    return tokens


def _modelangelo_hmm_search_command(field_values: dict, output_subdir: str) -> list:
    """The second, independent hmm_search command RELION appends when "Perform
    HMMer search?" is checked -- see this section's own header comment for
    the source and design notes. F1/F2/F3/E are suppressed from the
    primary (build) command's per-option loop (see the DRAFT_OVERRIDES
    entry below) and re-emitted here instead, since they belong to THIS
    command, not the build one -- the generic loop would otherwise still
    attach them to the build command, which is wrong (confirmed: RELION's
    own source only ever adds them to command2)."""
    if not field_values.get("do_hhmer") or not output_subdir:
        return []
    exe = field_values.get("fn_modelangelo_exe")
    fn_lib = field_values.get("fn_lib")
    if not exe or not fn_lib:
        return []
    subdir = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    tokens = [
        "&&", shlex.quote(str(exe)), "hmm_search",
        "-i", shlex.quote(subdir), "-f", shlex.quote(str(fn_lib)),
        "-o", shlex.quote(subdir),
    ]
    alphabet = field_values.get("alphabet")
    if alphabet:
        tokens += ["-a", shlex.quote(str(alphabet))]
    for key, flag in (("F1", "--F1"), ("F2", "--F2"), ("F3", "--F3"), ("E", "--E")):
        value = field_values.get(key)
        if value not in (None, ""):
            tokens += [flag, shlex.quote(str(value))]
    return tokens


# --------------------------------------------------------------------------
# Multi-command jobs (issue #56): real RELION's getCommands*Job() calls
# commands.push_back() more than once for 6 job types (Inimodel, MultiBody,
# ModelAngelo, TomoReconPart, Localres, TomoPickTomograms), and
# prepareFinalCommand (src/pipeline_jobs.cpp ~730-765) joins them with real
# shell " && " into ONE final_command string before ever running anything --
# multi-command execution is not a RELION-US invention, it's what RELION
# ALREADY does under the hood. ModelAngelo (above, issue #37) was the first
# job here to need this, via a literal "&&"-prefixed token list folded into
# extra_flags. The five below follow the exact same mechanism -- see
# extra_flags's own docstring for why a whole extra command can be
# represented as one more entry in a bare token list (its first token is
# just the string "&&" instead of a flag). Localres and TomoPickTomograms
# additionally need commands_before (a command that must run BEFORE the
# primary one), since neither extra_flags nor any other existing hook can
# inject something ahead of the resolved program name -- see
# JobDraftOverride.commands_before's own docstring.
# --------------------------------------------------------------------------


def _inimodel_extra_flags(field_values: dict, output_subdir: str) -> list:
    """`--grad --denovo_3dref` (issue #stress-test, unchanged from before)
    PLUS the second, independent `relion_align_symmetry` command real
    RELION always runs after relion_refine for this job (getCommandsInimodelJob,
    src/pipeline_jobs.cpp ~3555-3587) -- this is the command that actually
    writes initial_model.mrc, the one file every downstream job (e.g.
    Class3D's "Reference map") expects to consume. Confirmed for real:
    without it, a from-scratch InitialModel job reports
    RELION_JOB_EXIT_SUCCESS and runs 100 genuine VDAM iterations, but
    initial_model.mrc is simply never created.

    fn_model (command2's --i) is a value RELION computes from the FIRST
    command's own iteration count, not a plain field passthrough:
    `fn_model.compose(outputname + fn_run + "_it", total_nr_iter, "", 3)` --
    fn_run is always "run" here (is_continue is always false in this app,
    same reasoning as everywhere else in this table), so this is
    `<out>run_it<nr_iter, zero-padded to at least 3 digits>_model.star`.

    The --sym argument mirrors real RELION's own (at first glance
    counter-intuitive, but scientifically deliberate) choice exactly: `if
    (do_run_C1 && !(fn_sym=="C1"||"c1")) command2 += "--sym " + sym_name;
    else command2 += "--sym C1";` -- i.e. relion_refine itself always runs
    unbiased in C1 when do_run_C1 is checked (the common case, since ab-
    initio reconstruction is least biased without symmetry), but this
    SECOND command still aligns that C1 result to the user's real target
    symmetry (sym_name) as a final step, UNLESS sym_name is already
    trivially C1/c1 (nothing to align to). When do_run_C1 is unchecked,
    relion_refine already ran directly in sym_name, so command2 just
    reconfirms C1 -- redundant but harmless (RELION's own source does the
    same thing)."""
    tokens = ["--grad", "--denovo_3dref"]
    if not output_subdir:
        return tokens
    subdir = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    try:
        total_nr_iter = int(float(field_values.get("nr_iter") or 0))
    except (TypeError, ValueError):
        total_nr_iter = 0
    if total_nr_iter <= 0:
        return tokens
    fn_model = f"{subdir}run_it{total_nr_iter:03d}_model.star"
    sym_name = str(field_values.get("sym_name") or "C1")
    do_run_c1 = bool(field_values.get("do_run_C1"))
    sym = sym_name if (do_run_c1 and sym_name not in ("C1", "c1")) else "C1"
    tokens += [
        "&&", "`which relion_align_symmetry`",
        "--i", shlex.quote(fn_model),
        "--o", shlex.quote(subdir + "initial_model.mrc"),
        "--sym", shlex.quote(sym),
        "--apply_sym", "--select_largest_class",
    ]
    return tokens


def _multibody_flex_analyse_flags(field_values: dict, output_subdir: str) -> list:
    """The second, independent relion_flex_analyse command real RELION runs
    when "Perform flexibility analysis?" is checked (getCommandsMultiBodyJob,
    src/pipeline_jobs.cpp ~4820-4880) -- do_analyse defaults to checked, so
    this fires for the common case, not just an edge case. fn_run is always
    `<out>run` here (the `fn_run == ""` branch in real source only matters
    for a continuation run search that never applies -- is_continue is
    always false in this app, same reasoning as elsewhere in this table).
    do_select's eigenvalue-selection flags are included for completeness
    (mirrors real RELION exactly) even though do_select itself defaults to
    unchecked."""
    if not field_values.get("do_analyse") or not output_subdir:
        return []
    subdir = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    fn_run = f"{subdir}run"
    fn_bodies = field_values.get("fn_bodies")
    tokens = [
        "&&", "`which relion_flex_analyse`", "--PCA_orient",
        "--model", shlex.quote(fn_run + "_model.star"),
        "--data", shlex.quote(fn_run + "_data.star"),
        "--bodies", shlex.quote(str(fn_bodies or "")),
        "--o", shlex.quote(subdir + "analyse"),
    ]
    try:
        nr_movies = float(field_values.get("nr_movies") or 0)
    except (TypeError, ValueError):
        nr_movies = 0
    if nr_movies > 0:
        tokens += ["--do_maps", "--k", shlex.quote(str(field_values.get("nr_movies")))]
    if field_values.get("do_select"):
        tokens += [
            "--select_eigenvalue", shlex.quote(str(field_values.get("select_eigenval"))),
            "--select_eigenvalue_min", shlex.quote(str(field_values.get("eigenval_min"))),
            "--select_eigenvalue_max", shlex.quote(str(field_values.get("eigenval_max"))),
        ]
    return tokens


def _tomo_recon_part_helix_flags(field_values: dict, output_subdir: str) -> list:
    """Three extra `relion_helix_toolbox --impose` commands
    (getCommandsTomoReconPartJob, src/pipeline_jobs.cpp ~7960-7990), run
    only when "Do helical reconstruction?" is checked (default off) -- one
    each for half1/half2/merged, imposing the requested helical symmetry on
    the reconstructions relion_tomo_reconstruct_particle just wrote."""
    if not field_values.get("do_helix") or not output_subdir:
        return []
    subdir = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    twist = shlex.quote(str(field_values.get("helical_twist")))
    rise = shlex.quote(str(field_values.get("helical_rise")))
    try:
        z_pct = float(field_values.get("helical_z_percentage") or 0) / 100.0
    except (TypeError, ValueError):
        z_pct = 0.0
    diam = shlex.quote(str(field_values.get("helical_tube_outer_diameter")))
    tokens = []
    for name in ("half1", "half2", "merged"):
        tokens += [
            "&&", "`which relion_helix_toolbox`", "--impose",
            "--i", shlex.quote(f"{subdir}{name}.mrc"),
            "--o", shlex.quote(f"{subdir}helical_{name}.mrc"),
            "--twist", twist, "--rise", rise,
            "--z_percentage", str(z_pct),
            "--cyl_outer_diameter", diam,
        ]
    return tokens


def _localres_other_half(filename: str) -> Optional[str]:
    """Python port of FileName::getTheOtherHalf (src/filename.cpp ~456) --
    substitutes "half1"<->"half2" in the BASENAME only (the directory
    portion, if any, is preserved verbatim), matching real RELION's own
    half-map-pairing convention exactly. Returns None when neither
    substring is present (real RELION treats this as a hard error;
    RELION-US's own policy is to leave an incomplete draft rather than
    validate, so the caller just omits the symlink commands in that case)."""
    if "/" in filename:
        directory, _, name = filename.rpartition("/")
    else:
        directory, name = "", filename
    if "half1" in name:
        other = name.replace("half1", "half2")
    elif "half2" in name:
        other = name.replace("half2", "half1")
    else:
        return None
    return f"{directory}/{other}" if directory else other


def _localres_program_override(field_values: dict) -> Optional[str]:
    """do_resmap_locres's program is the user-configured fn_resmap
    executable path (`command = joboptions["fn_resmap"].getString();`,
    getCommandsLocresJob ~5350) -- not a quoted string literal like every
    other job's `command = "..."`, so the extractor's literal-scan can't
    see it; program_guess instead picked up the OTHER (do_relion_locres)
    branch's `` `which relion_postprocess` `` literal, which is already
    correct for THAT branch on its own. Returning None here for
    do_relion_locres (or neither box checked) leaves program_guess and the
    generic nr_mpi>1 -> program_mpi swap untouched, since relion_postprocess
    genuinely does have an MPI form (relion_postprocess_mpi) that swap
    already handles correctly."""
    if field_values.get("do_resmap_locres"):
        exe = field_values.get("fn_resmap")
        return shlex.quote(str(exe)) if exe else None
    return None


def _localres_suppress_output_flag(field_values: dict) -> bool:
    """do_resmap_locres's command never takes a --o flag at all -- ResMap is
    a third-party argparse program with no such option; real RELION's own
    getCommandsLocresJob only ever appends --o inside the OTHER
    (do_relion_locres) branch (~5410: `command += " --o " + outputname +
    "relion";`). Confirmed for real would be a broken draft otherwise: an
    unrecognized --o flag on ResMap's own CLI."""
    return bool(field_values.get("do_resmap_locres"))


def _localres_commands_before(field_values: dict, output_subdir: str) -> list:
    """The two `ln -s` commands real RELION runs before invoking ResMap
    itself (getCommandsLocresJob ~5345-5346): `commands.push_back("ln -s
    ../../" + fn_half1 + " " + outputname + "half1.mrc");` and the same for
    half2/fn_half2 (fn_half2 computed via getTheOtherHalf -- see
    _localres_other_half above). The "../../" prefix is correct as-is, not
    something to adjust for this app: RELION-US already runs every command
    from the PROJECT ROOT exactly like real RELION does (job_runner.py's
    _run_subprocess), and a relative symlink TARGET is resolved relative to
    the symlink's own location (<out>half1.mrc, two directories below the
    project root), not the process's cwd -- so "../../" + a project-root-
    relative fn_half1 lands back at the exact right file regardless."""
    if not field_values.get("do_resmap_locres") or not output_subdir:
        return []
    fn_half1 = str(field_values.get("fn_in") or "")
    if not fn_half1:
        return []
    fn_half2 = _localres_other_half(fn_half1)
    if fn_half2 is None:
        return []
    subdir = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    return [
        f"ln -s ../../{shlex.quote(fn_half1)} {shlex.quote(subdir + 'half1.mrc')}",
        f"ln -s ../../{shlex.quote(fn_half2)} {shlex.quote(subdir + 'half2.mrc')}",
    ]


def _localres_extra_flags(field_values: dict, output_subdir: str) -> list:
    """do_resmap_locres's own flags (getCommandsLocresJob ~5348-5373) --
    all unmapped today: `--maskVol=`/`--vxSize=`/`--pVal=`/`--minRes=`/
    `--maxRes=`/`--stepRes=` use "=" joining (not the generic "--flag
    value" shape any FlagOverride can express) and `--noguiSplit` takes TWO
    positional values under one flag, so this whole branch is handled as
    one composed function instead, same as Extract's bg_radius or
    Autopick's LoG parameters. do_relion_locres's own flags (angpix,
    adhoc_bfac, fn_mtf, fn_mask) are already correctly extracted with a
    do_relion_locres condition (see option_flags in job_definitions_raw.
    json) and need no override here; only its fn_in ("--locres --i", a
    compound literal the extractor couldn't split -- same shape as
    Ctffind's --is_ctffind4/Class2D's --grad) is added, via this job's
    `flags` entry below rather than here."""
    if not field_values.get("do_resmap_locres") or not output_subdir:
        return []
    subdir = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    tokens = []
    fn_mask = field_values.get("fn_mask")
    if fn_mask:
        tokens.append(f"--maskVol={shlex.quote(str(fn_mask))}")
    tokens += ["--noguiSplit", shlex.quote(subdir + "half1.mrc"), shlex.quote(subdir + "half2.mrc")]
    for key, flag in (
        ("angpix", "--vxSize"), ("pval", "--pVal"), ("minres", "--minRes"),
        ("maxres", "--maxRes"), ("stepres", "--stepRes"),
    ):
        value = field_values.get(key)
        if value not in (None, ""):
            tokens.append(f"{flag}={shlex.quote(str(value))}")
    return tokens


def _tomopick_program_extra(field_values: dict, output_subdir: str) -> list:
    """pick_mode's value is a positional token right after the program name
    on relion_python_tomo_pick's own CLI (getCommandsTomoPickTomogramsJob,
    src/pipeline_jobs.cpp ~7660: `command += " " + pick_mode;`) -- same
    "positional subcommand-style token" shape as ModelAngelo's build/
    build_no_seq or TomoDenoiseTomograms's cryoCARE:train/predict, both
    handled the same way via program_extra elsewhere in this table."""
    pick_mode = field_values.get("pick_mode")
    return [pick_mode] if pick_mode else []


def _tomopick_commands_before(field_values: dict, output_subdir: str) -> list:
    """The optional particles-from-star pre-pass real RELION runs first
    when picking mode "particles" is combined with an input STAR file
    (getCommandsTomoPickTomogramsJob ~7635-7648) -- converts an existing
    particles.star into this job's own annotation format before the
    interactive/auto picker itself runs."""
    if field_values.get("pick_mode") != "particles" or not output_subdir:
        return []
    in_star_file = field_values.get("in_star_file")
    if not in_star_file:
        return []
    in_tomoset = field_values.get("in_tomoset")
    if not in_tomoset:
        return []
    subdir = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    return [
        "`which relion_python_tomo_get_particle_poses` particles-from-star"
        f" --tomograms-file {shlex.quote(str(in_tomoset))}"
        f" --annotations-directory {shlex.quote(subdir + 'annotations')}"
        f" --in-star-file {shlex.quote(str(in_star_file))}"
    ]


def _tomopick_extra_flags(field_values: dict, output_subdir: str) -> list:
    """The second, UNCONDITIONAL relion_python_tomo_get_particle_poses
    command real RELION always runs after the picker itself
    (getCommandsTomoPickTomogramsJob ~7665-7679) -- converts whatever the
    picker just wrote into this job's own particles.star output. --spacing-
    angstroms is added for every pick_mode except "particles" (source:
    `if (!fnt.contains("particles")) command2 += " --spacing-angstroms "
    + particle_spacing;`)."""
    pick_mode = field_values.get("pick_mode")
    in_tomoset = field_values.get("in_tomoset")
    if not pick_mode or not in_tomoset or not output_subdir:
        return []
    subdir = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    tokens = [
        "&&", "`which relion_python_tomo_get_particle_poses`", str(pick_mode),
        "--tilt-series-star-file", shlex.quote(str(in_tomoset)),
        "--annotations-directory", shlex.quote(subdir + "annotations"),
        "--output-directory", shlex.quote(subdir),
    ]
    if pick_mode != "particles":
        tokens += ["--spacing-angstroms", shlex.quote(str(field_values.get("particle_spacing")))]
    return tokens


def _motioncorr_grouping_for_ps_flags(field_values: dict, output_subdir: str) -> list:
    """`--grouping_for_ps <N>`, the ONLY real effect of "Save sum of power
    spectra?" (getCommandsMotioncorrJob ~1650-1666): `float dose_for_ps =
    group_for_ps.getNumber(); float dose_rate = 1.0; if (!is_tomo) dose_rate
    = dose_per_frame.getNumber(); ... int grouping_for_ps = ROUND(dose_for_ps
    / dose_rate); if (grouping_for_ps == 0) grouping_for_ps = 1; command +=
    " --grouping_for_ps " + str(grouping_for_ps);` -- do_save_ps itself never
    becomes a flag. In TOMO mode specifically, dose_rate stays the hardcoded
    literal 1.0 (dose_per_frame is never read at all -- real RELION's own
    per-tilt dose lives in the tilt-series star file by that point, not this
    JobOption), confirmed by the `if (!is_tomo)` guard around the only line
    that would otherwise read it."""
    if not field_values.get("do_save_ps"):
        return []
    try:
        dose_for_ps = float(field_values.get("group_for_ps"))
    except (TypeError, ValueError):
        return []
    if dose_for_ps <= 0:
        return []
    if field_values.get("is_tomo"):
        dose_rate = 1.0
    else:
        try:
            dose_rate = float(field_values.get("dose_per_frame"))
        except (TypeError, ValueError):
            dose_rate = 0.0
    if dose_rate <= 0:
        return []
    grouping = int(dose_for_ps / dose_rate + 0.5)  # RELION's own ROUND macro
    if grouping == 0:
        grouping = 1
    return ["--grouping_for_ps", str(grouping)]


DRAFT_OVERRIDES: dict[str, JobDraftOverride] = {
    # getCommandsTomoImportJob, DEFAULT branch (do_coords == false):
    #   command = "relion_python_tomo_import SerialEM ..."
    # (src/pipeline_jobs.cpp ~lines 6490-6520). program: the extractor
    # picked the do_coords==true coordinate-importer instead (the first
    # `command = "..."` literal it saw), even though do_coords defaults to
    # false and the real default program is the SerialEM tilt-series
    # importer. flags: RELION-5's newer Python tomo tools use hyphenated
    # multi-word flags (`--tilt-image-movie-pattern`) that share no
    # spelling with their snake_case option key (`movie_files`), so the
    # generic `--<key>` rule silently drops every one of them. suppress:
    # the do_coords==true branch's own options (which happen to share flag
    # spellings RELION reuses, e.g. --scale_factor / --add_factor) would
    # otherwise leak into the default (tilt-series) draft via the generic
    # rule; selecting "Import coordinates instead?" in the GUI switches to
    # that branch, and the command box is editable for that case.
    "TomoImport": JobDraftOverride(
        program="relion_python_tomo_import SerialEM",
        output_flag="--output-directory",
        flags={
            "movie_files": FlagOverride("--tilt-image-movie-pattern"),
            "mdoc_files": FlagOverride("--mdoc-file-pattern"),
            "tilt_axis_angle": FlagOverride("--nominal-tilt-axis-angle"),
            "angpix": FlagOverride("--nominal-pixel-size"),
            "kV": FlagOverride("--voltage"),
            "Cs": FlagOverride("--spherical-aberration"),
            "Q0": FlagOverride("--amplitude-contrast"),
            "optics_group_name": FlagOverride("--optics-group-name"),
            # `if (dose_is_per_movie_frame) command += " --dose-per-movie-
            # frame " + dose_rate; else command += " --dose-per-tilt-image "
            # + dose_rate;` (~6506) -- the SAME dose_rate value goes out
            # under one of two flag names depending on the sibling checkbox.
            # Previously hardcoded to always emit --dose-per-tilt-image
            # regardless of dose_is_per_movie_frame -- confirmed as a real
            # bug (checking the box had no effect on the generated command)
            # while auditing this for
            # https://github.com/schiracha/RELION-US/issues/16.
            "dose_rate": FlagOverride(
                "--dose-per-tilt-image", condition="!dose_is_per_movie_frame",
                flag_if_condition_false="--dose-per-movie-frame",
            ),
            "prefix": FlagOverride("--prefix"),
            "mtf_file": FlagOverride("--mtf-file"),
            "flip_tiltseries_hand": FlagOverride("--invert-defocus-handedness"),
            "images_are_motion_corrected": FlagOverride("--images-are-motion-corrected"),
        },
        # dose_is_per_movie_frame and do_coords are both pure gates with no
        # CLI flag of their own (dose_is_per_movie_frame only SELECTS which
        # of two flag names dose_rate's own value goes out under, above;
        # do_coords only selects which of two entirely different program
        # branches this job runs -- see this table's own header comment on
        # why the do_coords==true branch stays unmapped) -- same shape as
        # do_helix/do_local_ang_searches elsewhere in this table. Found
        # running a fresh Import (Tomo) job for real against this dataset's
        # own defaults (dose_is_per_movie_frame=False, do_coords=False):
        # both showed up in unmapped_fields even though the generated
        # command was already fully correct, wrongly implying something
        # here still needed fixing.
        suppress=frozenset({
            "in_coords", "remove_substring", "remove_substring2",
            "is_center", "scale_factor", "add_factor",
            "dose_is_per_movie_frame", "do_coords",
        }),
    ),
    # getCommandsTomoExcludeTiltImagesJob (src/pipeline_jobs.cpp ~7017-7040):
    #   `which relion_python_tomo_exclude_tilt_images`
    #     --tilt-series-star-file <in_tiltseries> --cache-size <cache_size>
    #     --output-directory <out>
    "TomoExcludeTiltImages": JobDraftOverride(
        output_flag="--output-directory",
        flags={
            "in_tiltseries": FlagOverride("--tilt-series-star-file"),
            "cache_size": FlagOverride("--cache-size"),
        },
    ),
    # Verified per-job against getCommands*Job() in src/pipeline_jobs.cpp
    # (RELION cloned 2026-08-14): both take --output-directory, not the
    # default --o. (NB: TomoAlignTiltSeries and TomoReconstructTomograms
    # were also checked and use plain --o, so they're deliberately NOT
    # listed here.)
    #
    # getCommandsTomoPickTomogramsJob (~7615-7690) previously stayed fully
    # unmapped beyond --output-directory ("a genuinely branched job... left
    # out of flags/value_transforms entirely rather than risk a subtly-
    # wrong reconstruction" -- see this table's own header comment) --
    # re-read in full for issue #56 (multi-command support) and it turns
    # out not to be that deeply branched: ONE primary command (always
    # runs), an OPTIONAL command before it (pick_mode=="particles" AND an
    # input STAR file given), and an UNCONDITIONAL command after it. See
    # program_extra/commands_before/extra_flags's own docstrings above.
    "TomoPickTomograms": JobDraftOverride(
        output_flag="--output-directory",
        program_extra=_tomopick_program_extra,
        commands_before=_tomopick_commands_before,
        extra_flags=_tomopick_extra_flags,
        flags={
            "in_tomoset": FlagOverride("--tilt-series-star-file"),
        },
        # pick_mode is consumed by program_extra (it's a positional token,
        # not a --flag); in_star_file is consumed by commands_before.
        suppress=frozenset({"pick_mode", "in_star_file"}),
    ),
    "TomoDenoiseTomograms": JobDraftOverride(
        output_flag="--output-directory",
        program_extra=_tomo_denoise_subcommand_tokens,
        flags={
            # denoising_tomo_name/care_denoising_model's conditions are
            # each JUST the mode-select boolean -- the extracted
            # option_flags condition ALSO has a `.getString().length() >
            # 0` clause on the field's OWN value, but that's exactly what
            # the ordinary empty-value skip already does for every
            # non-boolean field, so it needs no extra grammar here (issue
            # #24). tomograms_for_training/number_training_subvolumes/
            # subvolume_dimensions are NOT that shape, though (verified
            # against getCommandsTomoDenoiseTomogramsJob ~6886-6891):
            # RELION guards all THREE of those flags with the SAME single
            # condition, `tomograms_for_training.length()>0 &&
            # do_cryocare_train` -- i.e. number_training_subvolumes/
            # subvolume_dimensions are gated on tomograms_for_training's
            # OWN non-emptiness, not their own. A bare do_cryocare_train
            # condition on those two would wrongly still emit
            # --number-training-subvolumes/--subvolume-sidelength while
            # --training-tomograms itself is correctly suppressed (e.g.
            # do_cryocare_train checked but tomograms_for_training still
            # empty). tomograms_for_training is a real option of this job
            # (in known_keys), so a second bare-identifier clause is
            # enough -- no new condition grammar needed.
            "tomograms_for_training": FlagOverride("--training-tomograms", condition="do_cryocare_train"),
            "number_training_subvolumes": FlagOverride(
                "--number-training-subvolumes", condition="tomograms_for_training && do_cryocare_train"),
            "subvolume_dimensions": FlagOverride(
                "--subvolume-sidelength", condition="tomograms_for_training && do_cryocare_train"),
            # Real RELION hard-errors (i != 1, ~6821-6825) before ever
            # reaching command-building if BOTH do_cryocare_train and
            # do_cryocare_predict are checked -- they're two independent
            # JobOption checkboxes (~6791/6797), not a mutually-exclusive
            # radio group, so that dual-checked state is reachable in
            # this app's UI even though real RELION would refuse it.
            # _tomo_denoise_subcommand_tokens already resolves that state
            # by giving do_cryocare_train precedence for the subcommand
            # token; these two conditions add "&& !do_cryocare_train" so
            # the predict-only flags never ride along under the
            # "cryoCARE:train" subcommand that precedence picked -- Click
            # would reject --tomogram-name/--model-file there, since they
            # belong to the OTHER subcommand.
            "denoising_tomo_name": FlagOverride(
                "--tomogram-name", condition="do_cryocare_predict && !do_cryocare_train"),
            "care_denoising_model": FlagOverride(
                "--model-file", condition="do_cryocare_predict && !do_cryocare_train"),
        },
        # do_cryocare_train/predict have no standalone flag of their own
        # (fully expressed via program_extra's subcommand token now).
        # ntiles_x/y/z are combined into extra_flags' single --n-tiles
        # (ntiles_x's own auto-extracted condition is unevaluable -- three
        # .length()>0 clauses on DIFFERENT fields plus a boolean -- so it
        # would otherwise sit in unmapped_fields implying a fix is still
        # needed when there isn't one; same reasoning as Extract's
        # bg_diameter suppression).
        suppress=frozenset({"do_cryocare_train", "do_cryocare_predict", "ntiles_x", "ntiles_y", "ntiles_z"}),
        extra_flags=_tomo_denoise_ntiles_flags,
    ),
    # See the "ModelAngelo" section header comment above
    # _modelangelo_program_extra for the full source this entry is
    # verified against, and the two mechanisms (output_subdir threaded
    # into program_extra/extra_flags, and a literal `&&` for the second
    # command) this job needed that no other job here has needed yet.
    "ModelAngelo": JobDraftOverride(
        program_extra=_modelangelo_program_extra,
        flags={
            "fn_map": FlagOverride("-v"),
            "p_seq": FlagOverride("-pf"),
            "d_seq": FlagOverride("-df"),
            "r_seq": FlagOverride("-rf"),
            # RELION emits -d unconditionally, even with gpu_id left blank
            # (no `!= ""` guard around it, unlike p_seq/d_seq/r_seq above) --
            # this app's ordinary empty-value skip means a blank gpu_id
            # omits the flag entirely here rather than reproducing RELION's
            # own "-d " (empty argument). Not worth a special case (like the
            # existing gpu_ids/--gpu one elsewhere in this module) for a job
            # whose underlying tool already treats a missing --device as
            # "find an available GPU" on its own (confirmed:
            # model_angelo/apps/build.py's own default=None for --device).
            "gpu_id": FlagOverride("-d"),
        },
        # do_hhmer/fn_lib/alphabet/F1/F2/F3/E have no flag of their own on
        # the PRIMARY (build) command -- they're fully expressed by
        # _modelangelo_hmm_search_command instead, which builds the entire
        # second command (see extra_flags below). fn_modelangelo_exe is
        # genuinely handled too, just not as a flag: _build_draft_command's
        # own exe-placeholder resolution (job_registry.py) already consumes
        # it to become the program name itself, at the very front of the
        # command -- suppressed here so it isn't ALSO reported unmapped for
        # a value that's actually right there, just not behind a `--flag`.
        suppress=frozenset({
            "do_hhmer", "fn_lib", "alphabet", "F1", "F2", "F3", "E", "fn_modelangelo_exe",
        }),
        extra_flags=_modelangelo_hmm_search_command,
    ),
    # Tomography's shared "optimisation set OR direct entries" input group
    # (in_optimisation / in_particles / in_tomograms / in_trajectories),
    # built by RelionJob::getTomoInputCommmand() (src/pipeline_jobs.cpp
    # ~6328-6430) rather than inlined in each job's own getCommands*Job() --
    # so the generic rule (which only reads the visible getCommands*Job()
    # body) never sees a flag for any of these four keys and silently drops
    # them all, no matter which one the user fills in. Flags differ by
    # whether the job is a "refine"-style caller (is_for_refine=true) or not:
    #   refine callers   (Inimodel, Class3D, Autorefine; is_for_refine=true):
    #     in_optimisation -> --ios   |  in_particles -> --i
    #     in_tomograms -> --tomograms  |  in_trajectories -> --trajectories
    #   non-refine callers (TomoSubtomo, TomoCtfRefine, TomoAlign,
    #   TomoReconPart; is_for_refine=false):
    #     in_optimisation -> --i    |  in_particles -> --p
    #     in_tomograms -> --t       |  in_trajectories -> --mot
    # Only one of in_optimisation vs. the in_particles/in_tomograms/
    # in_trajectories trio is ever filled in (RELION's own GUI shows them as
    # mutually exclusive, toggled by "OR: use direct entries?" --
    # use_direct_entries, which never itself becomes a flag -- see each
    # entry's `suppress` below); the unused set stays empty and the generic
    # empty-value skip already drops it, so mapping every key unconditionally
    # is safe regardless of which mode the user is in. in_particles sharing
    # "--i" with fn_img (the classic-SPA counterpart of the same job) is
    # likewise safe: a job is filled in as either SPA or tomo, never both.
    # Verified against pipeline_jobs.cpp's call sites (is_for_refine and the
    # has_tomograms/has_particles/has_trajectories/has_manifolds
    # HAS_COMPULSORY/HAS_OPTIONAL/HAS_NOT args) for each job below.
    #
    # "Always do compute stuff" (pipeline_jobs.cpp's own comment, verbatim,
    # above this exact three-line block in every one of Inimodel/Class2D/
    # Class3D/Autorefine/MultiBody's getCommands*Job(), unconditional
    # relative to any other branch):
    #   if (!joboptions["do_combine_thru_disc"].getBoolean())
    #       command += " --dont_combine_weights_via_disc";
    #   if (!joboptions["do_parallel_discio"].getBoolean())
    #       command += " --no_parallel_disc_io";
    #   if (joboptions["do_preread_images"].getBoolean())
    #       command += " --preread_images ";
    # All three are self-guarded (each condition names only its own key) but
    # the generic `--<key>` rule can't find them: none of the three flags
    # spell out as "--" + their key. Confirmed missing from every one of
    # these jobs' option_flags (the append sits on the line AFTER the `if`,
    # not alongside a `joboptions["key"]` reference the extractor's regex
    # can read), so all three sat silently unmapped -- "Use parallel disc
    # I/O?" and "Combine iterations through disc?" (unusually, NEGATED: the
    # flag appears when the box is OFF) never had any effect on the draft
    # regardless of what the user checked, on every classification/
    # refinement job in the app.
    #
    # Same missed-by-the-extractor shape, same four jobs, one more spot:
    #   if (!is_continue) {
    #     if (joboptions["do_ctf_correction"].getBoolean()) {
    #       command += " --ctf ";
    #       if (joboptions["ctf_intact_first_peak"].getBoolean())
    #         command += " --ctf_intact_first_peak ";
    #     }
    #   }
    # (Inimodel ~3428, Class2D ~3149, Class3D ~3828, Autorefine ~4315 in
    # pipeline_jobs.cpp -- identical body in all four). do_ctf_correction is
    # self-guarded but its flag ("--ctf") doesn't spell out as "--" + its
    # key, so the generic rule missed it too; the field defaults to Yes in
    # real RELION, so every draft from these four job types was silently
    # missing --ctf regardless of what the user had checked.
    # ctf_intact_first_peak needs BOTH conditions (its own checkbox AND
    # do_ctf_correction) -- FlagOverride.condition supplies the outer one,
    # the normal boolean-field emit logic supplies the inner one.
    "Inimodel": JobDraftOverride(
        output_suffix="run",
        # `if (!is_continue) { command += " --grad --denovo_3dref "; ... }`
        # (~3475-3478) -- unconditional in this app's context (RELION-US
        # never models a "continue" run, so !is_continue is always true
        # here, same reasoning as every other !is_continue-gated field in
        # this table), and a bare literal with no owning JobOption, so the
        # generic per-option extractor can't find it -- same shape as
        # Class2D's --grad/Ctffind's --is_ctffind4/Autopick's --LoG.
        # --denovo_3dref specifically is NOT cosmetic: confirmed for real,
        # a from-scratch Inimodel draft missing it ran relion_refine to
        # completion (RELION_JOB_EXIT_SUCCESS written, 100 real iterations)
        # but with _rlnReferenceDimensionality/_rlnDataDimensionality both
        # 2 in the resulting model.star -- relion_refine silently defaulted
        # to 2D classification instead of 3D ab-initio reconstruction, the
        # one thing this job type exists to do; every run_itNNN_classes.mrcs
        # it wrote was a flat 64x64 image, not a 64^3 volume.
        #
        # extra_flags ALSO carries the second relion_align_symmetry command
        # real RELION runs after this one (issue #56) -- see
        # _inimodel_extra_flags's own docstring above for the full source
        # citation and why it belongs here rather than a new hook.
        extra_flags=_inimodel_extra_flags,
        flags={
            "in_optimisation": FlagOverride("--ios"), "in_particles": FlagOverride("--i"),
            "in_tomograms": FlagOverride("--tomograms"), "in_trajectories": FlagOverride("--trajectories"),
            "fn_img": FlagOverride("--i"),
            "do_parallel_discio": FlagOverride("--no_parallel_disc_io", negated=True),
            "do_combine_thru_disc": FlagOverride("--dont_combine_weights_via_disc", negated=True),
            "do_preread_images": FlagOverride("--preread_images"),
            "do_ctf_correction": FlagOverride("--ctf"),
            "ctf_intact_first_peak": FlagOverride("--ctf_intact_first_peak", condition="do_ctf_correction"),
            # `if (do_run_C1) command += " --sym C1 "; else command += " --sym
            # " + fn_sym;` -- always emits one or the other (~3520-3527).
            # Mapping only the checked branch is safe: unchecked already
            # falls through to sym_name's own (separately handled) value.
            "do_run_C1": FlagOverride("--sym C1"),
            # `if (do_solvent) command += " --flatten_solvent ";` (~3529).
            "do_solvent": FlagOverride("--flatten_solvent"),
        },
        suppress=frozenset({"use_direct_entries", "use_gpu"}),
    ),
    "Class3D": JobDraftOverride(
        output_suffix="run",
        flags={
            "in_optimisation": FlagOverride("--ios"), "in_particles": FlagOverride("--i"),
            "in_tomograms": FlagOverride("--tomograms"), "in_trajectories": FlagOverride("--trajectories"),
            "fn_img": FlagOverride("--i"), "fn_ref": FlagOverride("--ref"),
            "do_parallel_discio": FlagOverride("--no_parallel_disc_io", negated=True),
            "do_combine_thru_disc": FlagOverride("--dont_combine_weights_via_disc", negated=True),
            "do_preread_images": FlagOverride("--preread_images"),
            "do_ctf_correction": FlagOverride("--ctf"),
            "ctf_intact_first_peak": FlagOverride("--ctf_intact_first_peak", condition="do_ctf_correction"),
            # `if (!ref_correct_greyscale) command += " --firstiter_cc";`
            # (~3916) -- self-guarded but negated, missed by the generic rule
            # for the same reason as do_parallel_discio/do_combine_thru_disc
            # above (name doesn't spell "--" + key either way).
            "ref_correct_greyscale": FlagOverride("--firstiter_cc", negated=True),
            "do_fast_subsets": FlagOverride("--fast_subsets"),  # ~3961
            "do_zero_mask": FlagOverride("--zero_mask"),  # ~3967, unconditional (is_continue always false here)
            "do_blush": FlagOverride("--blush"),  # ~3974
            # `if (!dont_skip_align) command += " --skip_align ";` (~3985) --
            # negated: checking "Perform image alignment?" (Yes by default)
            # is what SKIPS emitting --skip_align.
            "dont_skip_align": FlagOverride("--skip_align", negated=True),
            # `if (dont_skip_align) { ... if (allow_coarser) command += "
            # --allow_coarser_sampling"; }` (~4018) -- only reachable at all
            # when alignment is on.
            "allow_coarser": FlagOverride("--allow_coarser_sampling", condition="dont_skip_align"),
            "do_pad1": FlagOverride("--pad 1"),  # ~3939, "--pad 2" is relion_refine's own default when omitted
            # `if (dont_skip_align) { ... if (do_local_ang_searches)
            # command += " --sigma_ang " + floatToString(sigma_angles /
            # 3.); ... }` (~3989-4010) -- nested inside the `else` of `if
            # (!dont_skip_align) { --skip_align } else { ... }`, so BOTH
            # dont_skip_align and do_local_ang_searches gate this, not
            # do_local_ang_searches alone; the flag name and the /3.
            # computation both need an override too (issue #21).
            "sigma_angles": FlagOverride("--sigma_ang", condition="dont_skip_align && do_local_ang_searches"),
            # `if (do_helix) { ... if (dont_skip_align &&
            # !do_local_ang_searches) { ... } }` (~4031-4101) -- all four
            # share this one gate, itself nested inside the outer do_helix
            # block; each value is clamped to [0, 90] then /3, except
            # helical_range_distance, which is only emitted when positive
            # (see _third_if_positive).
            "range_tilt": FlagOverride(
                "--sigma_tilt", condition="do_helix && dont_skip_align && !do_local_ang_searches"),
            "range_psi": FlagOverride(
                "--sigma_psi", condition="do_helix && dont_skip_align && !do_local_ang_searches"),
            "range_rot": FlagOverride(
                "--sigma_rot", condition="do_helix && dont_skip_align && !do_local_ang_searches"),
            "helical_range_distance": FlagOverride(
                "--helical_sigma_distance",
                condition="do_helix && dont_skip_align && !do_local_ang_searches"),
            # `if (keep_tilt_prior_fixed) command += "
            # --helical_keep_tilt_prior_fixed";` (~4075-4076) -- plain
            # self-guarded boolean, only ever reached inside do_helix's
            # branch (is_continue is always false in this app).
            "keep_tilt_prior_fixed": FlagOverride("--helical_keep_tilt_prior_fixed", condition="do_helix"),
        },
        numeric_transforms={
            "sigma_angles": _div_by_3,
            "range_tilt": _clamp_0_90_then_third,
            "range_psi": _clamp_0_90_then_third,
            "range_rot": _clamp_0_90_then_third,
            "helical_range_distance": _third_if_positive,
        },
        suppress=frozenset({
            "use_direct_entries", "use_gpu",
            "do_helix", "do_apply_helical_symmetry", "do_local_search_helical_symmetry",
            # do_local_ang_searches is a pure gate for sigma_angles above --
            # like do_helix, it has no CLI flag of its own in real RELION,
            # so it's suppressed rather than left to show up in
            # unmapped_fields implying a fix is needed when there isn't one.
            "do_local_ang_searches",
        }),
    ),
    "Autorefine": JobDraftOverride(
        output_suffix="run",
        flags={
            "in_optimisation": FlagOverride("--ios"), "in_particles": FlagOverride("--i"),
            "in_tomograms": FlagOverride("--tomograms"), "in_trajectories": FlagOverride("--trajectories"),
            "fn_img": FlagOverride("--i"), "fn_ref": FlagOverride("--ref"),
            "do_parallel_discio": FlagOverride("--no_parallel_disc_io", negated=True),
            "do_combine_thru_disc": FlagOverride("--dont_combine_weights_via_disc", negated=True),
            "do_preread_images": FlagOverride("--preread_images"),
            "do_ctf_correction": FlagOverride("--ctf"),
            "ctf_intact_first_peak": FlagOverride("--ctf_intact_first_peak", condition="do_ctf_correction"),
            "ref_correct_greyscale": FlagOverride("--firstiter_cc", negated=True),  # ~4406, same shape as Class3D
            "do_zero_mask": FlagOverride("--zero_mask"),  # ~4462
            # `if (fn_mask.length() > 0) { ... if (do_solvent_fsc) command +=
            # " --solvent_correct_fsc "; }` (~4469) -- fn_mask is a plain
            # string field, so its own truthiness (empty vs filled) is the
            # condition; bool(field_values["fn_mask"]) reads the same as
            # RELION's own .length() > 0 check.
            "do_solvent_fsc": FlagOverride("--solvent_correct_fsc", condition="fn_mask"),
            "do_blush": FlagOverride("--blush"),  # ~4421
            "auto_faster": FlagOverride("--auto_ignore_angles --auto_resol_angles"),  # ~4440
            "do_pad1": FlagOverride("--pad 1"),  # ~4436
            # Same shape as Class3D's (issue #21), but Autorefine's
            # range_tilt/range_psi/range_rot are gated by `sampling !=
            # auto_local_sampling` -- a numeric comparison between two
            # SELECT fields via a healpix-order lookup table, not a
            # boolean -- so those three deliberately stay unmapped (see
            # the "Deliberately NOT included" footer below); only
            # helical_range_distance and keep_tilt_prior_fixed have a
            # plain do_helix boolean gate here (~4580-4586).
            "helical_range_distance": FlagOverride("--helical_sigma_distance", condition="do_helix"),
            "keep_tilt_prior_fixed": FlagOverride("--helical_keep_tilt_prior_fixed", condition="do_helix"),
        },
        numeric_transforms={
            "helical_range_distance": _third_if_positive,
        },
        # `if (!is_continue) { command += " --auto_refine --split_random_
        # halves"; ... }` (~4357-4359) -- unconditional in this app's
        # context (same !is_continue reasoning as elsewhere in this table)
        # and a bare literal with no owning JobOption. Not cosmetic: this
        # is what makes "3D Auto-refine" actually run RELION's gold-
        # standard auto-refinement (independent half-set FSC-driven
        # convergence) instead of a single, non-split, plain refinement --
        # the one thing distinguishing this job type from Class3D with
        # --K 1. Confirmed missing for real while stress-testing the SPA
        # tutorial's Refine3D step.
        extra_flags=lambda field_values, output_subdir: ["--auto_refine", "--split_random_halves"],
        suppress=frozenset({
            "use_direct_entries", "use_gpu",
            "do_helix", "do_apply_helical_symmetry", "do_local_search_helical_symmetry",
        }),
    ),
    "TomoSubtomo": JobDraftOverride(
        flags={
            "in_optimisation": FlagOverride("--i"), "in_particles": FlagOverride("--p"),
            "in_tomograms": FlagOverride("--t"), "in_trajectories": FlagOverride("--mot"),
            "do_float16": FlagOverride("--float16"),  # ~7112
            "do_stack2d": FlagOverride("--stack2d"),  # ~7117
        },
        suppress=frozenset({"use_direct_entries"}),
    ),
    "TomoCtfRefine": JobDraftOverride(
        flags={
            "in_optimisation": FlagOverride("--i"), "in_particles": FlagOverride("--p"),
            "in_tomograms": FlagOverride("--t"), "in_trajectories": FlagOverride("--mot"),
            # `if (do_reg_def) command += " --do_reg_defocus --lambda " +
            # lambda;` (~7243) -- do_reg_def's own flag doesn't spell out as
            # "--" + key, so (like do_ctf_correction elsewhere) it was
            # missed entirely; lambda's flag DOES equal "--" + its key, so
            # the generic rule found and emitted it UNCONDITIONALLY --
            # confirmed for real, a default-settings draft carried --lambda
            # regardless of do_reg_def. Splitting the compound literal here
            # and adding lambda's own condition fixes both at once.
            "do_reg_def": FlagOverride("--do_reg_defocus"),
            "lambda": FlagOverride("--lambda", condition="do_reg_def"),
            "do_frame_scale": FlagOverride("--per_frame_scale"),  # ~7255
            "do_tomo_scale": FlagOverride("--per_tomo_scale"),  # ~7256
        },
        suppress=frozenset({"use_direct_entries", "in_halfmaps"}),
        extra_flags=_tomo_ref1_ref2_flags,
    ),
    "TomoAlign": JobDraftOverride(
        flags={
            "in_optimisation": FlagOverride("--i"), "in_particles": FlagOverride("--p"),
            "in_tomograms": FlagOverride("--t"), "in_trajectories": FlagOverride("--mot"),
            # `bool do_shift_align = joboptions["do_shift_align"]
            # .getBoolean(); ... if (do_shift_align) command += "
            # --shift_only ";` (~7383/7396) -- read into a local copy first,
            # so _self_guarded's joboptions[...] scan never sees it guarding
            # its own flag (same reason do_motion below was missed).
            "do_shift_align": FlagOverride("--shift_only"),
            "do_motion": FlagOverride("--motion"),  # ~7384/7403, same local-copy shape
            "do_sq_exp_ker": FlagOverride("--sq_exp_ker", condition="do_motion"),  # ~7408
        },
        suppress=frozenset({"use_direct_entries", "in_halfmaps"}),
        extra_flags=_tomo_ref1_ref2_flags,
    ),
    "TomoReconPart": JobDraftOverride(
        flags={
            "in_optimisation": FlagOverride("--i"), "in_particles": FlagOverride("--p"),
            "in_tomograms": FlagOverride("--t"), "in_trajectories": FlagOverride("--mot"),
        },
        # do_helix's own primary-command flags (helical_nr_asu/twist/rise)
        # are already correctly auto-mapped via the generic per-option loop
        # (each has its own do_helix-conditioned option_flags entry). The
        # do_helix branch ALSO runs three extra relion_helix_toolbox
        # commands (issue #56, see _tomo_recon_part_helix_flags above) once
        # helical reconstruction is actually on -- helical_tube_outer_
        # diameter/helical_z_percentage belong ONLY to those, so they're
        # suppressed here (now genuinely handled, not still needing a fix)
        # rather than showing up as unmapped.
        suppress=frozenset({
            "use_direct_entries", "do_helix",
            "helical_tube_outer_diameter", "helical_z_percentage",
        }),
        extra_flags=_tomo_recon_part_helix_flags,
    ),
    "TomoReconstructTomograms": JobDraftOverride(
        flags={
            # `if (do_fourier) { command += " --fourier "; ... }` (~6746),
            # not nested inside anything else -- generate_split_tomograms's
            # mutual-exclusivity check with do_fourier is enforced as an
            # ERROR when both are true, not a gate on this emission.
            "do_fourier": FlagOverride("--fourier"),
        },
    ),
    "TomoAlignTiltSeries": JobDraftOverride(
        flags={
            # Three mutually-exclusive alignment methods (~6581-6583,
            # enforced by an i!=1 ERROR check, not by nesting) -- each
            # method's own toggle flag is simple; picking more than one is
            # caught by real RELION itself when the draft is actually run.
            "do_imod_fiducials": FlagOverride("--imod_fiducials"),  # ~6609
            "do_imod_patchtrack": FlagOverride("--imod_patchtrack"),  # ~6615
            "do_aretomo2": FlagOverride("--aretomo2"),  # ~6622
            "do_aretomo_tiltcorrect": FlagOverride("--aretomo_tiltcorrect", condition="do_aretomo2"),  # ~6627
            "do_aretomo_ctf": FlagOverride("--aretomo_ctf", condition="do_aretomo2"),  # ~6633
            "do_aretomo_phaseshift": FlagOverride(
                "--aretomo_phaseshift", condition="do_aretomo2 && do_aretomo_ctf"),  # ~6636
        },
    ),
    "Class2D": JobDraftOverride(
        output_suffix="run",
        # `else if (joboptions["do_grad"].getBoolean()) { ... command += "
        # --grad --class_inactivity_threshold 0.1 --grad_write_iter 10"; ...
        # }` (~3203-3211) -- do_grad/VDAM's own mode flags are a bare
        # unconditional-within-the-branch literal chunk with no owning
        # JobOption, same shape as Ctffind's --is_ctffind4/Autopick's --LoG/
        # Extract's --extract, so the generic per-option extractor can't
        # find them either. do_grad defaults to Yes (VDAM is RELION's own
        # recommended default since relion-4.0), so this is the COMMON
        # case, not an edge one -- confirmed for real: a from-scratch
        # Class2D draft with do_grad on (the default) had --iter 100 (the
        # real nr_iter_grad value, correctly mapped on its own) but no
        # --grad at all, which would have made relion_refine interpret
        # that iter count as plain EM iterations instead of VDAM
        # mini-batches -- a real, wrong-algorithm bug, not just a missing
        # flag. do_em/do_grad are independent checkboxes in this app's own
        # form (not a radio group) -- mirrors RELION's own explicit
        # mutual-exclusivity error ("You cannot specify to use both the EM
        # and the VDAM algorithm!") by only firing when do_grad is on AND
        # do_em is off, same as RELION's own else-if.
        extra_flags=lambda field_values, output_subdir: (
            ["--grad", "--class_inactivity_threshold", "0.1", "--grad_write_iter", "10"]
            if field_values.get("do_grad") and not field_values.get("do_em")
            else []
        ),
        flags={
            "do_parallel_discio": FlagOverride("--no_parallel_disc_io", negated=True),
            "do_combine_thru_disc": FlagOverride("--dont_combine_weights_via_disc", negated=True),
            "do_preread_images": FlagOverride("--preread_images"),
            "do_ctf_correction": FlagOverride("--ctf"),
            "ctf_intact_first_peak": FlagOverride("--ctf_intact_first_peak", condition="do_ctf_correction"),
            "do_zero_mask": FlagOverride("--zero_mask"),  # ~3268, unconditional (is_continue always false here)
            "do_center": FlagOverride("--center_classes"),  # ~3277
            # `if (!dont_skip_align) command += " --skip_align ";` (~3285)
            "dont_skip_align": FlagOverride("--skip_align", negated=True),
            "allow_coarser": FlagOverride("--allow_coarser_sampling", condition="dont_skip_align"),  # ~3301
            "do_bimodal_psi": FlagOverride("--bimodal_psi", condition="do_helix && dont_skip_align"),  # ~3317
        },
        # use_gpu -- gates --gpu (built from gpu_ids' value), never a flag
        # on its own (pipeline_jobs.cpp, all 6 jobs across this table using
        # it share the identical `if (joboptions["use_gpu"].getBoolean())
        # ... command += " --gpu \"" + joboptions["gpu_ids"].getString() +
        # "\"";` shape). do_helix -- gates the helical_* fields here too
        # (Class3D/Autorefine already listed above).
        suppress=frozenset({"use_gpu", "do_helix"}),
    ),
    "MultiBody": JobDraftOverride(
        output_suffix="run",
        flags={
            "do_parallel_discio": FlagOverride("--no_parallel_disc_io", negated=True),
            "do_combine_thru_disc": FlagOverride("--dont_combine_weights_via_disc", negated=True),
            "do_preread_images": FlagOverride("--preread_images"),
            # All three below sit inside `if (!is_continue || (is_continue &&
            # fn_cont != "")) { ... }` (~4713) -- since is_continue is always
            # false in this app, that whole condition is always true here,
            # same reasoning as do_ctf_correction's "!is_continue" elsewhere.
            "do_blush": FlagOverride("--blush"),  # ~4772
            "do_subtracted_bodies": FlagOverride("--reconstruct_subtracted_bodies"),  # ~4775
            "do_pad1": FlagOverride("--pad 1"),  # ~4788
        },
        suppress=frozenset({"use_gpu"}),
        # The second, independent relion_flex_analyse command real RELION
        # runs when "Perform flexibility analysis?" is checked -- see
        # _multibody_flex_analyse_flags's own docstring above (issue #56).
        # do_analyse defaults to checked, so this fires for the tutorial's
        # own default settings, not just an edge case.
        extra_flags=_multibody_flex_analyse_flags,
    ),
    # Motioncorr/Ctffind's `is_tomo`-guarded fields. This table is keyed by
    # BASE name (see TOMO_VARIANT_OF above) -- job_registry._resolve_tomo_
    # variant resolves TomoMotioncorr/TomoCtffind to "Motioncorr"/"Ctffind"
    # before every lookup here, since the override/flag-name facts below are
    # about the real RELION job CLASS, shared by both menu entries; only
    # is_tomo's actual value (which entry the user picked) differs, and
    # that's threaded in separately as field_values["is_tomo"] -- see
    # job_registry._build_draft_command and _evaluate_condition's docstring.
    # do_dose_weighting and do_own_motioncor are additionally
    # mapped because their OWN flag also doesn't spell out as "--" + key, so
    # they need a name override on top of the self-guard:
    #   do_dose_weighting (~1641, condition `!is_tomo && joboptions[
    #   "do_dose_weighting"].getBoolean()`) -- job_registry._self_guarded
    #   only inspects joboptions[...] references, so this condition's bare
    #   `!is_tomo` term is invisible to it and the field is treated as
    #   self-guarded regardless of SPA/Tomo (same as real RELION's GUI never
    #   showing this JobOption at all in tomo mode -- see
    #   initialiseMotioncorrJob's `if (!is_tomo) joboptions["do_dose_
    #   weighting"] = ...`, ~1501 -- RELION-US shows one static field list
    #   either way). Emitting --dose_weighting when checked in Tomo mode is
    #   therefore still possible by leaving the box checked; low-risk since
    #   its own RELION default is enabled and this mirrors the box's own
    #   plain reading, not a change from before the toggle existed.
    #   do_own_motioncor (~1559-1580, found by actually running a
    #   Motioncorr job against RELION 5.0.1 -- it failed with "You have to
    #   choose either UCSF MotionCor2 or RELION's own implementation", the
    #   exact compulsory choice motion correction refuses to run without.
    #   Self-guarded: `if (joboptions["do_own_motioncor"].getBoolean())
    #   ... command += " --use_own ";`. ONLY the checked (default,
    #   RELION's-own-implementation) branch is covered: the unchecked
    #   branch emits a DIFFERENT flag (--use_motioncor2, ~1580) plus
    #   requires fn_motioncor2_exe, a genuine two-way branch a single
    #   FlagOverride can't express (same category as TomoImport's
    #   do_coords branch) -- switching to UCSF MotionCor2 still needs
    #   hand-editing the command box.
    #   do_even_odd_split (~1554, condition `is_tomo && joboptions[
    #   "do_even_odd_split"].getBoolean()`) -- real MotionCor2 denoising
    #   option, tomo-only. Its flag (--even_odd_split) doesn't spell out as
    #   "--" + key either, so it needs the same kind of override as
    #   do_dose_weighting/do_own_motioncor above, here with an explicit
    #   condition since (unlike those two) there's a genuine SPA-vs-Tomo
    #   difference worth enforcing: no reason to ever emit a tomo-only flag
    #   for an SPA job.
    #
    # value_transforms: gain_rot/gain_flip are "radio" fields whose stored
    # value is the human-facing label ("No rotation (0)"), but
    # motioncorr_runner.cpp:105-106 does `textToInteger(parser.getOption(
    # "--gain_rot", ...))` on the PROGRAM side, expecting a bare
    # "0"/"1"/"2"/"3" -- passing the label crashes with "Error in
    # textToInteger" (confirmed for real against RELION 5.0.1).
    "Motioncorr": JobDraftOverride(
        flags={
            "do_dose_weighting": FlagOverride("--dose_weighting"),
            "do_own_motioncor": FlagOverride("--use_own"),
            "do_even_odd_split": FlagOverride("--even_odd_split", condition="is_tomo"),
            # `if (!is_tomo && do_dose_weighting) { command += "
            # --dose_weighting "; if (do_save_noDW) command += "
            # --save_noDW "; } }` (~1642-1647) -- same !is_tomo shape as
            # do_dose_weighting above, plus its own name mismatch.
            "do_save_noDW": FlagOverride("--save_noDW", condition="!is_tomo && do_dose_weighting"),
            # `if (do_float16) { if (!do_save_ps) { error_message = "...";
            # return false; } command += " --float16"; }` (~1660-1670) --
            # self-guarded in real source, but the extractor's per-option
            # scan missed it entirely (option_flags has no entry at all for
            # this key -- likely the nested do_save_ps error-check
            # confusing its pattern match), so it fell through to the
            # generic "--" + key fallback, which doesn't match either
            # ("--do_float16" vs the real "--float16"). Confirmed for real
            # running a from-scratch tomography MotionCorr job with "Write
            # output in float16?" checked (the tutorial's own recommended
            # setting): the draft silently never wrote float16 output at
            # all.
            "do_float16": FlagOverride("--float16"),
        },
        value_transforms={
            "gain_rot": {
                "No rotation (0)": "0", "90 degrees (1)": "1",
                "180 degrees (2)": "2", "270 degrees (3)": "3",
            },
            "gain_flip": {
                "No flipping (0)": "0", "Flip upside down (1)": "1",
                "Flip left to right (2)": "2",
            },
        },
        # do_save_ps has NO flag of its own -- it only gates a COMPUTED
        # value (see _motioncorr_grouping_for_ps_flags below), same shape
        # as do_helix elsewhere in this table, so it's suppressed rather
        # than left to show up in unmapped_fields implying a fix is still
        # needed. Also found running a real MotionCorr(Tomo) job with "Save
        # sum of power spectra?" checked (required together with float16,
        # per the tutorial and RELION's own hard error otherwise): the
        # draft carried --float16 but never --grouping_for_ps, and
        # CTFFIND-4.1 cannot read float16 images without a power-spectrum
        # sum to fall back on.
        suppress=frozenset({"do_save_ps"}),
        extra_flags=_motioncorr_grouping_for_ps_flags,
    ),
    "Ctffind": JobDraftOverride(
        # (~1809-1813, condition `else { if (joboptions["use_noDW"]
        # .getBoolean()) ...}` -- the `else` closes the `if (is_tomo) {...}`
        # above it, i.e. this is the !is_tomo branch. use_noDW is genuinely
        # SPA-only (RELION's own GUI never shows this JobOption in tomo
        # mode -- initialiseCtffindJob's `if (!is_tomo) joboptions[
        # "use_noDW"] = ...`, ~1711), so the override carries that condition
        # explicitly rather than assuming it like do_dose_weighting above:
        # unlike that field, RELION-US's own Ctffind gained a real SPA/Tomo
        # toggle, and there's no reason to ever emit this flag for a tomo
        # job. localsearch_nominal_defocus/exp_factor_dose need no override
        # at all -- their extracted condition is the bare token "is_tomo"
        # (no joboptions[] reference), which _self_guarded correctly treats
        # as a real branch and job_registry._evaluate_condition now resolves
        # against the toggle like any other is_tomo-gated field.
        #   command += " --use_noDW ";
        # slow_search (~1827, `if (!slow_search) command += " --fast_search
        # ";`) -- negated, applies in both SPA and Tomo mode (not nested
        # inside the is_tomo split above).
        #
        # --is_ctffind4 (~1826, `command += " --is_ctffind4 ";`) is a bare
        # unconditional literal with no owning JobOption at all -- same
        # shape as Postprocess's `--o ... postprocess` suffix, so the
        # generic per-option extractor (which only ever sees a flag beside
        # a `joboptions["key"]` reference) can't find it either. NOT
        # cosmetic: ctffind_runner.cpp only adds `--old-school-input-
        # ctffind4` to the executable's own invocation when is_ctffind4 is
        # set, and modern CTFFIND (4.x/5.x) without that flag starts its
        # own interactive prompt instead of reading the parameters
        # RELION writes into each micrograph's *_ctffind3.com script --
        # confirmed for real: every micrograph failed with "there was an
        # error in executing" against RELION 5.0.1 + a real ctffind
        # binary before this fix, immediately fixed after adding it.
        flags={
            "use_noDW": FlagOverride("--use_noDW", condition="!is_tomo"),
            "slow_search": FlagOverride("--fast_search", negated=True),
        },
        extra_flags=lambda field_values, output_subdir: ["--is_ctffind4"],
    ),
    # getCommandsImportJob (src/pipeline_jobs.cpp:1439-1441, found by
    # actually running an Import job against RELION 5.0.1 -- the draft
    # silently dropped the input file field entirely, which is how this was
    # caught). Both do_raw's fn_in_raw and do_other's fn_in_other are read
    # into a shared local `fn_in`, which is appended to `--i` in ONE place
    # AFTER both branches (line 1439) -- neither joboptions["fn_in_raw"] nor
    # ["fn_in_other"] appears anywhere near a `command +=` line, so the
    # extractor's per-option scan never sees a flag for either key. NOT
    # safe to map unconditionally, unlike the tomo shared-input group
    # above: RELION gives fn_in_raw and fn_in_other their own non-empty
    # defaults ("Micrographs/*.tif" and "ref.mrc") regardless of which
    # branch is active, so the generic empty-value skip does NOT drop the
    # inactive one -- confirmed for real, a default-settings draft emitted
    # "--i 'Micrographs/*.tif' ... --i ref.mrc" (two --i flags) before each
    # got its own condition, mirroring RELION's own
    # `if (do_raw) ... else if (do_other) ...`.
    #
    # output_flag/extra_output_args: getCommandsImportJob (~1440-1441)
    # takes a DIFFERENT shape than every other job in this table: a
    # directory (`--odir`) AND a separate compulsory output FILENAME
    # (`--ofile`, via _import_mode_and_ofile_args above), not one bare
    # `--o <dir>/`. `--o` isn't even a recognized argument to the real
    # relion_import binary -- confirmed for real, running the
    # default-settings draft against RELION 5.0.1 failed immediately with
    # "WARNING: Option --o is not a valid RELION argument" plus "ERROR:
    # Argument --odir not found" / "--ofile not found" (both compulsory,
    # per relion_import --help). That same function also supplies
    # --do_movies/--do_micrographs -- also compulsory, also confirmed for
    # real (see its own docstring).
    "Import": JobDraftOverride(
        output_flag="--odir",
        flags={
            "fn_in_raw": FlagOverride("--i", condition="do_raw"),
            "fn_in_other": FlagOverride("--i", condition="do_other"),
        },
        extra_output_args=_import_mode_and_ofile_args,
    ),
    "Autopick": JobDraftOverride(
        # getCommandsAutopickJob uses `--odir` (~2069/2191), not the generic
        # `--o` -- same shape as Import (see its own override for the
        # confirmed-for-real reasoning). Confirmed for real here too: a
        # completed LoG-mode Autopick job's coordinate files and logfile.pdf
        # landed in AutoPick/ (relion_autopick's own working-directory
        # fallback when handed an --o it doesn't recognize) instead of
        # AutoPick/job003/ -- invisible to this app's own Command Center/
        # Outputs tab, which only ever look inside the job's own numbered
        # directory.
        output_flag="--odir",
        flags={
            # fn_input_autopick's `--i` appears TWICE in getCommandsAutopickJob:
            # once inside the `is_continue && continue_manual` branch (~2067,
            # relion_manualpick) and once in the real fresh-job path (~2175,
            # right after the icheck==1/empty-field guards, shared by all
            # three picking modes) -- the extractor's per-option scan takes
            # the FIRST `command +=` beside a given joboptions[] key in the
            # whole function body, so it attributed this flag to the
            # continue-only occurrence's condition ("is_continue &&
            # continue_manual"), which RELION-US's own field_values can never
            # satisfy (this app never models a "continue" run). Confirmed for
            # real: a from-scratch LoG-mode Autopick job's draft omitted --i
            # entirely even with the input STAR file field filled in. Every
            # other field on this job's I/O tab already resolves correctly
            # since fn_input_autopick is the only one duplicated between the
            # two branches.
            "fn_input_autopick": FlagOverride("--i"),
            # All within the `else if (do_refs)` branch (~2295-2379) unless
            # noted -- one of Autopick's three mutually-exclusive picking
            # modes (LoG/references/topaz, enforced by an else-if chain, not
            # by nesting these under do_refs itself in the source, but the
            # effect is the same: none of these apply unless do_refs is on).
            "log_invert": FlagOverride("--Log_invert", condition="do_log"),  # ~2292, LoG mode
            "do_invert_refs": FlagOverride("--invert", condition="do_refs"),  # ~2338
            # `if (do_ctf_autopick) { command += " --ctf "; if (do_ignore_
            # first_ctfpeak_autopick) command += " --ctf_intact_first_peak
            # "; }` (~2341) -- same nested-self-guard shape as
            # do_ctf_correction elsewhere in this table.
            "do_ctf_autopick": FlagOverride("--ctf", condition="do_refs"),
            "do_ignore_first_ctfpeak_autopick": FlagOverride(
                "--ctf_intact_first_peak", condition="do_refs && do_ctf_autopick"),
            # `if (do_pick_helical_segments) { command += " --helix"; if
            # (do_amyloid) command += " --amyloid"; ... }` (~2367) -- only
            # the flags themselves; the --min_distance/--helical_tube_*
            # VALUE fields right after need a computed value (nr_asu *
            # rise) this table can't express and stay unmapped for
            # hand-editing.
            "do_pick_helical_segments": FlagOverride("--helix", condition="do_refs"),
            "do_amyloid": FlagOverride("--amyloid", condition="do_refs && do_pick_helical_segments"),
            # `if (joboptions["do_refs"].getBoolean() || joboptions["do_log"]
            # .getBoolean()) { if (do_write_fom_maps) command += "
            # --write_fom_maps "; if (do_read_fom_maps) command += "
            # --read_fom_maps "; }` (~2398-2410) -- both self-guarded
            # booleans, but nested under a top-level `||` between Autopick's
            # two picking-with-a-reference modes (LoG vs. references), which
            # job_registry._evaluate_condition can now evaluate (see its
            # OR-support, added for exactly this case -- issue #15).
            "do_write_fom_maps": FlagOverride("--write_fom_maps", condition="do_refs || do_log"),
            "do_read_fom_maps": FlagOverride("--read_fom_maps", condition="do_refs || do_log"),
        },
        suppress=frozenset({"use_gpu"}),
        extra_flags=_autopick_log_flags,
    ),
    "Maskcreate": JobDraftOverride(
        output_suffix="mask.mrc",
        # do_helix -- gates the helical_* fields here too (Class3D/
        # Autorefine already listed above).
        suppress=frozenset({"do_helix"}),
    ),
    # Fixed, unconditional literal suffix (verified by reading the function
    # in full -- no branch controls this line): src/pipeline_jobs.cpp ~5340
    # (command += " --o " + outputname + "postprocess";).
    "Postprocess": JobDraftOverride(
        output_suffix="postprocess",
        flags={
            # `FileName fn_half1 = joboptions["fn_in"].getString(); ...
            # command += " --i " + fn_half1;` (~5317/5334) -- read into a
            # local variable first, so the extractor's per-option scan
            # (which looks for `joboptions["key"]` directly beside a
            # `command +=`) never sees it. fn_in is Postprocess's REQUIRED
            # primary input (one of the two half-maps) -- every draft from
            # this job was silently missing its main --i argument.
            "fn_in": FlagOverride("--i"),
            "do_skip_fsc_weighting": FlagOverride("--skip_fsc_weighting"),  # ~5370
        },
    ),
    # getCommandsLocresJob (src/pipeline_jobs.cpp ~5310-5390) is two
    # mutually-exclusive modes with genuinely DIFFERENT programs
    # (do_resmap_locres's is the user-configured fn_resmap executable;
    # do_relion_locres's is relion_postprocess/_mpi) -- previously had NO
    # entry here at all, so every draft fell through to the fully generic
    # rule with a wrong program guess. do_resmap_locres defaults to CHECKED
    # (do_relion_locres defaults unchecked), so this is the common case, not
    # an edge case -- and it's also a multi-command job (issue #56): real
    # RELION symlinks both half-maps into the job directory before running
    # ResMap. See program_override/commands_before/extra_flags's own
    # docstrings above for the full source citations.
    "Localres": JobDraftOverride(
        output_suffix="relion",
        program_override=_localres_program_override,
        suppress_output_flag=_localres_suppress_output_flag,
        commands_before=_localres_commands_before,
        extra_flags=_localres_extra_flags,
        flags={
            # `command += " --locres --i " + fn_in;` (~5382) -- a compound
            # literal combining TWO flags in one assignment (same shape as
            # Ctffind's --is_ctffind4/Class2D's --grad), only reachable in
            # do_relion_locres mode; do_resmap_locres mode doesn't pass
            # fn_in as a CLI flag at all (only via the symlink commands
            # above, using the RAW value directly).
            "fn_in": FlagOverride("--locres --i", condition="do_relion_locres"),
        },
        # fn_resmap is now consumed by program_override; pval/minres/maxres/
        # stepres are now consumed by extra_flags (their real flags --pVal/
        # --minRes/--maxRes/--stepRes don't case-match the generic "--" +
        # key rule, so they were unmapped before too, just not yet fixed).
        suppress=frozenset({"fn_resmap", "pval", "minres", "maxres", "stepres"}),
    ),
    "Extract": JobDraftOverride(
        # getCommandsExtractJob uses `--part_dir` (~2568) -- a THIRD distinct
        # output-flag convention in this table, different from both the
        # generic `--o` every ordinary job gets and Import/Autopick's
        # `--odir`. Confirmed for real: relion_preprocess warned "Option --o
        # is not a valid RELION argument" and (combined with the missing
        # --extract below) refused to run at all.
        output_flag="--part_dir",
        flags={
            # `if (do_reextract) { ... if (do_reset_offsets) command += "
            # --reset_offsets"; else if (do_recenter) { command += "
            # --recenter"; command += " --recenter_x " + ...; ... } }`
            # (~2501-2519) -- only the two booleans' own flags; recenter_x/
            # y/z are separately, already-correctly mapped (their flag
            # equals "--" + their own key).
            "do_reset_offsets": FlagOverride("--reset_offsets", condition="do_reextract"),
            "do_recenter": FlagOverride("--recenter", condition="do_reextract"),
            "do_invert": FlagOverride("--invert_contrast"),  # ~2605
            "do_float16": FlagOverride("--float16"),  # ~2577
            # `if (do_extract_helix) { command += " --helix"; command += "
            # --helical_outer_diameter " + ...; if (helical_bimodal_
            # angular_priors) command += " --helical_bimodal_angular_
            # priors"; if (do_extract_helical_tubes) { command += "
            # --helical_tubes"; if (do_cut_into_segments) { command += "
            # --helical_cut_into_segments"; ... } else command += "
            # --helical_nr_asu 1 --helical_rise 1"; } }` (~2609-2630,
            # issue #18). helical_tube_outer_diameter/helical_nr_asu/
            # helical_rise already have correctly-extracted conditions of
            # their own; the true-branch (do_cut_into_segments) --
            # helical_nr_asu/--helical_rise values are covered by THEIR OWN
            # entries elsewhere in this raw data, not here -- the else
            # branch's hardcoded "1"/"1" literal fallback (not read from any
            # field) is handled by extra_flags below instead, since a
            # FlagOverride can only pass a field's own value through, never
            # substitute a different literal on the opposite branch.
            #
            # helical_bimodal_angular_priors (~2618-2619) had NO
            # option_flags entry at all -- its flag equals "--" + key, so
            # the generic fallback rule was emitting it UNCONDITIONALLY
            # regardless of do_extract_helix, confirmed for real while
            # fixing this issue (a genuine latent bug, same shape as the
            # "72 fields" class documented in job_registry._build_draft_
            # command's own comment).
            "do_extract_helix": FlagOverride("--helix"),
            "helical_bimodal_angular_priors": FlagOverride(
                "--helical_bimodal_angular_priors", condition="do_extract_helix"),
            "do_extract_helical_tubes": FlagOverride("--helical_tubes", condition="do_extract_helix"),
            "do_cut_into_segments": FlagOverride(
                "--helical_cut_into_segments", condition="do_extract_helix && do_extract_helical_tubes"),
            # `if (do_norm) { ... command += " --norm --bg_radius " + ...;
            # ... }` (~2597-2603, issue #17) -- kept as its own plain,
            # unconditional-on-itself flag (rather than folded into
            # extra_flags' --bg_radius computation below) specifically so
            # it still appears even in the rare case that computation
            # bails out (extract_size unreadable mid-edit) -- see
            # _extract_bg_radius_flags's own docstring.
            "do_norm": FlagOverride("--norm"),
        },
        # bg_diameter/do_rescale are pure gates once their real effects are
        # accounted for elsewhere: bg_diameter's contribution is folded
        # into extra_flags' computed --bg_radius (issue #17) below;
        # do_rescale's "--scale" flag is already separately, correctly
        # mapped via its own (differently-named) "rescale" field's
        # option_flags entry, and do_rescale's OTHER effect (scaling
        # bg_radius itself) is also read directly by extra_flags. Neither
        # has anything left to hand-edit, so they're suppressed rather
        # than shown as still needing a fix. do_norm is NOT suppressed --
        # it's a plain FlagOverride above, not folded into extra_flags.
        suppress=frozenset({"bg_diameter", "do_rescale"}),
        extra_flags=_extract_extra_flags,
    ),
    "Select": JobDraftOverride(
        flags={
            # `else if (do_split) { command += " --split "; if (do_random)
            # command += " --random_order "; ... }` (~2861-2870).
            "do_random": FlagOverride("--random_order", condition="do_split"),
            # `FileName fnt = joboptions["fn_model"].getString(); if
            # (fnt.contains("Class2D/")) { ... if (do_recenter) command +=
            # " --recenter "; }` (~2977-2990) -- do_recenter's OWN boolean
            # check happens automatically afterward via the normal
            # boolean-field emit logic (same as every other FlagOverride);
            # this condition only needs to encode the ADDITIONAL fn_model
            # substring check (issue #23).
            "do_recenter": FlagOverride("--recenter", condition='fn_model.contains("Class2D/")'),
        },
    ),
    "Subtract": JobDraftOverride(
        flags={
            # Everything below sits inside `if (do_fliplabel) {
            # ...different command entirely... } else { ...normal
            # subtraction... }` (~5180) -- do_fliplabel itself switches to a
            # wholly different command shape (`--revert <file> --o <dir>`,
            # no --i/--data/etc.) that a flag override can't express, so it
            # stays unmapped for hand-editing; everything else here only
            # applies in the (default) non-revert branch.
            "fn_opt": FlagOverride("--i", condition="!do_fliplabel"),  # ~5212, required primary input
            # `if (do_data) { ... command += " --data " + fn_data; }`
            # (~5223/5229) -- fn_data's own flag ("--data") differs from the
            # generic "--fn_data" rule too, so (unlike Extract's do_recenter
            # above) it needed its own entry alongside its gate.
            "fn_data": FlagOverride("--data", condition="!do_fliplabel && do_data"),
            "do_float16": FlagOverride("--float16", condition="!do_fliplabel"),  # ~5250
            "do_center_mask": FlagOverride("--recenter_on_mask", condition="!do_fliplabel"),  # ~5239
        },
    ),
    "Motionrefine": JobDraftOverride(
        flags={"do_float16": FlagOverride("--float16")},  # ~5902
    ),
    "Ctfrefine": JobDraftOverride(
        flags={
            # `if (do_aniso_mag) { --fit_aniso; --kmin_mag <minres>; } else
            # { if (do_ctf) {...} if (do_tilt) {...} }` (~6116-6151) --
            # each toggle's own self-contained flag; the companion
            # --kmin_*/--fit_mode VALUES built from minres/do_phase/
            # do_defocus/do_astig/do_bfactor need real branch logic none
            # of FlagOverride/value_transforms/numeric_transforms can
            # express alone -- see _ctfrefine_kmin_and_fit_mode_flags
            # above (issue #20).
            "do_aniso_mag": FlagOverride("--fit_aniso"),
            "do_ctf": FlagOverride("--fit_defocus", condition="!do_aniso_mag"),
            "do_tilt": FlagOverride("--fit_beamtilt", condition="!do_aniso_mag"),
            "do_trefoil": FlagOverride("--odd_aberr_max_n 3", condition="do_tilt"),
            "do_4thorder": FlagOverride("--fit_aberr", condition="!do_aniso_mag"),
        },
        suppress=frozenset({"minres", "do_phase", "do_defocus", "do_astig", "do_bfactor"}),
        extra_flags=_ctfrefine_kmin_and_fit_mode_flags,
    ),
}
# Deliberately NOT included above (mode-branched or otherwise not safely
# reducible to a single default override -- left for hand-editing rather
# than risking a wrong guess):
#   - Autopick.ref3d_sampling, Class3D/Autorefine/MultiBody.sampling,
#     Autorefine.auto_local_sampling ("7.5 degrees" -> --healpix_order N,
#     via JobOption::getHealPixOrder's degrees->healpix-order table, MINUS an
#     oversampling factor computed elsewhere in the same function --
#     pipeline_jobs.cpp ~3993-4000, ~4482-4499, ~4754-4763, ~2314-2321) --
#     a real value TRANSFORM, not just a lookup, so a wrong guess risks a
#     subtly incorrect run rather than a loud crash.
#   - Joinstar's output suffix depends on which of fn_part/fn_mic/fn_mov is
#     filled in (src/pipeline_jobs.cpp ~5069/5103/5137).
#   - Localres only appends "relion" in the do_relion_locres branch; the
#     default ResMap branch uses a different program entirely
#     (src/pipeline_jobs.cpp ~5510).
#   - Select's class_ranker branch appends bare `outputname` (no suffix)
#     plus two EXTRA fixed flags, not a suffix change
#     (src/pipeline_jobs.cpp ~2926).
#
# From the broader unmapped-field audit (every field with no option_flags
# entry at all, across every job type, cross-checked against real source --
# see test_job_registry.py's _UNMAPPED_FIELD_FIXES for the ones that WERE
# safe to fix). Left unmapped rather than guessed:
#   - Autopick.do_topaz_filaments/topaz-internal fields, Motionrefine.
#     do_own_params, Subtract.do_fliplabel: each switches to a genuinely
#     different multi-flag/multi-value shape (or, for do_fliplabel, an
#     entirely different command), not a single flag toggle.
#   - Autorefine's do_helix-gated range_rot/range_tilt/range_psi (gated by
#     `sampling != auto_local_sampling`, a numeric comparison between two
#     SELECT fields via a healpix-order lookup table -- not boolean-
#     expressible, unlike Class3D's identically-named fields above, which
#     use a plain boolean gate instead and ARE fixed), and Autopick's
#     do_pick_helical_segments-gated --min_distance (helical_nr_asu *
#     helical_rise, a genuine multiply this table can't express).
#   - Extract.do_norm/bg_diameter/do_extract_helix family: bg_radius is
#     computed (diameter->radius, extract_size- and do_rescale-dependent,
#     ~2584-2600); the helix branch hardcodes "--helical_nr_asu 1
#     --helical_rise 1" in its else (~2629), not read from any field.
#   - TomoImport.dose_is_per_movie_frame: the SAME "dose_rate" field needs a
#     DIFFERENT flag (--dose-per-movie-frame vs --dose-per-tilt-image)
#     depending on this boolean (~6506) -- one option key, two possible
#     flags; FlagOverride only supports one flag per key.


# Option fields this app adds on top of what RELION's own source defines --
# used ONLY for ModelAngelo's mask_path so far. --mask-path/-m is a real,
# documented ModelAngelo CLI option (model_angelo/apps/build.py and
# build_no_seq.py, both accept it identically: "path to mask file",
# default None) that RELION's own GUI never asks for at all -- confirmed
# absent from getCommandsModelAngeloJob's real source (see the "ModelAngelo"
# section above DRAFT_OVERRIDES) and from job_definitions_raw.json's
# extracted option list for this job. This is NOT reconstructed-or-guessed
# RELION behavior the way everything else in this module is -- RELION
# simply doesn't have this field -- so it's kept in its own clearly-labeled
# table rather than folded into DRAFT_OVERRIDES, which is specifically
# "verified against RELION's own source" territory. This is also why
# ModelAngelo is marked "(beta)" in JOB_CATALOG above: it's the one job in
# this app that goes beyond faithfully reproducing RELION itself.
SYNTHETIC_OPTIONS: dict[str, list[dict]] = {
    "ModelAngelo": [{
        "key": "mask_path",
        "label": "Mask (optional; not in RELION's own GUI):",
        "field_type": "inputnode",
        "default": "",
        "help": (
            "Optional mask restricting ModelAngelo to a region of the map -- "
            "passed through to build's/build_no_seq's --mask-path/-m. "
            "ModelAngelo's own CLI has supported this for both subcommands "
            "since before RELION's own GUI wrapped it; RELION's GUI never "
            "asks for it, so this field exists only here (see this app's "
            "\"(beta)\" label on the ModelAngelo job in the sidebar)."
        ),
    }],
}


def synthetic_options(internal_name: str) -> list[dict]:
    """Extra option fields this app adds beyond RELION's own extracted set,
    for a job whose real underlying tool supports something RELION's GUI
    never exposes. [] for every job except the ones in SYNTHETIC_OPTIONS
    above (see its own comment for why ModelAngelo is the one job that has
    any). Returns fresh dicts each call -- callers may freely combine this
    with a job's real options list without risking a shared mutable
    default."""
    return [dict(opt) for opt in SYNTHETIC_OPTIONS.get(internal_name, [])]


def _override(internal_name: str) -> Optional[JobDraftOverride]:
    return DRAFT_OVERRIDES.get(internal_name)


def has_draft_value_transform(internal_name: str, option_key: str) -> bool:
    """True if this option's label needs translating before it can go on the
    command line -- see JobDraftOverride.value_transforms. False means pass
    the field's own value through unchanged, the same as every other field."""
    override = _override(internal_name)
    return bool(override and option_key in override.value_transforms)


def draft_value_for(internal_name: str, option_key: str, raw_value: str) -> Optional[str]:
    """The real CLI value for a translated radio field's current label.
    Returns None if the label isn't one of this field's known choices --
    e.g. a label this table hasn't been updated for after a RELION version
    change -- so the caller treats the field as unmapped rather than emit a
    value that would crash the program. Only call this after
    has_draft_value_transform confirms this (job, key) is tracked at all."""
    override = _override(internal_name)
    if override is None:
        return None
    return override.value_transforms.get(option_key, {}).get(raw_value)


def has_draft_numeric_transform(internal_name: str, option_key: str) -> bool:
    """True if this option's raw numeric value needs a computed conversion
    (clamp/divide/positivity-gate) before it's what RELION's own program
    actually parses -- see JobDraftOverride.numeric_transforms. Distinct
    from has_draft_value_transform, which is a label->string LOOKUP for
    radio fields; this is a numeric COMPUTATION for slider/number fields."""
    override = _override(internal_name)
    return bool(override and option_key in override.numeric_transforms)


def draft_numeric_value_for(internal_name: str, option_key: str, raw_value: float) -> Optional[float]:
    """The real CLI value for this option's current raw numeric value, or
    None to mean RELION's OWN guard on the COMPUTED value itself (e.g.
    helical_range_distance <= 0) says "correctly omit this flag" -- not
    "can't resolve" (the caller must not mark the field unmapped in that
    case). Only call this after has_draft_numeric_transform confirms this
    (job, key) is tracked at all."""
    override = _override(internal_name)
    if override is None:
        return None
    fn = override.numeric_transforms.get(option_key)
    return fn(raw_value) if fn is not None else None


def draft_flag_for(internal_name: str, option_key: str) -> Optional[str]:
    """Verified CLI flag for this option key, or None to fall back to the
    generic `--<key>` rule. See JobDraftOverride.flags."""
    override = _override(internal_name)
    if override is None:
        return None
    entry = override.flags.get(option_key)
    return entry.flag if entry is not None else None


def draft_flag_condition_for(internal_name: str, option_key: str) -> Optional[str]:
    """The bare-identifier-or-joboptions[...] condition (see
    job_registry._evaluate_condition) that must hold for a flags-mapped
    flag to actually be emitted, or None if it's unconditional (the common
    case). Needed when a single flag name is shared by two mutually
    exclusive option keys that both carry their own non-empty RELION
    default, so the generic empty-value skip can't tell which one is
    active -- see DRAFT_OVERRIDES["Import"]."""
    override = _override(internal_name)
    if override is None:
        return None
    entry = override.flags.get(option_key)
    return entry.condition if entry is not None else None


def draft_flag_is_negated(internal_name: str, option_key: str) -> bool:
    """True if this option's mapped flag fires when the checkbox is
    UNCHECKED rather than checked. See FlagOverride.negated."""
    override = _override(internal_name)
    if override is None:
        return False
    entry = override.flags.get(option_key)
    return bool(entry is not None and entry.negated)


def draft_flag_if_condition_false_for(internal_name: str, option_key: str) -> Optional[str]:
    """The alternate flag to use for this option's OWN value when its
    `condition` evaluates False, instead of omitting the field -- or None if
    there isn't one (the common case: condition false just means "don't
    emit this flag at all"). See FlagOverride.flag_if_condition_false."""
    override = _override(internal_name)
    if override is None:
        return None
    entry = override.flags.get(option_key)
    return entry.flag_if_condition_false if entry is not None else None


def draft_program_override(internal_name: str, field_values: Optional[dict] = None) -> Optional[str]:
    """Verified program string for jobs whose extracted program_guess is
    wrong for the default configuration, else None. Checks
    JobDraftOverride.program_override FIRST (when field_values is given --
    callers that don't need field-dependent resolution, e.g. this module's
    own tests, can omit it and get the old fixed-`program`-only behavior
    unchanged), falling back to the static `program` string. See both
    fields' own docstrings for why a job needs one or the other."""
    override = _override(internal_name)
    if override is None:
        return None
    if field_values is not None and override.program_override is not None:
        resolved = override.program_override(field_values)
        if resolved is not None:
            return resolved
    return override.program


def draft_commands_before(internal_name: str, field_values: dict, output_subdir: str = "") -> list:
    """Complete shell command strings this job needs to run BEFORE the
    primary command this table otherwise builds, or [] for none right now.
    See JobDraftOverride.commands_before."""
    override = _override(internal_name)
    if override is None or override.commands_before is None:
        return []
    return override.commands_before(field_values, output_subdir)


def draft_suppress_output_flag(internal_name: str, field_values: dict) -> bool:
    """True if this job's generic --o/output_subdir insertion must be
    skipped entirely for the CURRENT field values. See
    JobDraftOverride.suppress_output_flag."""
    override = _override(internal_name)
    if override is None or override.suppress_output_flag is None:
        return False
    return bool(override.suppress_output_flag(field_values))


def draft_is_suppressed(internal_name: str, option_key: str) -> bool:
    """True if this option belongs to a non-default branch and should be
    omitted from the default draft entirely. See JobDraftOverride.suppress."""
    override = _override(internal_name)
    return bool(override and option_key in override.suppress)


# RELION runs every job from the PROJECT ROOT and passes the job's output
# directory as a project-root-relative path, e.g. `--o Import/job001/`. Its
# getCommands*Job() functions append this themselves (see e.g. `command +=
# " --o " + outputname;` throughout src/pipeline_jobs.cpp). RELION-US mirrors
# that: the draft command includes the output flag pointing at
# `<JobDir>/jobNNN/`, and the runner executes from the project root (see
# job_runner.start_subprocess_job). Most programs take `--o` -- the default
# every job gets unless its JobDraftOverride sets output_flag.
DRAFT_OUTPUT_FLAG_DEFAULT = "--o"


def draft_output_flag(internal_name: str) -> str:
    """RELION's output-directory flag for this job type (`--o` for most,
    otherwise whatever JobDraftOverride.output_flag says)."""
    override = _override(internal_name)
    if override is not None and override.output_flag:
        return override.output_flag
    return DRAFT_OUTPUT_FLAG_DEFAULT


def draft_output_suffix(internal_name: str) -> Optional[str]:
    """Literal suffix RELION appends to the output directory to form a file
    rootname prefix (e.g. "run" -> `Refine3D/job001/run`), or None when the
    bare directory is correct. See JobDraftOverride.output_suffix."""
    override = _override(internal_name)
    return override.output_suffix if override is not None else None


def draft_program_extra(internal_name: str, field_values: dict, output_subdir: str = "") -> list:
    """Extra command-line tokens inserted immediately after the resolved
    program name, before output_flag/subdir and the per-option loop -- see
    JobDraftOverride.program_extra's own docstring for why position
    matters here. [] if this job has none."""
    override = _override(internal_name)
    if override is None or override.program_extra is None:
        return []
    return override.program_extra(field_values, output_subdir)


def draft_extra_output_args(internal_name: str, field_values: dict) -> list:
    """Extra command-line tokens a job's compulsory-but-computed output
    argument needs, appended right after the output flag/subdir -- e.g.
    Import's --ofile. [] if this job has none. See
    JobDraftOverride.extra_output_args."""
    override = _override(internal_name)
    if override is None or override.extra_output_args is None:
        return []
    return override.extra_output_args(field_values)


def draft_extra_flags(internal_name: str, field_values: dict, output_subdir: str = "") -> list:
    """Extra command-line tokens computed from the full field_values dict,
    appended once per job near the end of the draft command -- for a value
    built from MULTIPLE fields with real conditional/computed logic (e.g.
    Extract's --bg_radius). [] if this job has none. See
    JobDraftOverride.extra_flags."""
    override = _override(internal_name)
    if override is None or override.extra_flags is None:
        return []
    return override.extra_flags(field_values, output_subdir)


# --------------------------------------------------------------------------
# Form-presentation overrides -- separate from DRAFT_OVERRIDES above (which
# only affects the generated command), these change how a field is offered
# in the popup itself. Real RELION renders every entry below as a plain
# Yes/No checkbox too; this isn't correcting real RELION, it's RELION-US
# choosing a clearer widget for a field whose two states are easy to miss on
# an unlabeled checkbox (unlike "Use parallel disc I/O?", where Yes/No reads
# naturally, "Is dose rate per movie frame?" doesn't hint at what the OTHER
# state means without reading the help text). The underlying value is still
# a plain bool sent as field_values[key] -- job_registry/DRAFT_OVERRIDES
# above need no changes to consume it; only frontend/app.js's renderField/
# getFieldValue read this to pick a <select> over a checkbox.
BOOLEAN_SELECT_LABELS: dict[tuple[str, str], tuple[str, str]] = {
    # (internal_name, key): (label when False, label when True)
    ("TomoImport", "dose_is_per_movie_frame"): ("Dose per tilt image", "Dose per movie frame"),
}


def boolean_select_labels(internal_name: str, option_key: str) -> Optional[tuple[str, str]]:
    """(label_for_false, label_for_true) if this boolean field should be
    offered as an explicit two-way dropdown instead of a checkbox, else
    None (the common case -- a plain checkbox)."""
    return BOOLEAN_SELECT_LABELS.get((internal_name, option_key))
