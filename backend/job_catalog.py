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
# off the source), and a genuinely branched job (TomoPickTomograms,
# TomoDenoiseTomograms) is left out of `flags`/`value_transforms` entirely
# rather than risk a subtly-wrong reconstruction -- its default draft stays
# program-name-only (plus, here, just its --output-directory flag) with the
# real source shown for hand-editing.


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
    # Extra command-line tokens to append right after the output
    # flag/subdir, computed from the CURRENT field values -- for a
    # compulsory argument RELION derives from OTHER fields rather than
    # reading directly off one JobOption (e.g. Import's --ofile, picked
    # from is_multiframe's own two-way branch; see _import_ofile_args
    # below). Returns [] for "nothing to add" (e.g. the branch this
    # computation covers isn't the one currently active). This is the one
    # slot in this table that runs code instead of holding data --
    # reserved for exactly this "compulsory value, computed from a couple
    # of already-known fields, not itself a JobOption" shape; anything
    # bigger belongs in the editable command box by hand instead, per this
    # project's policy against reconstructing real per-job branching.
    extra_output_args: Optional[Callable[[dict], list]] = None


def _import_ofile_args(field_values: dict) -> list:
    """relion_import's --ofile is compulsory (src/apps/import.cpp:49) and
    has no default -- without it the binary refuses to run at all
    (confirmed for real, running an Import job against RELION 5.0.1). Its
    value is a literal RELION picks per branch, not a JobOption string this
    app can just read: do_raw's is_multiframe checkbox selects
    "movies.star" vs "micrographs.star" (pipeline_jobs.cpp ~1312-1324) -- a
    plain two-way pick on one already-known boolean, safe to compute here.
    do_other's fn_out is derived from fn_in_other itself (basename, or a
    coords_suffix construction for the coordinate-import case) -- genuine
    per-node-type branch logic this app deliberately doesn't try to
    reconstruct (same policy as TomoImport's do_coords branch); that half
    is left for the user to add via the editable command box, same as
    is_multiframe itself already is (it shows in `unmapped`)."""
    if not field_values.get("do_raw"):
        return []
    ofile = "movies.star" if field_values.get("is_multiframe") else "micrographs.star"
    return ["--ofile", ofile]


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
            # Default is per-tilt-image dose; toggling "Is dose rate per
            # movie frame?" switches RELION to --dose-per-movie-frame (a
            # branch a static map can't express -- noted in the field help).
            "dose_rate": FlagOverride("--dose-per-tilt-image"),
            "prefix": FlagOverride("--prefix"),
            "mtf_file": FlagOverride("--mtf-file"),
            "flip_tiltseries_hand": FlagOverride("--invert-defocus-handedness"),
            "images_are_motion_corrected": FlagOverride("--images-are-motion-corrected"),
        },
        suppress=frozenset({
            "in_coords", "remove_substring", "remove_substring2",
            "is_center", "scale_factor", "add_factor",
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
    "TomoPickTomograms": JobDraftOverride(output_flag="--output-directory"),
    "TomoDenoiseTomograms": JobDraftOverride(output_flag="--output-directory"),
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
    "Inimodel": JobDraftOverride(
        output_suffix="run",
        flags={
            "in_optimisation": FlagOverride("--ios"), "in_particles": FlagOverride("--i"),
            "in_tomograms": FlagOverride("--tomograms"), "in_trajectories": FlagOverride("--trajectories"),
            "fn_img": FlagOverride("--i"),
            "do_parallel_discio": FlagOverride("--no_parallel_disc_io", negated=True),
            "do_combine_thru_disc": FlagOverride("--dont_combine_weights_via_disc", negated=True),
            "do_preread_images": FlagOverride("--preread_images"),
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
        },
        suppress=frozenset({
            "use_direct_entries", "use_gpu",
            "do_helix", "do_apply_helical_symmetry", "do_local_search_helical_symmetry",
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
        },
        suppress=frozenset({
            "use_direct_entries", "use_gpu",
            "do_helix", "do_apply_helical_symmetry", "do_local_search_helical_symmetry",
        }),
    ),
    "TomoSubtomo": JobDraftOverride(
        flags={
            "in_optimisation": FlagOverride("--i"), "in_particles": FlagOverride("--p"),
            "in_tomograms": FlagOverride("--t"), "in_trajectories": FlagOverride("--mot"),
        },
        suppress=frozenset({"use_direct_entries"}),
    ),
    "TomoCtfRefine": JobDraftOverride(
        flags={
            "in_optimisation": FlagOverride("--i"), "in_particles": FlagOverride("--p"),
            "in_tomograms": FlagOverride("--t"), "in_trajectories": FlagOverride("--mot"),
        },
        suppress=frozenset({"use_direct_entries"}),
    ),
    "TomoAlign": JobDraftOverride(
        flags={
            "in_optimisation": FlagOverride("--i"), "in_particles": FlagOverride("--p"),
            "in_tomograms": FlagOverride("--t"), "in_trajectories": FlagOverride("--mot"),
        },
        suppress=frozenset({"use_direct_entries"}),
    ),
    "TomoReconPart": JobDraftOverride(
        flags={
            "in_optimisation": FlagOverride("--i"), "in_particles": FlagOverride("--p"),
            "in_tomograms": FlagOverride("--t"), "in_trajectories": FlagOverride("--mot"),
        },
        suppress=frozenset({"use_direct_entries", "do_helix"}),
    ),
    "Class2D": JobDraftOverride(
        output_suffix="run",
        flags={
            "do_parallel_discio": FlagOverride("--no_parallel_disc_io", negated=True),
            "do_combine_thru_disc": FlagOverride("--dont_combine_weights_via_disc", negated=True),
            "do_preread_images": FlagOverride("--preread_images"),
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
        },
        suppress=frozenset({"use_gpu"}),
    ),
    # Motioncorr/Ctffind's `is_tomo`-guarded fields (job_registry._evaluate_
    # condition resolves the `is_tomo`/`!is_tomo` tokens itself -- see that
    # module for why False is the correct constant here). do_dose_weighting
    # and do_own_motioncor are additionally mapped because their OWN flag
    # also doesn't spell out as "--" + key, so they need a name override on
    # top of the self-guard:
    #   do_dose_weighting (~1641, condition `!is_tomo && joboptions[
    #   "do_dose_weighting"].getBoolean()`, itself self-guarded once
    #   is_tomo is known false): command += " --dose_weighting ";
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
    #   "do_even_odd_split"].getBoolean()`) -- given is_tomo is a fixed,
    #   known-false constant in every draft this app builds, this box can
    #   never affect the default draft's command; suppressed rather than
    #   "unmapped" (there is nothing to fix; add --even_odd_split to the
    #   editable command box by hand for a tomography tilt-series run that
    #   wants it).
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
        },
        suppress=frozenset({"do_even_odd_split"}),
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
    ),
    "Ctffind": JobDraftOverride(
        # (~1809-1813, condition `else { if (joboptions["use_noDW"]
        # .getBoolean()) ...}` -- the `else` is the !is_tomo branch,
        # likewise self-guarded once is_tomo is known false):
        #   command += " --use_noDW ";
        flags={"use_noDW": FlagOverride("--use_noDW")},
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
    # (`--ofile`, via _import_ofile_args above), not one bare `--o <dir>/`.
    # `--o` isn't even a recognized argument to the real relion_import
    # binary -- confirmed for real, running the default-settings draft
    # against RELION 5.0.1 failed immediately with "WARNING: Option --o is
    # not a valid RELION argument" plus "ERROR: Argument --odir not found" /
    # "--ofile not found" (both compulsory, per relion_import --help).
    "Import": JobDraftOverride(
        output_flag="--odir",
        flags={
            "fn_in_raw": FlagOverride("--i", condition="do_raw"),
            "fn_in_other": FlagOverride("--i", condition="do_other"),
        },
        extra_output_args=_import_ofile_args,
    ),
    "Autopick": JobDraftOverride(suppress=frozenset({"use_gpu"})),
    "Maskcreate": JobDraftOverride(
        output_suffix="mask.mrc",
        # do_helix -- gates the helical_* fields here too (Class3D/
        # Autorefine already listed above).
        suppress=frozenset({"do_helix"}),
    ),
    # Fixed, unconditional literal suffix (verified by reading the function
    # in full -- no branch controls this line): src/pipeline_jobs.cpp ~5340
    # (command += " --o " + outputname + "postprocess";).
    "Postprocess": JobDraftOverride(output_suffix="postprocess"),
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
#   - Ctfrefine.do_defocus/do_astig/do_bfactor/do_phase ("No"/"Per-
#     micrograph"/"Per-particle" -> single-letter codes concatenated into one
#     --fit_defocus-style flag via JobOption::getCtfFitString,
#     pipeline_jobs.cpp ~243-249).
#   - Joinstar's output suffix depends on which of fn_part/fn_mic/fn_mov is
#     filled in (src/pipeline_jobs.cpp ~5069/5103/5137).
#   - Localres only appends "relion" in the do_relion_locres branch; the
#     default ResMap branch uses a different program entirely
#     (src/pipeline_jobs.cpp ~5510).
#   - Select's class_ranker branch appends bare `outputname` (no suffix)
#     plus two EXTRA fixed flags, not a suffix change
#     (src/pipeline_jobs.cpp ~2926).


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


def draft_program_override(internal_name: str) -> Optional[str]:
    """Verified program string for jobs whose extracted program_guess is
    wrong for the default configuration, else None. See
    JobDraftOverride.program."""
    override = _override(internal_name)
    return override.program if override is not None else None


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


def draft_extra_output_args(internal_name: str, field_values: dict) -> list:
    """Extra command-line tokens a job's compulsory-but-computed output
    argument needs, appended right after the output flag/subdir -- e.g.
    Import's --ofile. [] if this job has none. See
    JobDraftOverride.extra_output_args."""
    override = _override(internal_name)
    if override is None or override.extra_output_args is None:
        return []
    return override.extra_output_args(field_values)
