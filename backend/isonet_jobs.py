"""
isonet_jobs.py -- wires IsoNet2 (github.com/IsoNet-cryoET/IsoNet2), a conda-
env-based deep-learning tool for tomogram missing-wedge correction,
denoising, and CTF correction, into this app's Jobs list as six chained job
types, one per IsoNet2 CLI module: IsonetPrepareStar, IsonetDeconv,
IsonetMakeMask, IsonetDenoise, IsonetRefine, IsonetPredict (isonet.py's own
`prepare_star`/`deconv`/`make_mask`/`denoise`/`refine`/`predict`, confirmed
reading every one of their signatures in IsoNet2's IsoNet/bin/isonet.py).

Deliberately NOT built on job_registry.py's DRAFT_OVERRIDES/
data/job_definitions_raw.json machinery: that table is a source-verified
overlay correcting the rare case where extraction from REAL, COMPILED
RELION C++ (getCommands*Job() in src/pipeline_jobs.cpp) got a flag wrong --
see job_catalog.JobDraftOverride's own docstring ("this project's policy
against reconstructing real per-job branching"). IsoNet2 has no RELION C++
class at all, so building its command logic through that table would be
exactly the invented-branching that policy exists to avoid, and would
corrupt what data/job_definitions_raw.json currently guarantees (every
entry there really was extracted from RELION source). build_isonet_command
below is IsoNet2's own, independent, hand-written command builder instead.

Also deliberately NOT built on custom_jobs.py's CUSTOM_JOB_DEFINITIONS/
CUSTOM_JOB_RUNNERS/job_runner.start_custom_job: that path runs entirely
in-process (no real subprocess, no SLURM -- confirmed no _run_slurm_job
branch exists in job_runner._run_custom), fine for the fast, synchronous
format-conversion bridges it was built for (IMOD/Warp/DeepETPicker/AreTomo2
imports) but not for IsoNet2's denoise/refine/predict, which are genuine
multi-hour GPU training/inference runs users need to submit to a cluster
like every other GPU-heavy RELION job. Command strings built here instead
go through job_runner.start_subprocess_job directly (see main.py), which
gives local execution AND SLURM submission on equal footing with real
RELION jobs, without touching the RELION-verified extraction table.

Catalog/display metadata (label_new/display_name/category/description) for
these six job types lives in job_catalog.py's CUSTOM_JOBS dict, the exact
same table ImodImport/WarpImport/DeepETPickerImport/AreTomoImport use --
IsoNet2 has no real RELION type label either, so it follows their fabricated
custom.isonet_* convention (relion_pipeliner won't recognize it; pipeline
registration falls back gracefully to this app's own numbering, the same
harmless degrade those four already rely on -- see job_runner.py's
_register_in_relion_pipeline).

Star-file chaining between the six stages (prepare_star -> deconv ->
make_mask -> denoise/refine -> predict) is purely conventional -- confirmed
there is no typed node-type registry anywhere in this codebase (RELION's
own node-graph computation happens only inside the compiled relion_pipeliner
binary, which won't recognize these fabricated labels either). Each
downstream stage's `star_file` option is an "inputnode" field with a
"*.star" glob pattern and a `default` guessing the typical prior stage's
output path -- identical to how DynaMight/ModelAngelo already do this.
"""
from __future__ import annotations

import shlex


def _isonet_flag(key: str, value) -> list[str]:
    """Two shell tokens, `--key` and its value, Fire-CLI style. Deliberately
    SPACE-separated (`--key value`), not `--key=value`, despite `isonet.py
    <cmd> --help` documenting flags in `=` form (e.g. `-f, --full=FULL`) --
    confirmed for real, running this app's actual `conda run -n <env>
    isonet.py ...` invocation: this machine's conda (25.1.1) mishandles
    `--key=value` tokens when they follow `conda run`, silently stripping
    the leading `--` before the child process ever sees them (isonet.py
    then fails with e.g. `'pixel_size=10' not recognized!`), while the
    identical flags in `--key value` form pass through untouched and parse
    correctly. Booleans are spelled out as True/False rather than a bare
    `--key` (Fire also accepts that form, but an explicit value is
    unambiguous and matches how this app already renders boolean RELION
    flags in its own draft commands)."""
    if isinstance(value, bool):
        rendered = "True" if value else "False"
    else:
        rendered = str(value)
    return [f"--{key}", shlex.quote(rendered)]


def _is_default(option: dict, value) -> bool:
    """An empty/unset value, or one matching the option's own declared
    default, is omitted from the draft command entirely -- keeps the command
    box short and matches this app's existing convention for real RELION
    jobs (job_registry._build_draft_command skips values that don't differ
    from what the flag would do if simply left off)."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    default = option.get("default")
    return str(value) == str(default)


def build_isonet_command(internal_name: str, field_values: dict, output_subdir: str) -> str:
    """Builds the full, editable shell command for one IsoNet2 job: a
    `conda run -n <env>` prefix (the "conda environment name" field the user
    asked for -- see ISONET_JOB_DEFINITIONS' conda_env option below) around
    the real isonet.py subcommand invocation.

    output_subdir is always this job's own <JobDir>/jobNNN (already created
    on disk by the time this runs -- job_runner.start_subprocess_job mkdirs
    it before building/launching the command). Deliberately NOT a
    user-editable field (like every other job type in this app, output
    always lands in the job's own tracked directory, so Outputs/Clean/Delete
    and successive runs stay honest -- see custom_jobs.py's _resolve_out
    docstring for the same reasoning applied to the import bridges).

    prepare_star is the one module with no --output_dir of its own at all
    (confirmed against a real install: `isonet.py prepare_star --help` has
    no such flag) -- it only takes --star_name, written via a literal
    `starfile.write(df, star_name)` with no directory joining performed
    internally. So for prepare_star specifically, the job's own directory is
    folded into --star_name instead (unless the user already typed a
    path containing "/", in which case it's respected as given, matching
    how a RELION output-path field works when hand-edited).

    Every OTHER stage's --star_file is first COPIED into this job's own
    directory (as tomograms.star), and isonet.py is pointed at that copy,
    not the user's original -- deconv/make_mask/predict all write their new
    column (rlnDeconvTomoName/rlnMaskName/rlnCorrectedTomoName or
    rlnDenoisedTomoName) back onto whatever file --star_file points at IN
    PLACE (confirmed reading IsoNet/utils/utils.py's process_tomograms:
    `starfile.write(new_star, star_path)`, where star_path IS the input
    path -- it never writes into --output_dir), and denoise/refine's own
    "with preview" step does the exact same thing internally via a call to
    self.predict(). Without this copy, every one of these jobs would
    silently rewrite whatever star file the user pointed it at -- possibly
    a much earlier job's own tracked output, or even the SAME file two
    sibling jobs both read from -- exactly the "successive runs silently
    overwrite one shared file" problem custom_jobs.py's _resolve_out
    already avoids for the import bridges (see its own docstring). The copy
    keeps this job's result in ITS OWN directory (Outputs tab / Clean /
    Delete stay honest, matching every other job in this app) and leaves
    the input star -- and whatever job produced it -- untouched, so it can
    be reused by a second, different run.
    """
    definition = ISONET_JOB_DEFINITIONS[internal_name]
    field_values = field_values or {}
    env = str(field_values.get("conda_env") or "isonet2_environment").strip() or "isonet2_environment"

    subdir_arg = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    job_star = subdir_arg + _JOB_STAR_FILENAME
    source_star = str(field_values.get("star_file") or "").strip() if internal_name != "IsonetPrepareStar" else ""

    prefix: list[str] = []
    if source_star:
        prefix = ["cp", shlex.quote(source_star), shlex.quote(job_star), "&&"]

    parts = prefix + [
        "conda", "run", "--no-capture-output", "-n", shlex.quote(env),
        "isonet.py", definition["subcommand"],
    ]
    if internal_name != "IsonetPrepareStar":
        parts.extend(_isonet_flag("output_dir", subdir_arg))

    for option in definition["options"]:
        key = option["key"]
        if key == "conda_env":
            continue  # consumed above, not an isonet.py flag
        value = field_values.get(key, option.get("default"))
        if internal_name == "IsonetPrepareStar" and key == "star_name":
            star_name = str(value if not _is_default(option, value) else option["default"])
            if "/" not in star_name:
                star_name = subdir_arg + star_name
            parts.extend(_isonet_flag("star_name", star_name))
            continue
        if key == "star_file" and internal_name != "IsonetPrepareStar":
            if source_star:
                # Points at the COPY just made above, not the user's original.
                parts.extend(_isonet_flag("star_file", job_star))
            continue
        if _is_default(option, value):
            continue
        parts.extend(_isonet_flag(key, value))
    return " ".join(parts)


_ARCH_CHOICES = [
    "unet-small", "unet-medium", "unet-large",
    "scunet-small", "scunet-medium", "scunet-large", "scunet-fast", "scunet-fast-large",
]  # exhaustive: IsoNet/models/network.py's Net.initialize() branches on exactly these 8

_CONDA_ENV_OPTION = {
    "key": "conda_env", "field_type": "text", "label": "Conda environment name:",
    "default": "isonet2_environment",
    "help": "The conda environment IsoNet2 was installed into (see isonet2_environment.yml / install.sh). "
            "The command is run as `conda run -n <this> isonet.py ...`.",
}

_GPU_IDS_OPTION = {
    "key": "gpuID", "field_type": "text", "label": "Which GPUs to use:",
    "default": "",
    "help": "GPU IDs, comma-separated (e.g. \"0\" or \"0,1,2,3\"). Leave blank to let IsoNet2 pick.",
}

_TOMO_IDX_OPTION = {
    "key": "tomo_idx", "field_type": "text", "label": "Tomogram indices (optional):",
    "default": "",
    "help": "Process only these STAR row indices, 1-based (e.g. \"1,2,4\" or \"5-10,15,16\"). Leave blank for all.",
}

_SNRFALLOFF_DECONV_OPTIONS = [
    {"key": "snrfalloff", "field_type": "slider", "label": "SNR falloff:", "default": 1.0,
     "min": 0.0, "max": 5.0, "step": 0.1,
     "help": "Frequency-dependent SNR attenuation. Larger values reduce high-frequency contribution more "
             "aggressively (more stable on noisy data); smaller values preserve more high-frequency content "
             "but risk amplifying noise."},
    {"key": "deconvstrength", "field_type": "slider", "label": "Deconvolution strength:", "default": 1.0,
     "min": 0.0, "max": 5.0, "step": 0.1,
     "help": "Scalar multiplier for deconvolution strength. Higher emphasizes correction and low-frequency "
             "recovery but can introduce ringing/artifacts if set too high."},
    {"key": "highpassnyquist", "field_type": "slider", "label": "High-pass cutoff (fraction of Nyquist):",
     "default": 0.02, "min": 0.0, "max": 0.5, "step": 0.005,
     "help": "Very-low-frequency high-pass cutoff, as a fraction of Nyquist. Removes large-scale intensity "
             "gradients/drift; usually left at default."},
]


# The filename build_isonet_command copies every downstream stage's input
# star into, inside that job's OWN output directory, before running
# isonet.py against the copy -- see build_isonet_command's own docstring
# for why (deconv/make_mask/predict/with_preview all mutate --star_file IN
# PLACE; copying first keeps that mutation inside this job's own tracked
# directory instead of silently rewriting an earlier job's output).
_JOB_STAR_FILENAME = "tomograms.star"


def _star_input_option(default_guess: str, help_extra: str = "") -> dict:
    return {
        "key": "star_file", "field_type": "inputnode", "label": "Input tomograms STAR file (required):",
        "default": default_guess, "pattern": "*.star",
        "help": (
            f"Required -- IsoNet2 cannot run without it. {help_extra} "
            "This job COPIES the star file you point it at into its own output directory (as "
            "tomograms.star) and works on that copy -- your input file is never modified. That copy, "
            "not your original input, is what a following stage should be pointed at to continue the "
            "chain (this is why the default above points at a PRIOR job's own output, not the ultimate "
            "Prepare Star source)."
        ).strip(),
    }


ISONET_JOB_DEFINITIONS = {
    "IsonetPrepareStar": {
        "internal_name": "IsonetPrepareStar",
        "subcommand": "prepare_star",
        "label_new": "custom.isonet_prepare_star",
        "display_name": "IsoNet2 – Prepare Star",
        "category": "IsoNet (Beta)",
        "description": "Generate a tomograms.star file for IsoNet2 from folder(s) of reconstructed tomograms",
        "options": [
            _CONDA_ENV_OPTION,
            # field_type "directory" -- folder-only Browse button (frontend/
            # app.js's pickFileDialog in mode:"directory": lists subfolders
            # only, with a "Select This Folder" action instead of picking on
            # click), distinct from "filename"/"inputnode" below, which only
            # ever pick a single FILE.
            #
            # full vs. even+odd is a genuine EITHER/OR, not two independently
            # optional fields -- confirmed both in isonet.py's own source
            # (`count_folder = full if full not in ["None", None] else even`;
            # leaving all three at "None" crashes with a bare
            # FileNotFoundError('None'), confirmed running this for real) and
            # in the GUI/README (the GUI's Prepare tab has an explicit
            # "Even/Odd Input" toggle; FAQ: "Use even/odd... for
            # --method isonet2-n2n... Use full tomograms for... --method
            # isonet2 when movies/tilt-series are not available").
            {"key": "full", "field_type": "directory", "label": "Full tomograms folder:", "default": "None",
             "help": "One of two ways to supply tomograms -- either this, OR even+odd below (not both; "
             "leaving all three at \"None\" fails). Directory containing full tomogram(s) (.mrc/.rec), "
             "for single-map training (--method isonet2 downstream). Use this when you don't have "
             "even/odd halves (e.g. no separate movies/tilt-series to split)."},
            {"key": "even", "field_type": "directory", "label": "Even half-tomograms folder:", "default": "None",
             "help": "One of two ways to supply tomograms -- this + odd below, OR full above (not both). "
             "Directory containing even half-tomograms, for Noise2Noise training (--method isonet2-n2n "
             "downstream) -- generally recommended over full when you have paired halves, since it gives "
             "better denoising (per IsoNet2's own FAQ)."},
            {"key": "odd", "field_type": "directory", "label": "Odd half-tomograms folder:", "default": "None",
             "help": "Must be set together with even above (both or neither). Directory containing odd "
             "half-tomograms."},
            {"key": "mask_folder", "field_type": "directory", "label": "Mask folder (optional):", "default": "None",
             "help": "Optional. Directory containing pre-made mask files for the tomograms, if you already "
             "have them -- most users should skip this and use the separate IsoNet2 – Make Mask job instead, "
             "which generates masks automatically after this one."},
            {"key": "coordinate_folder", "field_type": "directory", "label": "Coordinate folder (optional):",
             "default": "None",
             "help": "Optional. Directory containing subtomogram coordinate files, if you already have "
             "them. When set, the number of subtomograms is taken from these files INSTEAD of the "
             "\"Subtomograms per tomogram\" field below, which is then ignored."},
            {"key": "star_name", "field_type": "text", "label": "Output STAR filename:", "default": "tomograms.star",
             "help": "Name of the generated STAR file, written into this job's output directory. Every "
             "downstream IsoNet2 stage should be pointed at this same file (see its own star-file field's "
             "help for why)."},
            {"key": "pixel_size", "field_type": "text", "label": "Pixel size (Å, or \"auto\"):", "default": "auto",
             "help": "Optional. Pixel size in Ångstroms. Leave as \"auto\" to read it from the tomogram "
             "headers -- override only if there's no usable metadata or you need a different value. Aim "
             "for ~10Å/px binned; extreme deviations aren't recommended (target Z resolution is ~30Å)."},
            {"key": "defocus", "field_type": "text", "label": "Defocus (Å, zero-tilt):", "default": "10000",
             "help": "Optional. Defocus at zero tilt, in Ångstroms -- a single value applies to every "
             "tomogram, or give a comma-separated list (one value per tomogram). Only used for CTF "
             "correction later, not for missing-wedge geometry. If you don't know it yet, leave the "
             "default and edit the generated STAR's rlnDefocus column by hand afterward (the IsoNet2 GUI's "
             "own tutorial does exactly this)."},
            {"key": "cs", "field_type": "slider", "label": "Spherical aberration Cs (mm):", "default": 2.7,
             "min": 0.0, "max": 10.0, "step": 0.1,
             "help": "Optional, only used for CTF correction later. Spherical aberration, in mm -- from "
             "your microscope's specifications."},
            {"key": "voltage", "field_type": "text", "label": "Voltage (kV):", "default": "300",
             "help": "Optional, only used for CTF correction later. Acceleration voltage, in kV."},
            {"key": "ac", "field_type": "slider", "label": "Amplitude contrast:", "default": 0.1,
             "min": 0.0, "max": 1.0, "step": 0.01,
             "help": "Optional, only used for CTF correction later. Amplitude contrast fraction."},
            {"key": "tilt_min", "field_type": "text", "label": "Minimum tilt angle (°):", "default": "-60",
             "help": "Optional. Defines the shape of the missing-wedge mask used during training -- "
             "override if your acquisition's tilt range differs from ±60°."},
            {"key": "tilt_max", "field_type": "text", "label": "Maximum tilt angle (°):", "default": "60",
             "help": "Optional, paired with the minimum above. Override if your tilt range differs."},
            {"key": "create_average", "field_type": "boolean", "label": "Create averaged full tomograms:",
             "default": False,
             "help": "Optional, and only meaningful when even+odd are set (ignored for full). Sums the "
             "even and odd folders into full tomograms alongside the halves -- useful so the Deconvolution "
             "and Make Mask stages have a full tomogram to work from even though you're training "
             "Noise2Noise on the halves (see the FAQ: 'When should I use CTF deconvolution?')."},
            {"key": "number_subtomos", "field_type": "text", "label": "Subtomograms per tomogram:", "default": "auto",
             "help": "Optional; ignored if a coordinate folder is set above (see its own help). \"auto\" "
             "divides 3000 total subtomograms per epoch across your tomograms. Increasing this is like "
             "increasing training exposure (more runtime/memory); decreasing it is not recommended. Can "
             "also be edited per-tomogram directly in the generated STAR file afterward."},
        ],
        "standard_groups": [
            {"name": "", "fields": ["conda_env", "full", "even", "odd"]},
            {"name": "Optional inputs", "fields": ["mask_folder", "coordinate_folder"]},
            {"name": "Output", "fields": ["star_name"]},
            {"name": "Acquisition metadata (for later CTF correction)",
             "fields": ["pixel_size", "defocus", "cs", "voltage", "ac"]},
            {"name": "Missing-wedge geometry", "fields": ["tilt_min", "tilt_max"]},
            {"name": "Subtomogram sampling", "fields": ["create_average", "number_subtomos"]},
        ],
    },
    "IsonetDeconv": {
        "internal_name": "IsonetDeconv",
        "subcommand": "deconv",
        "label_new": "custom.isonet_deconv",
        "display_name": "IsoNet2 – CTF Deconvolution",
        "category": "IsoNet (Beta)",
        "description": "CTF deconvolution preprocessing for tomograms (enhances low-resolution contrast)",
        "options": [
            _CONDA_ENV_OPTION,
            _star_input_option(
                "IsonetPrepareStar/job001/tomograms.star",
                "Typically the output of an IsoNet2 – Prepare Star job.",
            ),
            {"key": "input_column", "field_type": "text", "label": "Input STAR column:", "default": "rlnTomoName",
             "help": "Optional. STAR column to read tomogram paths from -- rlnTomoName (full tomograms) is "
             "the only column this module reads directly; there's no even/odd form of deconv."},
            *_SNRFALLOFF_DECONV_OPTIONS,
            {"key": "chunk_size", "field_type": "text", "label": "Chunk size (voxels, optional):", "default": "",
             "help": "Optional. Process tomograms in cubic chunks of this size, to reduce memory usage on "
             "very large tomograms or limited RAM/VRAM. May create edge artifacts if too small. Leave "
             "blank to disable (the overlap fraction below is then unused)."},
            {"key": "overlap_rate", "field_type": "slider", "label": "Chunk overlap fraction:", "default": 0.25,
             "min": 0.0, "max": 0.9, "step": 0.05,
             "help": "Optional, and only meaningful when chunk size above is set (ignored otherwise). "
             "Fractional overlap between adjacent chunks. Larger overlaps reduce edge artifacts at the "
             "cost of extra computation."},
            {"key": "ncpus", "field_type": "text", "label": "CPU workers:", "default": "4",
             "help": "Optional. Number of CPU workers for CPU-bound parts of deconvolution."},
            {"key": "phaseflipped", "field_type": "boolean", "label": "Input already phase-flipped:", "default": False,
             "help": "Optional. If checked, input is assumed already phase-flipped -- keep this consistent "
             "with the \"input already phase-flipped\" fields on any downstream Denoise/Refine/Predict job "
             "using the same tomograms."},
            _TOMO_IDX_OPTION,
        ],
        "standard_groups": [
            {"name": "", "fields": ["conda_env", "star_file", "input_column"]},
            {"name": "Deconvolution strength", "fields": ["snrfalloff", "deconvstrength", "highpassnyquist"]},
            {"name": "Performance", "fields": ["chunk_size", "overlap_rate", "ncpus"]},
            {"name": "CTF handling", "fields": ["phaseflipped"]},
            {"name": "Subset", "fields": ["tomo_idx"]},
        ],
    },
    "IsonetMakeMask": {
        "internal_name": "IsonetMakeMask",
        "subcommand": "make_mask",
        "label_new": "custom.isonet_make_mask",
        "display_name": "IsoNet2 – Make Mask",
        "category": "IsoNet (Beta)",
        "description": "Generate sampling masks for tomograms, to prioritize regions of interest during "
                       "training. Recommended before Refine; not necessary before Denoise (per IsoNet2's own FAQ).",
        "options": [
            _CONDA_ENV_OPTION,
            _star_input_option(
                "IsonetDeconv/job001/tomograms.star",
                "Typically the output of an IsoNet2 – CTF Deconvolution or IsoNet2 – Denoise (Train) job.",
            ),
            {"key": "input_column", "field_type": "text", "label": "Input STAR column:", "default": "rlnDeconvTomoName",
             "help": "Optional -- has a built-in fallback chain, so leaving the default is usually fine: "
             "tries rlnDeconvTomoName first, then rlnTomoName, then rlnTomoReconstructedTomogramHalf1. "
             "IsoNet2's own GUI recommends pointing this at whichever processed column you have -- "
             "rlnDenoisedTomoName after Denoise, rlnDeconvTomoName after Deconvolution, or "
             "rlnCorrectedTomoName if re-masking an already-refined dataset -- and specifically warns "
             "that the raw, unprocessed columns this fallback chain ends on (rlnTomoName / "
             "rlnTomoReconstructedTomogramHalf1) \"will likely generate poor masks.\""},
            {"key": "patch_size", "field_type": "slider", "label": "Local patch size:", "default": 4,
             "min": 1, "max": 32, "step": 1,
             "help": "Optional. Local patch size used for max/std local filters. Larger values smooth "
             "detection of specimen regions."},
            {"key": "density_percentage", "field_type": "slider", "label": "Density percentile kept:", "default": 50,
             "min": 0, "max": 100, "step": 1,
             "help": "Optional. Percentage of voxels retained by local-density ranking. Lower values "
             "create stricter masks. Raise this (less strict) if a mask misses specimen regions."},
            {"key": "std_percentage", "field_type": "slider", "label": "Std-dev percentile kept:", "default": 50,
             "min": 0, "max": 100, "step": 1,
             "help": "Optional. Percentage of voxels retained by local-standard-deviation ranking. Lower "
             "values emphasize textured regions. Raise this (less strict) if a mask misses specimen regions."},
            {"key": "z_crop", "field_type": "slider", "label": "Z crop fraction:", "default": 0.2,
             "min": 0.0, "max": 0.9, "step": 0.05,
             "help": "Optional. Fraction of tomogram Z cropped from both ends (masks out the top and "
             "bottom, each half this fraction) to avoid sampling low-quality reconstruction edges."},
            _TOMO_IDX_OPTION,
        ],
        "standard_groups": [
            {"name": "", "fields": ["conda_env", "star_file", "input_column"]},
            {"name": "Mask sensitivity", "fields": ["patch_size", "density_percentage", "std_percentage", "z_crop"]},
            {"name": "Subset", "fields": ["tomo_idx"]},
        ],
    },
    "IsonetDenoise": {
        "internal_name": "IsonetDenoise",
        "subcommand": "denoise",
        "label_new": "custom.isonet_denoise",
        "display_name": "IsoNet2 – Denoise (Train)",
        "category": "IsoNet (Beta)",
        "description": "Train a Noise2Noise denoising model on even/odd tomogram pairs",
        "options": [
            _CONDA_ENV_OPTION,
            _star_input_option(
                "IsonetPrepareStar/job001/tomograms.star",
                "Needs even/odd tomogram pairs (rlnTomoReconstructedTomogramHalf1/2) for Noise2Noise training.",
            ),
            _GPU_IDS_OPTION,
            {"key": "ncpus", "field_type": "text", "label": "CPU workers:", "default": "16",
             "help": "Optional. Number of CPUs used for data processing."},
            {"key": "arch", "field_type": "radio", "label": "Network architecture:", "default": "unet-medium",
             "options": _ARCH_CHOICES,
             "help": "Optional. Determines model capacity and VRAM requirements -- pick a smaller "
             "architecture (or reduce cube size / batch size below, or enable mixed precision) if you "
             "run out of GPU memory."},
            {"key": "pretrained_model", "field_type": "filename", "label": "Pretrained model (optional):",
             "default": "", "pattern": "*.pt",
             "help": "Optional, for continuing an earlier run only. Path to a trained model checkpoint to "
             "continue training from -- its architecture/cube_size/CTF_mode are reloaded from the "
             "checkpoint, overriding the fields on this page."},
            {"key": "cube_size", "field_type": "slider", "label": "Training cube size (voxels):", "default": 96,
             "min": 32, "max": 256, "step": 8,
             "help": "Optional. Size of training subvolumes -- any multiple of 16, 64 or larger. Larger "
             "cubes use more GPU memory."},
            {"key": "epochs", "field_type": "slider", "label": "Epochs:", "default": 50,
             "min": 1, "max": 500, "step": 1,
             "help": "Optional. Number of training epochs -- IsoNet2's own FAQ recommends at least 50."},
            {"key": "batch_size", "field_type": "text", "label": "Batch size (or \"auto\"):", "default": "auto",
             "help": "Optional. Subtomograms per optimization step. \"auto\" picks GPUs×2 (or 4 for a "
             "single GPU) -- reduce this (minimum: your GPU count) if you run out of GPU memory."},
            {"key": "loss_func", "field_type": "radio", "label": "Loss function:", "default": "L2",
             "options": ["L2", "Huber", "L1"], "help": "Optional. Training loss function."},
            {"key": "save_interval", "field_type": "slider", "label": "Checkpoint save interval (epochs):",
             "default": 10, "min": 1, "max": 100, "step": 1,
             "help": "Optional. Interval, in epochs, between saved checkpoints -- also how often the "
             "preview below (if enabled) updates."},
            {"key": "learning_rate", "field_type": "text", "label": "Learning rate:", "default": "3e-4",
             "help": "Optional. Initial learning rate."},
            {"key": "learning_rate_min", "field_type": "text", "label": "Minimum learning rate:", "default": "3e-4",
             "help": "Optional. Minimum learning rate for the scheduler."},
            {"key": "mixed_precision", "field_type": "boolean", "label": "Mixed precision (fp16):", "default": True,
             "help": "Optional. Uses float16/mixed precision to reduce VRAM and speed up training, if your "
             "GPU and drivers support it."},
            {"key": "CTF_mode", "field_type": "radio", "label": "CTF handling mode:", "default": "None",
             "options": ["None", "phase_only", "network", "wiener"],
             "help": "Optional. None: no CTF correction. phase_only: phase-only correction. network: "
             "CTF-shaped filter on network input. wiener: Wiener filter on network target."},
            {"key": "isCTFflipped", "field_type": "boolean", "label": "Input already phase-flipped:", "default": False,
             "help": "Optional. Whether input tomograms are already phase-flipped -- keep this consistent "
             "with any upstream Deconvolution job's own \"already phase-flipped\" field for the same data."},
            {"key": "do_phaseflip_input", "field_type": "boolean", "label": "Apply phase flip during training:",
             "default": True, "help": "Optional. Whether to apply phase flip during training."},
            {"key": "bfactor", "field_type": "slider", "label": "B-factor:", "default": 0,
             "min": 0, "max": 500, "step": 10,
             "help": "Optional. B-factor to boost high-frequency content. Recommend 0 for cellular "
             "tomograms; 200-300 for isolated samples."},
            {"key": "clip_first_peak_mode", "field_type": "radio", "label": "Clip first CTF peak mode:", "default": "1",
             "options": ["0", "1", "2", "3"],
             "help": "Optional. Attenuates the overrepresented very-low-frequency CTF peak. 0: none, "
             "1: constant clip, 2: negative sine, 3: cosine. 2/3 might increase low-resolution contrast."},
            *_SNRFALLOFF_DECONV_OPTIONS,
            {"key": "with_preview", "field_type": "boolean", "label": "Predict a preview after training:",
             "default": True, "help": "Optional. Runs a prediction with the latest checkpoint every save "
             "interval (above), so you can watch results improve live -- the tomogram index below only "
             "matters when this is on."},
            {"key": "prev_tomo_idx", "field_type": "text", "label": "Preview tomogram index:", "default": "1",
             "help": "Optional, and only used when \"predict a preview\" above is on. STAR row index (or "
             "range, e.g. \"1,2,4\") to auto-predict for the preview."},
        ],
        "standard_groups": [
            {"name": "", "fields": ["conda_env", "star_file", "gpuID", "ncpus"]},
            {"name": "Network architecture", "fields": ["arch", "cube_size", "pretrained_model"]},
            {"name": "Training", "fields": [
                "epochs", "batch_size", "loss_func", "learning_rate", "learning_rate_min",
                "save_interval", "mixed_precision",
            ]},
            {"name": "CTF handling", "fields": [
                "CTF_mode", "isCTFflipped", "do_phaseflip_input", "bfactor", "clip_first_peak_mode",
            ]},
            {"name": "CTF deconvolution (used alongside CTF handling above)",
             "fields": ["snrfalloff", "deconvstrength", "highpassnyquist"]},
            {"name": "Live preview", "fields": ["with_preview", "prev_tomo_idx"]},
        ],
    },
    "IsonetRefine": {
        "internal_name": "IsonetRefine",
        "subcommand": "refine",
        "label_new": "custom.isonet_refine",
        "display_name": "IsoNet2 – Refine (Train)",
        "category": "IsoNet (Beta)",
        "description": "Train IsoNet2's missing-wedge correction model (isonet2 / isonet2-n2n)",
        "options": [
            _CONDA_ENV_OPTION,
            _star_input_option(
                "IsonetMakeMask/job001/tomograms.star",
                "IsoNet2's own FAQ: masks are recommended for every refine run (though not strictly "
                "required -- point this at an IsoNet2 – CTF Deconvolution job's output instead to skip "
                "masking).",
            ),
            _GPU_IDS_OPTION,
            {"key": "ncpus", "field_type": "text", "label": "CPU workers:", "default": "16",
             "help": "Optional. Number of CPUs used for data processing."},
            {"key": "method", "field_type": "radio", "label": "Method:", "default": "auto",
             "options": ["auto", "isonet2", "isonet2-n2n"],
             "help": "Optional, but NOT purely cosmetic: \"auto\" detects from the STAR file's own columns "
             "(rlnTomoName present -> isonet2; rlnTomoReconstructedTomogramHalf1/2 present -> "
             "isonet2-n2n) -- but if a star file somehow has BOTH sets of columns, auto-detection is "
             "ambiguous and isonet.py raises an error demanding you set this explicitly instead. Leaving "
             "it on \"auto\" is fine for a normal single-purpose star file."},
            {"key": "arch", "field_type": "radio", "label": "Network architecture:", "default": "unet-medium",
             "options": _ARCH_CHOICES,
             "help": "Optional. Determines model capacity and VRAM requirements -- pick a smaller "
             "architecture (or reduce cube size / batch size below, or enable mixed precision) if you "
             "run out of GPU memory."},
            {"key": "pretrained_model", "field_type": "filename", "label": "Pretrained model (optional):",
             "default": "", "pattern": "*.pt",
             "help": "Optional, for continuing an earlier run only. Path to a trained model checkpoint to "
             "continue training from -- its architecture/cube_size/CTF_mode are reloaded from the "
             "checkpoint, overriding the fields on this page."},
            {"key": "cube_size", "field_type": "slider", "label": "Training cube size (voxels):", "default": 96,
             "min": 32, "max": 256, "step": 8,
             "help": "Optional. Size of training subvolumes -- any multiple of 16, 64 or larger. Larger "
             "cubes use more GPU memory; if you run low on disk space, this is the field to reduce first "
             "(back to the default 96, per IsoNet2's own tutorial)."},
            {"key": "epochs", "field_type": "slider", "label": "Epochs:", "default": 50,
             "min": 1, "max": 500, "step": 1,
             "help": "Optional. Number of training epochs -- IsoNet2's own FAQ recommends at least 50."},
            {"key": "input_column", "field_type": "text", "label": "Input STAR column:", "default": "rlnDeconvTomoName",
             "help": "Optional. STAR column to use as input tomograms -- the default assumes a "
             "Deconvolution pass already ran. If you skipped deconv, change this to rlnTomoName (full "
             "tomograms) instead; for isonet2-n2n, the even/odd half columns are used automatically and "
             "this field is not read."},
            {"key": "batch_size", "field_type": "text", "label": "Batch size (or \"auto\"):", "default": "auto",
             "help": "Optional. Subtomograms per optimization step. \"auto\" picks GPUs×2 (or 4 for a "
             "single GPU) -- reduce this (minimum: your GPU count) if you run out of GPU memory."},
            {"key": "loss_func", "field_type": "radio", "label": "Loss function:", "default": "L2",
             "options": ["L2", "Huber", "L1"], "help": "Optional. Training loss function."},
            {"key": "learning_rate", "field_type": "text", "label": "Learning rate:", "default": "3e-4",
             "help": "Optional. Initial learning rate."},
            {"key": "save_interval", "field_type": "slider", "label": "Checkpoint save interval (epochs):",
             "default": 10, "min": 1, "max": 100, "step": 1,
             "help": "Optional. Interval, in epochs, between saved checkpoints -- also how often the "
             "preview below (if enabled) updates."},
            {"key": "learning_rate_min", "field_type": "text", "label": "Minimum learning rate:", "default": "3e-4",
             "help": "Optional. Minimum learning rate for the scheduler."},
            {"key": "mw_weight", "field_type": "text", "label": "Missing-wedge loss weight:", "default": "-1",
             "help": "Optional. Weight for missing-wedge loss; -1 (default) disables it, using a single "
             "combined loss for both missing-wedge correction and denoising. IsoNet2's own FAQ recommends "
             "20-200 to prioritize missing-wedge reconstruction over general denoising."},
            {"key": "apply_mw_x1", "field_type": "boolean", "label": "Apply missing wedge to subtomograms:",
             "default": True, "help": "Optional. Whether to apply the missing wedge to subtomograms at "
             "the start."},
            {"key": "mixed_precision", "field_type": "boolean", "label": "Mixed precision (fp16):", "default": True,
             "help": "Optional. Uses float16/mixed precision to reduce VRAM and speed up training, if your "
             "GPU and drivers support it."},
            {"key": "CTF_mode", "field_type": "radio", "label": "CTF handling mode:", "default": "None",
             "options": ["None", "phase_only", "network", "wiener"],
             "help": "Optional. None: no CTF correction. phase_only: phase-only correction. network: "
             "CTF-shaped filter on network input (per IsoNet2's own FAQ, generally gives the highest "
             "resolution detail, paired with clip_first_peak_mode 1 below). wiener: Wiener filter on "
             "network target (also works well, but needs more hyperparameter tuning -- FAQ recommends "
             "snrfalloff 0-1 and deconvstrength 1-5 below in that case)."},
            {"key": "clip_first_peak_mode", "field_type": "radio", "label": "Clip first CTF peak mode:", "default": "1",
             "options": ["0", "1", "2", "3"],
             "help": "Optional. Attenuates the overrepresented very-low-frequency CTF peak. 0: none, "
             "1: constant clip, 2: negative sine, 3: cosine. 2/3 might increase low-resolution contrast "
             "for specific datasets."},
            {"key": "bfactor", "field_type": "slider", "label": "B-factor:", "default": 0,
             "min": 0, "max": 500, "step": 10,
             "help": "Optional. B-factor to boost high-frequency content. Recommend 0 for cellular "
             "tomograms; 200-300 for isolated samples."},
            {"key": "isCTFflipped", "field_type": "boolean", "label": "Input already phase-flipped:", "default": False,
             "help": "Optional. Whether input tomograms are already phase-flipped -- keep this consistent "
             "with any upstream Deconvolution job's own \"already phase-flipped\" field for the same data."},
            {"key": "do_phaseflip_input", "field_type": "boolean", "label": "Apply phase flip during training:",
             "default": True, "help": "Optional. Whether to apply phase flip during training."},
            {"key": "noise_level", "field_type": "slider", "label": "Synthetic noise level:", "default": 0.0,
             "min": 0.0, "max": 5.0, "step": 0.1,
             "help": "Optional. Adds artificial noise during training -- the filter below only matters "
             "once this is above 0."},
            {"key": "noise_mode", "field_type": "radio", "label": "Synthetic noise filter:", "default": "nofilter",
             "options": ["nofilter", "ramp", "hamming"],
             "help": "Optional, and only meaningful when the noise level above is greater than 0. Filter "
             "applied when generating synthetic noise."},
            {"key": "random_rot_weight", "field_type": "slider", "label": "Random rotation augmentation:",
             "default": 0.2, "min": 0.0, "max": 1.0, "step": 0.05,
             "help": "Optional. Fraction of training samples that get a random-rotation augmentation."},
            {"key": "with_preview", "field_type": "boolean", "label": "Predict a preview after training:",
             "default": True, "help": "Optional. Runs a prediction with the latest checkpoint every save "
             "interval (above), so you can watch results improve live -- the tomogram index below only "
             "matters when this is on."},
            {"key": "prev_tomo_idx", "field_type": "text", "label": "Preview tomogram index:", "default": "1",
             "help": "Optional, and only used when \"predict a preview\" above is on. STAR row index (or "
             "range, e.g. \"1,2,4\") to auto-predict for the preview."},
            *_SNRFALLOFF_DECONV_OPTIONS,
        ],
        "standard_groups": [
            {"name": "", "fields": ["conda_env", "star_file", "method", "input_column", "gpuID", "ncpus"]},
            {"name": "Network architecture", "fields": ["arch", "cube_size", "pretrained_model"]},
            {"name": "Training", "fields": [
                "epochs", "batch_size", "loss_func", "learning_rate", "learning_rate_min",
                "save_interval", "mixed_precision",
            ]},
            {"name": "Missing-wedge weighting", "fields": ["apply_mw_x1", "mw_weight"]},
            {"name": "CTF handling", "fields": [
                "CTF_mode", "isCTFflipped", "do_phaseflip_input", "bfactor", "clip_first_peak_mode",
            ]},
            {"name": "CTF deconvolution (used alongside CTF handling above)",
             "fields": ["snrfalloff", "deconvstrength", "highpassnyquist"]},
            {"name": "Augmentation", "fields": ["noise_level", "noise_mode", "random_rot_weight"]},
            {"name": "Live preview", "fields": ["with_preview", "prev_tomo_idx"]},
        ],
    },
    "IsonetPredict": {
        "internal_name": "IsonetPredict",
        "subcommand": "predict",
        "label_new": "custom.isonet_predict",
        "display_name": "IsoNet2 – Predict",
        "category": "IsoNet (Beta)",
        "description": "Apply a trained IsoNet2 model to tomograms (missing-wedge correction / denoising)",
        "options": [
            _CONDA_ENV_OPTION,
            _star_input_option(
                "IsonetRefine/job001/tomograms.star",
                "Point this at the SAME star file (or that stage's own tomograms.star copy) used to "
                "train the model below -- typically an IsoNet2 – Refine or IsoNet2 – Denoise job's own "
                "output.",
            ),
            {"key": "model", "field_type": "inputnode", "label": "Trained model checkpoint (required):",
             "default": "", "pattern": "*.pt",
             "help": "Required -- IsoNet2 cannot predict without a trained model. Path to a checkpoint "
             "(.pt) from an IsoNet2 – Refine (Train) or IsoNet2 – Denoise (Train) job's own output "
             "directory, named network_<method>_<arch>_<cube_size>_full.pt -- that filename always "
             "points at the newest checkpoint, so you can queue Predict against a still-running "
             "training job and it will pick up the finished model automatically."},
            _GPU_IDS_OPTION,
            {"key": "input_column", "field_type": "text", "label": "Input STAR column:", "default": "rlnDeconvTomoName",
             "help": "Optional, and CONDITIONAL: only read at all if the loaded model's own method is "
             "\"isonet2\" (single-map). For an isonet2-n2n or plain n2n/Denoise-trained model, the "
             "even/odd half columns are used automatically instead and this field is ignored -- which "
             "method a given .pt uses is saved inside the checkpoint itself, not chosen here."},
            {"key": "apply_mw_x1", "field_type": "boolean", "label": "Apply missing-wedge mask:", "default": True,
             "help": "Optional. Builds and applies the missing-wedge mask to cubic inputs before prediction."},
            {"key": "isCTFflipped", "field_type": "boolean", "label": "Input already phase-flipped:", "default": False,
             "help": "Optional. Declare if input tomograms are already phase-flipped -- keep this "
             "consistent with the same field on the training (Refine/Denoise) job that produced the model."},
            {"key": "padding_factor", "field_type": "slider", "label": "Padding factor:", "default": 1.5,
             "min": 1.0, "max": 4.0, "step": 0.1,
             "help": "Optional. Cubic padding factor used during tiling. Larger padding reduces seams but "
             "increases computation -- match this to the tiling the model was trained with for best results."},
            _TOMO_IDX_OPTION,
            {"key": "output_prefix", "field_type": "text", "label": "Output filename prefix:", "default": "",
             "help": "Optional. Prefix added to predicted MRC filenames."},
            {"key": "save_slices", "field_type": "boolean", "label": "Save preview slices/spectrum:", "default": True,
             "help": "Optional. Saves orthoslice/spectrum preview images alongside each predicted tomogram."},
        ],
        "standard_groups": [
            {"name": "", "fields": ["conda_env", "star_file", "model", "gpuID"]},
            {"name": "CTF / missing-wedge handling", "fields": ["input_column", "apply_mw_x1", "isCTFflipped"]},
            {"name": "Prediction tiling", "fields": ["padding_factor"]},
            {"name": "Output", "fields": ["tomo_idx", "output_prefix", "save_slices"]},
        ],
    },
}
