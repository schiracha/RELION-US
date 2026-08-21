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
from typing import Optional

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
    "Ctffind": "CtfFind",
    "Ctfrefine": "CtfRefine",
    "TomoCtfRefine": "CtfRefine",
    "Manualpick": "ManualPick",
    "Autopick": "AutoPick",
    "TomoPickTomograms": "Picks",
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


def internal_name_for_label(type_label: str) -> str | None:
    """Reverse of a job's `label_new`, for reading RELION's own
    `default_pipeline.star` (whose rows carry only the type label).

    RELION appends a sub-label to the base type for many jobs -- `label +=
    ".movies"`, `".em"`, `".topaz"`, 35 places in pipeline_jobs.cpp -- so a
    real project records "relion.class2d.em" where this catalog holds
    "relion.class2d". Longest matching base wins, so "relion.class2d" is not
    mistaken for a prefix of something more specific that also exists.
    """
    label = (type_label or "").strip()
    if not label:
        return None
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
# It breaks for a handful of jobs whose real flag names DIFFER from the
# option keys -- specifically RELION-5's newer Python tomo tools, which use
# hyphenated multi-word flags (`--tilt-image-movie-pattern`) that share no
# spelling with the snake_case option key (`movie_files`). For those, the
# heuristic silently drops every field (all "unmapped") and, for TomoImport,
# even picked the WRONG program (the do_coords=true `relion_tomo_import_
# coordinates` branch instead of the default tilt-series importer).
#
# These two curated overlays fix that for the affected jobs, transcribed
# VERBATIM from the RELION source we read (src/pipeline_jobs.cpp,
# getCommands*Job(), RELION cloned 2026-08-14) and cited per entry. They are
# authoritative: when an option key appears in DRAFT_FLAG_MAP[job], the draft
# builder uses that exact flag and always emits it, bypassing the flags_used
# membership test (which is unreliable for these jobs). This is a small,
# verified data table -- NOT a mechanical reimplementation of RELION's
# per-job command branching (which this project deliberately avoids). Jobs
# with genuinely multi-command / mode-branched builders (TomoPickTomograms,
# TomoDenoiseTomograms) are intentionally left OUT: their default draft
# stays program-name-only with the real source shown for hand-editing,
# rather than risk a subtly-wrong reconstruction of their branching.

# option_key -> real CLI flag, per job. Only keys whose flag name differs
# from `--<key>` need an entry; anything omitted falls through to the
# generic rule.
DRAFT_FLAG_MAP: dict[str, dict[str, str]] = {
    # getCommandsTomoImportJob, DEFAULT branch (do_coords == false):
    #   command = "relion_python_tomo_import SerialEM ..."
    # (src/pipeline_jobs.cpp ~lines 6490-6520). The do_coords==true branch's
    # fields (in_coords, remove_substring[2], is_center, scale_factor,
    # add_factor) are deliberately NOT mapped here -- they belong to the
    # other mode and correctly show as unmapped in the default draft.
    "TomoImport": {
        "movie_files": "--tilt-image-movie-pattern",
        "mdoc_files": "--mdoc-file-pattern",
        "tilt_axis_angle": "--nominal-tilt-axis-angle",
        "angpix": "--nominal-pixel-size",
        "kV": "--voltage",
        "Cs": "--spherical-aberration",
        "Q0": "--amplitude-contrast",
        "optics_group_name": "--optics-group-name",
        # Default is per-tilt-image dose; toggling "Is dose rate per movie
        # frame?" switches RELION to --dose-per-movie-frame (a branch a
        # static map can't express -- noted in the field help).
        "dose_rate": "--dose-per-tilt-image",
        "prefix": "--prefix",
        "mtf_file": "--mtf-file",
        "flip_tiltseries_hand": "--invert-defocus-handedness",
        "images_are_motion_corrected": "--images-are-motion-corrected",
    },
    # getCommandsTomoExcludeTiltImagesJob (src/pipeline_jobs.cpp ~7017-7040):
    #   `which relion_python_tomo_exclude_tilt_images`
    #     --tilt-series-star-file <in_tiltseries> --cache-size <cache_size>
    #     --output-directory <out>
    "TomoExcludeTiltImages": {
        "in_tiltseries": "--tilt-series-star-file",
        "cache_size": "--cache-size",
    },
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
    # use_direct_entries, which never itself becomes a flag -- see
    # DRAFT_SUPPRESS below); the unused set stays empty and the generic
    # empty-value skip already drops it, so mapping every key unconditionally
    # here is safe regardless of which mode the user is in. in_particles
    # sharing "--i" with fn_img (the classic-SPA counterpart of the same
    # job) is likewise safe: a job is filled in as either SPA or tomo, never
    # both. Verified against pipeline_jobs.cpp's call sites (is_for_refine
    # and the has_tomograms/has_particles/has_trajectories/has_manifolds
    # HAS_COMPULSORY/HAS_OPTIONAL/HAS_NOT args) for each job below.
    "Inimodel": {
        "in_optimisation": "--ios", "in_particles": "--i",
        "in_tomograms": "--tomograms", "in_trajectories": "--trajectories",
        "fn_img": "--i",
        "do_parallel_discio": "--no_parallel_disc_io",
        "do_combine_thru_disc": "--dont_combine_weights_via_disc",
        "do_preread_images": "--preread_images",
    },
    "Class3D": {
        "in_optimisation": "--ios", "in_particles": "--i",
        "in_tomograms": "--tomograms", "in_trajectories": "--trajectories",
        "fn_img": "--i", "fn_ref": "--ref",
        "do_parallel_discio": "--no_parallel_disc_io",
        "do_combine_thru_disc": "--dont_combine_weights_via_disc",
        "do_preread_images": "--preread_images",
    },
    "Autorefine": {
        "in_optimisation": "--ios", "in_particles": "--i",
        "in_tomograms": "--tomograms", "in_trajectories": "--trajectories",
        "fn_img": "--i", "fn_ref": "--ref",
        "do_parallel_discio": "--no_parallel_disc_io",
        "do_combine_thru_disc": "--dont_combine_weights_via_disc",
        "do_preread_images": "--preread_images",
    },
    "TomoSubtomo": {
        "in_optimisation": "--i", "in_particles": "--p",
        "in_tomograms": "--t", "in_trajectories": "--mot",
    },
    "TomoCtfRefine": {
        "in_optimisation": "--i", "in_particles": "--p",
        "in_tomograms": "--t", "in_trajectories": "--mot",
    },
    "TomoAlign": {
        "in_optimisation": "--i", "in_particles": "--p",
        "in_tomograms": "--t", "in_trajectories": "--mot",
    },
    "TomoReconPart": {
        "in_optimisation": "--i", "in_particles": "--p",
        "in_tomograms": "--t", "in_trajectories": "--mot",
    },
    # "Always do compute stuff" (pipeline_jobs.cpp's own comment, verbatim,
    # above this exact three-line block in every one of these 5 jobs'
    # getCommands*Job(), unconditional relative to any other branch):
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
    # flag appears when the box is OFF, see DRAFT_NEGATED_FLAGS below) never
    # had any effect on the draft regardless of what the user checked, on
    # every classification/refinement job in the app.
    "Class2D": {
        "do_parallel_discio": "--no_parallel_disc_io",
        "do_combine_thru_disc": "--dont_combine_weights_via_disc",
        "do_preread_images": "--preread_images",
    },
    "MultiBody": {
        "do_parallel_discio": "--no_parallel_disc_io",
        "do_combine_thru_disc": "--dont_combine_weights_via_disc",
        "do_preread_images": "--preread_images",
    },
    # Motioncorr/Ctffind's `is_tomo`-guarded fields (job_registry._evaluate_
    # condition now resolves the `is_tomo`/`!is_tomo` tokens themselves --
    # see that module for why False is the correct constant here). These two
    # booleans are additionally mapped because their OWN flag also doesn't
    # spell out as "--" + key, so they need a name override on top of that:
    #   Motioncorr (pipeline_jobs.cpp ~1641, condition `!is_tomo &&
    #   joboptions["do_dose_weighting"].getBoolean()`, itself self-guarded
    #   once is_tomo is known false):
    #     command += " --dose_weighting ";
    #   Ctffind (~1809-1813, condition `else { if (joboptions["use_noDW"]
    #   .getBoolean()) ...}` -- the `else` is the !is_tomo branch, likewise
    #   self-guarded once is_tomo is known false):
    #     command += " --use_noDW ";
    "Motioncorr": {"do_dose_weighting": "--dose_weighting"},
    "Ctffind": {"use_noDW": "--use_noDW"},
}

# Keys in DRAFT_FLAG_MAP[job] whose mapped flag fires when the checkbox is
# UNCHECKED, not checked -- RELION's own source guards these with a bare
# `if (!joboptions["key"].getBoolean())`, the opposite of the field's own
# display sense ("Use parallel disc I/O?" defaults to Yes; unchecking it is
# what adds `--no_parallel_disc_io`). Every other DRAFT_FLAG_MAP boolean
# fires on checked, matching RELION's overwhelmingly more common convention,
# so this is deliberately an opt-in exception list rather than a per-entry
# field on DRAFT_FLAG_MAP itself.
DRAFT_NEGATED_FLAGS: dict[str, set[str]] = {
    "Class2D": {"do_parallel_discio", "do_combine_thru_disc"},
    "Inimodel": {"do_parallel_discio", "do_combine_thru_disc"},
    "Class3D": {"do_parallel_discio", "do_combine_thru_disc"},
    "Autorefine": {"do_parallel_discio", "do_combine_thru_disc"},
    "MultiBody": {"do_parallel_discio", "do_combine_thru_disc"},
}

# internal_name -> program string, for jobs whose extracted program_guess is
# wrong for the DEFAULT configuration. Currently only TomoImport, whose
# extractor picked the first `command = "..."` literal it saw -- the
# do_coords==true coordinate-importer -- even though do_coords defaults to
# false and the real default program is the SerialEM tilt-series importer.
DRAFT_PROGRAM_OVERRIDE: dict[str, str] = {
    "TomoImport": "relion_python_tomo_import SerialEM",
}

# internal_name -> set of option keys that belong to a NON-default branch and
# must be left out of the default draft (and not flagged as "unmapped",
# since they're deliberately omitted, not un-handled). For TomoImport the
# default is the tilt-series importer, so the do_coords==true coordinate
# branch's own options (which happen to share flag spellings RELION reuses,
# e.g. --scale_factor / --add_factor) would otherwise leak into the default
# draft via the generic rule. Selecting "Import coordinates instead?" in the
# GUI switches to that branch; the command box is editable and the real
# source is shown for that case.
DRAFT_SUPPRESS: dict[str, set[str]] = {
    "TomoImport": {
        "in_coords",
        "remove_substring",
        "remove_substring2",
        "is_center",
        "scale_factor",
        "add_factor",
    },
    # "OR: use direct entries?" (use_direct_entries) never itself becomes a
    # CLI flag in getTomoInputCommmand() (src/pipeline_jobs.cpp ~6328) -- it
    # only selects, GUI-side, whether in_optimisation or the in_particles/
    # in_tomograms/in_trajectories trio gets used (see DRAFT_FLAG_MAP above).
    # Leaving it unmapped would flag a field that will never map to anything,
    # in every one of these jobs, not just a non-default branch.
    "Inimodel": {"use_direct_entries", "use_gpu"},
    "Class3D": {
        "use_direct_entries", "use_gpu",
        "do_helix", "do_apply_helical_symmetry", "do_local_search_helical_symmetry",
    },
    "Autorefine": {
        "use_direct_entries", "use_gpu",
        "do_helix", "do_apply_helical_symmetry", "do_local_search_helical_symmetry",
    },
    "TomoSubtomo": {"use_direct_entries"},
    "TomoCtfRefine": {"use_direct_entries"},
    "TomoAlign": {"use_direct_entries"},
    "TomoReconPart": {"use_direct_entries", "do_helix"},
    # Same reasoning as use_direct_entries above, for two more gating-only
    # booleans that never become a flag themselves -- they only control
    # whether OTHER fields' flags get emitted (see job_registry._evaluate_
    # condition, which now actually reads these values rather than
    # skipping their condition check entirely):
    #   use_gpu           -- gates --gpu (built from gpu_ids' value), never
    #                         a flag on its own (pipeline_jobs.cpp, all 6
    #                         jobs below use the identical
    #                         `if (joboptions["use_gpu"].getBoolean()) ...
    #                         command += " --gpu \"" + joboptions["gpu_ids"]
    #                         .getString() + "\"";` shape).
    #   do_helix           -- gates the helical_* fields on Maskcreate too
    #                         (Class3D/Autorefine already listed above).
    "Autopick": {"use_gpu"},
    "Class2D": {"use_gpu", "do_helix"},
    "MultiBody": {"use_gpu"},
    "Maskcreate": {"do_helix"},
    # "Do even/odd frame splitting?" (pipeline_jobs.cpp ~1554: condition
    # `is_tomo && joboptions["do_even_odd_split"].getBoolean()`). Given
    # is_tomo is a fixed, known-false constant in every draft this app
    # builds (see job_registry._evaluate_condition's is_tomo handling), this
    # box can never affect the default draft's command -- correctly omitted
    # rather than "unmapped" (there is nothing to fix; add `--even_odd_split`
    # to the editable command box by hand for a tomography tilt-series run
    # that wants it).
    "Motioncorr": {"do_even_odd_split"},
}


def draft_flag_for(internal_name: str, option_key: str) -> Optional[str]:
    """Verified CLI flag for this option key, or None to fall back to the
    generic `--<key>` rule. See DRAFT_FLAG_MAP."""
    return DRAFT_FLAG_MAP.get(internal_name, {}).get(option_key)


def draft_flag_is_negated(internal_name: str, option_key: str) -> bool:
    """True if this DRAFT_FLAG_MAP-mapped boolean's flag fires when the
    checkbox is UNCHECKED rather than checked. See DRAFT_NEGATED_FLAGS."""
    return option_key in DRAFT_NEGATED_FLAGS.get(internal_name, set())


def draft_program_override(internal_name: str) -> Optional[str]:
    """Verified program string for jobs whose extracted program_guess is
    wrong for the default configuration, else None. See DRAFT_PROGRAM_OVERRIDE."""
    return DRAFT_PROGRAM_OVERRIDE.get(internal_name)


def draft_is_suppressed(internal_name: str, option_key: str) -> bool:
    """True if this option belongs to a non-default branch and should be
    omitted from the default draft entirely. See DRAFT_SUPPRESS."""
    return option_key in DRAFT_SUPPRESS.get(internal_name, set())


# --------------------------------------------------------------------------
# Output-directory flag, per job type
# --------------------------------------------------------------------------
#
# RELION runs every job from the PROJECT ROOT and passes the job's output
# directory as a project-root-relative path, e.g. `--o Import/job001/`. Its
# getCommands*Job() functions append this themselves (see e.g. `command +=
# " --o " + outputname;` throughout src/pipeline_jobs.cpp). RELION-US mirrors
# that: the draft command includes the output flag pointing at
# `<JobDir>/jobNNN/`, and the runner executes from the project root (see
# job_runner.start_subprocess_job). Most programs take `--o`; RELION-5's
# Python tomo tools take `--output-directory` instead (verified in the same
# source, getCommandsTomo*Job()). Anything not listed uses the default `--o`.
DRAFT_OUTPUT_FLAG_DEFAULT = "--o"
# Verified per-job against getCommands*Job() in src/pipeline_jobs.cpp
# (RELION cloned 2026-08-14). NB: TomoAlignTiltSeries and
# TomoReconstructTomograms were checked and use plain `--o` (NOT
# --output-directory), so they are deliberately NOT listed here.
DRAFT_OUTPUT_FLAG: dict[str, str] = {
    "TomoImport": "--output-directory",           # default (SerialEM tilt-series) branch
    "TomoExcludeTiltImages": "--output-directory",
    "TomoPickTomograms": "--output-directory",
    "TomoDenoiseTomograms": "--output-directory",
}


def draft_output_flag(internal_name: str) -> str:
    """RELION's output-directory flag for this job type (`--o` for most,
    `--output-directory` for the RELION-5 Python tomo tools)."""
    return DRAFT_OUTPUT_FLAG.get(internal_name, DRAFT_OUTPUT_FLAG_DEFAULT)


# --------------------------------------------------------------------------
# Output-value suffix, per job type
# --------------------------------------------------------------------------
#
# For most jobs RELION's `--o`/`--output-directory` value is the bare job
# directory (e.g. `Import/job001/`), which is what the generic draft builder
# already emits. But several jobs append a literal suffix to `outputname` to
# form a FILE ROOTNAME PREFIX rather than a plain folder -- confirmed by
# reading each job's getCommands*Job() in src/pipeline_jobs.cpp (RELION
# cloned 2026-08-14). Missing this is what produces output files like
# `_it000_class001.mrc` instead of `run_it000_class001.mrc`: this app was
# always emitting the bare directory with no suffix, for every job,
# unconditionally.
#
# The five classic iterative-refinement jobs all set `fn_run = "run"` in
# their DEFAULT (non-continuation) branch -- RELION-US never models a
# "continue this job" run (see the `!is_continue` note above), so "run" is
# always correct here:
#   Class2D    -- src/pipeline_jobs.cpp ~3183  (command += " --o " + outputname + fn_run;)
#   Inimodel   -- src/pipeline_jobs.cpp ~3466  (same pattern)
#   Class3D    -- src/pipeline_jobs.cpp ~3860  (same pattern)
#   Autorefine -- src/pipeline_jobs.cpp ~4351  (same pattern)
#   MultiBody  -- src/pipeline_jobs.cpp ~4736-4744, `else` (non-continue)
#                 branch: fn_run = "run"; command += " --o " + outputname + fn_run;
#                 (MultiBody's SECOND command, appending "analyse" to launch
#                 relion_flex_analyse, is conditional on further GUI state
#                 and is deliberately NOT reproduced here -- left for the
#                 user to hand-edit, consistent with this project's policy
#                 against guessing at real per-job branching.)
#
# Two more jobs append a fixed, unconditional literal (verified by reading
# each function in full -- no branch controls this line):
#   Maskcreate  -- src/pipeline_jobs.cpp ~4942 (command += " --o " + outputname + "mask.mrc";)
#   Postprocess -- src/pipeline_jobs.cpp ~5340 (command += " --o " + outputname + "postprocess";)
#
# Deliberately NOT included (mode-branched or otherwise not safely
# reducible to a single default suffix -- left for hand-editing rather than
# risking a wrong guess):
#   Joinstar   -- suffix depends on which of fn_part/fn_mic/fn_mov is filled
#                 in (src/pipeline_jobs.cpp ~5069/5103/5137).
#   Localres   -- only appends "relion" in the do_relion_locres branch; the
#                 default ResMap branch uses a different program entirely
#                 (src/pipeline_jobs.cpp ~5510).
#   Select     -- the class_ranker branch appends bare `outputname` (no
#                 suffix) plus two EXTRA fixed flags, not a suffix change
#                 (src/pipeline_jobs.cpp ~2926).
DRAFT_OUTPUT_SUFFIX: dict[str, str] = {
    "Class2D": "run",
    "Inimodel": "run",
    "Class3D": "run",
    "Autorefine": "run",
    "MultiBody": "run",
    "Maskcreate": "mask.mrc",
    "Postprocess": "postprocess",
}


def draft_output_suffix(internal_name: str) -> Optional[str]:
    """Literal suffix RELION appends to the output directory to form a file
    rootname prefix (e.g. "run" -> `Refine3D/job001/run`), or None when the
    bare directory is correct. See DRAFT_OUTPUT_SUFFIX."""
    return DRAFT_OUTPUT_SUFFIX.get(internal_name)
