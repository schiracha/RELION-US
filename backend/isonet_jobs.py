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
    """
    definition = ISONET_JOB_DEFINITIONS[internal_name]
    field_values = field_values or {}
    env = str(field_values.get("conda_env") or "isonet2_environment").strip() or "isonet2_environment"

    subdir_arg = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    parts = [
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


def _star_input_option(default_guess: str, help_extra: str = "") -> dict:
    return {
        "key": "star_file", "field_type": "inputnode", "label": "Input tomograms STAR file:",
        "default": default_guess, "pattern": "*.star",
        "help": f"STAR file listing tomograms and acquisition metadata. {help_extra}".strip(),
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
            # Plain "text", not "filename" -- these are FOLDER paths, and this
            # app's Browse widget (pickFileDialog, frontend/app.js) is a
            # file-only picker (it navigates into folders but only ever
            # selects a file that matches a pattern); there's no folder-only
            # field_type anywhere else in this codebase to reuse, and adding
            # one is out of scope here (see this plan's "no frontend changes"
            # design decision). Typed by hand instead, same as isonet.py's
            # own CLI would take them.
            {"key": "full", "field_type": "text", "label": "Full tomograms folder:", "default": "None",
             "help": "Directory containing full tomogram(s) (.mrc/.rec). Leave as \"None\" if "
             "you're using even/odd halves instead."},
            {"key": "even", "field_type": "text", "label": "Even half-tomograms folder:", "default": "None",
             "help": "Directory containing even half-tomograms, for Noise2Noise training."},
            {"key": "odd", "field_type": "text", "label": "Odd half-tomograms folder:", "default": "None",
             "help": "Directory containing odd half-tomograms, for Noise2Noise training."},
            {"key": "mask_folder", "field_type": "text", "label": "Mask folder (optional):", "default": "None",
             "help": "Directory containing pre-made mask files for the tomograms, if you have them."},
            {"key": "coordinate_folder", "field_type": "text", "label": "Coordinate folder (optional):",
             "default": "None",
             "help": "Directory containing coordinate files for subtomogram extraction, if you have them."},
            {"key": "star_name", "field_type": "text", "label": "Output STAR filename:", "default": "tomograms.star",
             "help": "Name of the generated STAR file, written into this job's output directory."},
            {"key": "pixel_size", "field_type": "text", "label": "Pixel size (Å, or \"auto\"):", "default": "auto",
             "help": "Pixel size in Ångstroms. Leave as \"auto\" to read it from the tomogram headers. Aim for "
             "~10Å/px binned; extreme deviations aren't recommended (target Z resolution is ~30Å)."},
            {"key": "defocus", "field_type": "text", "label": "Defocus (Å, zero-tilt):", "default": "10000",
             "help": "Defocus at zero tilt, in Ångstroms. A single value applies to every tomogram."},
            {"key": "cs", "field_type": "slider", "label": "Spherical aberration Cs (mm):", "default": 2.7,
             "min": 0.0, "max": 10.0, "step": 0.1, "help": "Spherical aberration, in mm."},
            {"key": "voltage", "field_type": "text", "label": "Voltage (kV):", "default": "300",
             "help": "Acceleration voltage, in kV."},
            {"key": "ac", "field_type": "slider", "label": "Amplitude contrast:", "default": 0.1,
             "min": 0.0, "max": 1.0, "step": 0.01, "help": "Amplitude contrast fraction."},
            {"key": "tilt_min", "field_type": "text", "label": "Minimum tilt angle (°):", "default": "-60",
             "help": "Minimum tilt angle in degrees."},
            {"key": "tilt_max", "field_type": "text", "label": "Maximum tilt angle (°):", "default": "60",
             "help": "Maximum tilt angle in degrees."},
            {"key": "create_average", "field_type": "boolean", "label": "Create averaged full tomograms:",
             "default": False, "help": "When even/odd folders are given, also average them into full tomograms."},
            {"key": "number_subtomos", "field_type": "text", "label": "Subtomograms per tomogram:", "default": "auto",
             "help": "Number of subtomograms extracted during training. Leave as \"auto\", or edit per-tomogram "
             "in the generated STAR file afterward."},
        ],
        "standard_groups": [{"name": "", "fields": [
            "conda_env", "full", "even", "odd", "mask_folder", "coordinate_folder", "star_name",
            "pixel_size", "defocus", "cs", "voltage", "ac", "tilt_min", "tilt_max",
            "create_average", "number_subtomos",
        ]}],
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
             "help": "STAR column to read tomogram paths from."},
            *_SNRFALLOFF_DECONV_OPTIONS,
            {"key": "chunk_size", "field_type": "text", "label": "Chunk size (voxels, optional):", "default": "",
             "help": "Process tomograms in cubic chunks of this size, to reduce memory usage on very large "
             "tomograms or limited RAM/VRAM. May create edge artifacts if too small. Leave blank to disable."},
            {"key": "overlap_rate", "field_type": "slider", "label": "Chunk overlap fraction:", "default": 0.25,
             "min": 0.0, "max": 0.9, "step": 0.05,
             "help": "Fractional overlap between adjacent chunks, if chunking is enabled. Larger overlaps reduce "
             "edge artifacts at the cost of extra computation."},
            {"key": "ncpus", "field_type": "text", "label": "CPU workers:", "default": "4",
             "help": "Number of CPU workers for CPU-bound parts of deconvolution."},
            {"key": "phaseflipped", "field_type": "boolean", "label": "Input already phase-flipped:", "default": False,
             "help": "If checked, input is assumed already phase-flipped."},
            _TOMO_IDX_OPTION,
        ],
        "standard_groups": [{"name": "", "fields": [
            "conda_env", "star_file", "input_column", "snrfalloff", "deconvstrength", "highpassnyquist",
            "chunk_size", "overlap_rate", "ncpus", "phaseflipped", "tomo_idx",
        ]}],
    },
    "IsonetMakeMask": {
        "internal_name": "IsonetMakeMask",
        "subcommand": "make_mask",
        "label_new": "custom.isonet_make_mask",
        "display_name": "IsoNet2 – Make Mask",
        "category": "IsoNet (Beta)",
        "description": "Generate sampling masks for tomograms, to prioritize regions of interest during training",
        "options": [
            _CONDA_ENV_OPTION,
            _star_input_option(
                "IsonetDeconv/job001/tomograms.star",
                "Typically the output of an IsoNet2 – CTF Deconvolution job.",
            ),
            {"key": "input_column", "field_type": "text", "label": "Input STAR column:", "default": "rlnDeconvTomoName",
             "help": "STAR column to read tomograms from. Falls back to rlnTomoName, then "
             "rlnTomoReconstructedTomogramHalf1, if absent."},
            {"key": "patch_size", "field_type": "slider", "label": "Local patch size:", "default": 4,
             "min": 1, "max": 32, "step": 1,
             "help": "Local patch size used for max/std local filters. Larger values smooth detection of "
             "specimen regions."},
            {"key": "density_percentage", "field_type": "slider", "label": "Density percentile kept:", "default": 50,
             "min": 0, "max": 100, "step": 1,
             "help": "Percentage of voxels retained by local-density ranking. Lower values create stricter masks."},
            {"key": "std_percentage", "field_type": "slider", "label": "Std-dev percentile kept:", "default": 50,
             "min": 0, "max": 100, "step": 1,
             "help": "Percentage of voxels retained by local-standard-deviation ranking. Lower values emphasize "
             "textured regions."},
            {"key": "z_crop", "field_type": "slider", "label": "Z crop fraction:", "default": 0.2,
             "min": 0.0, "max": 0.9, "step": 0.05,
             "help": "Fraction of tomogram Z cropped from both ends (masks out the top and bottom, each "
             "half this fraction) to avoid sampling low-quality reconstruction edges."},
            _TOMO_IDX_OPTION,
        ],
        "standard_groups": [{"name": "", "fields": [
            "conda_env", "star_file", "input_column", "patch_size", "density_percentage",
            "std_percentage", "z_crop", "tomo_idx",
        ]}],
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
             "help": "Number of CPUs used for data processing."},
            {"key": "arch", "field_type": "radio", "label": "Network architecture:", "default": "unet-medium",
             "options": _ARCH_CHOICES,
             "help": "Determines model capacity and VRAM requirements."},
            {"key": "pretrained_model", "field_type": "filename", "label": "Pretrained model (optional):",
             "default": "", "pattern": "*.pt",
             "help": "Path to a trained model checkpoint to continue training from."},
            {"key": "cube_size", "field_type": "slider", "label": "Training cube size (voxels):", "default": 96,
             "min": 32, "max": 256, "step": 8,
             "help": "Size of training subvolumes. Must be compatible with the network's downsampling factors."},
            {"key": "epochs", "field_type": "slider", "label": "Epochs:", "default": 50,
             "min": 1, "max": 500, "step": 1, "help": "Number of training epochs."},
            {"key": "batch_size", "field_type": "text", "label": "Batch size (or \"auto\"):", "default": "auto",
             "help": "Subtomograms per optimization step. \"auto\" picks GPUs×2 (or 4 for a single GPU)."},
            {"key": "loss_func", "field_type": "radio", "label": "Loss function:", "default": "L2",
             "options": ["L2", "Huber", "L1"], "help": "Training loss function."},
            {"key": "save_interval", "field_type": "slider", "label": "Checkpoint save interval (epochs):",
             "default": 10, "min": 1, "max": 100, "step": 1, "help": "Interval, in epochs, between saved checkpoints."},
            {"key": "learning_rate", "field_type": "text", "label": "Learning rate:", "default": "3e-4",
             "help": "Initial learning rate."},
            {"key": "learning_rate_min", "field_type": "text", "label": "Minimum learning rate:", "default": "3e-4",
             "help": "Minimum learning rate for the scheduler."},
            {"key": "mixed_precision", "field_type": "boolean", "label": "Mixed precision (fp16):", "default": True,
             "help": "Use float16/mixed precision to reduce VRAM and speed up training."},
            {"key": "CTF_mode", "field_type": "radio", "label": "CTF handling mode:", "default": "None",
             "options": ["None", "phase_only", "network", "wiener"],
             "help": "None: no CTF correction. phase_only: phase-only correction. network: CTF-shaped filter on "
             "network input. wiener: Wiener filter on network target."},
            {"key": "isCTFflipped", "field_type": "boolean", "label": "Input already phase-flipped:", "default": False,
             "help": "Whether input tomograms are already phase-flipped."},
            {"key": "do_phaseflip_input", "field_type": "boolean", "label": "Apply phase flip during training:",
             "default": True, "help": "Whether to apply phase flip during training."},
            {"key": "bfactor", "field_type": "slider", "label": "B-factor:", "default": 0,
             "min": 0, "max": 500, "step": 10,
             "help": "B-factor to boost high-frequency content. Recommend 0 for cellular tomograms; 200–300 "
             "for isolated samples."},
            {"key": "clip_first_peak_mode", "field_type": "radio", "label": "Clip first CTF peak mode:", "default": "1",
             "options": ["0", "1", "2", "3"],
             "help": "Attenuates the overrepresented very-low-frequency CTF peak. 0: none, 1: constant clip, "
             "2: negative sine, 3: cosine. 2/3 might increase low-resolution contrast."},
            *_SNRFALLOFF_DECONV_OPTIONS,
            {"key": "with_preview", "field_type": "boolean", "label": "Predict a preview after training:",
             "default": True, "help": "Run prediction with the final checkpoint(s) after training."},
            {"key": "prev_tomo_idx", "field_type": "text", "label": "Preview tomogram index:", "default": "1",
             "help": "STAR row index (or range, e.g. \"1,2,4\") to auto-predict for the preview."},
        ],
        "standard_groups": [{"name": "", "fields": [
            "conda_env", "star_file", "gpuID", "ncpus", "arch", "pretrained_model", "cube_size", "epochs",
            "batch_size", "loss_func", "save_interval", "learning_rate", "learning_rate_min", "mixed_precision",
            "CTF_mode", "isCTFflipped", "do_phaseflip_input", "bfactor", "clip_first_peak_mode",
            "snrfalloff", "deconvstrength", "highpassnyquist", "with_preview", "prev_tomo_idx",
        ]}],
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
                "IsonetDeconv/job001/tomograms.star",
                "Typically the output of an IsoNet2 – CTF Deconvolution job.",
            ),
            _GPU_IDS_OPTION,
            {"key": "ncpus", "field_type": "text", "label": "CPU workers:", "default": "16",
             "help": "Number of CPUs used for data processing."},
            {"key": "method", "field_type": "radio", "label": "Method:", "default": "auto",
             "options": ["auto", "isonet2", "isonet2-n2n"],
             "help": "\"auto\" detects from the STAR file (full tomograms -> isonet2; even/odd halves -> "
             "isonet2-n2n). Set explicitly if both are present."},
            {"key": "arch", "field_type": "radio", "label": "Network architecture:", "default": "unet-medium",
             "options": _ARCH_CHOICES,
             "help": "Determines model capacity and VRAM requirements."},
            {"key": "pretrained_model", "field_type": "filename", "label": "Pretrained model (optional):",
             "default": "", "pattern": "*.pt",
             "help": "Path to a trained model checkpoint to continue training from."},
            {"key": "cube_size", "field_type": "slider", "label": "Training cube size (voxels):", "default": 96,
             "min": 32, "max": 256, "step": 8,
             "help": "Size of training subvolumes. Must be compatible with the network's downsampling factors."},
            {"key": "epochs", "field_type": "slider", "label": "Epochs:", "default": 50,
             "min": 1, "max": 500, "step": 1, "help": "Number of training epochs."},
            {"key": "input_column", "field_type": "text", "label": "Input STAR column:", "default": "rlnDeconvTomoName",
             "help": "STAR column to use as input tomograms."},
            {"key": "batch_size", "field_type": "text", "label": "Batch size (or \"auto\"):", "default": "auto",
             "help": "Subtomograms per optimization step. \"auto\" picks GPUs×2 (or 4 for a single GPU)."},
            {"key": "loss_func", "field_type": "radio", "label": "Loss function:", "default": "L2",
             "options": ["L2", "Huber", "L1"], "help": "Training loss function."},
            {"key": "learning_rate", "field_type": "text", "label": "Learning rate:", "default": "3e-4",
             "help": "Initial learning rate."},
            {"key": "save_interval", "field_type": "slider", "label": "Checkpoint save interval (epochs):",
             "default": 10, "min": 1, "max": 100, "step": 1, "help": "Interval, in epochs, between saved checkpoints."},
            {"key": "learning_rate_min", "field_type": "text", "label": "Minimum learning rate:", "default": "3e-4",
             "help": "Minimum learning rate for the scheduler."},
            {"key": "mw_weight", "field_type": "text", "label": "Missing-wedge loss weight:", "default": "-1",
             "help": "Weight for missing-wedge loss. Higher emphasizes missing-wedge regions more strongly. "
             "-1 disables it (default)."},
            {"key": "apply_mw_x1", "field_type": "boolean", "label": "Apply missing wedge to subtomograms:",
             "default": True, "help": "Whether to apply the missing wedge to subtomograms at the start."},
            {"key": "mixed_precision", "field_type": "boolean", "label": "Mixed precision (fp16):", "default": True,
             "help": "Use float16/mixed precision to reduce VRAM and speed up training."},
            {"key": "CTF_mode", "field_type": "radio", "label": "CTF handling mode:", "default": "None",
             "options": ["None", "phase_only", "network", "wiener"],
             "help": "None: no CTF correction. phase_only: phase-only correction. network: CTF-shaped filter on "
             "network input. wiener: Wiener filter on network target."},
            {"key": "clip_first_peak_mode", "field_type": "radio", "label": "Clip first CTF peak mode:", "default": "1",
             "options": ["0", "1", "2", "3"],
             "help": "Attenuates the overrepresented very-low-frequency CTF peak. 0: none, 1: constant clip, "
             "2: negative sine, 3: cosine. 2/3 might increase low-resolution contrast."},
            {"key": "bfactor", "field_type": "slider", "label": "B-factor:", "default": 0,
             "min": 0, "max": 500, "step": 10,
             "help": "B-factor to boost high-frequency content. Recommend 0 for cellular tomograms; 200–300 "
             "for isolated samples."},
            {"key": "isCTFflipped", "field_type": "boolean", "label": "Input already phase-flipped:", "default": False,
             "help": "Whether input tomograms are already phase-flipped."},
            {"key": "do_phaseflip_input", "field_type": "boolean", "label": "Apply phase flip during training:",
             "default": True, "help": "Whether to apply phase flip during training."},
            {"key": "noise_level", "field_type": "slider", "label": "Synthetic noise level:", "default": 0.0,
             "min": 0.0, "max": 5.0, "step": 0.1, "help": "Adds artificial noise during training."},
            {"key": "noise_mode", "field_type": "radio", "label": "Synthetic noise filter:", "default": "nofilter",
             "options": ["nofilter", "ramp", "hamming"],
             "help": "Filter applied when generating synthetic noise."},
            {"key": "random_rot_weight", "field_type": "slider", "label": "Random rotation augmentation:",
             "default": 0.2, "min": 0.0, "max": 1.0, "step": 0.05,
             "help": "Fraction of training samples that get a random-rotation augmentation."},
            {"key": "with_preview", "field_type": "boolean", "label": "Predict a preview after training:",
             "default": True, "help": "Run prediction with the final checkpoint(s) after training."},
            {"key": "prev_tomo_idx", "field_type": "text", "label": "Preview tomogram index:", "default": "1",
             "help": "STAR row index (or range, e.g. \"1,2,4\") to auto-predict for the preview."},
            *_SNRFALLOFF_DECONV_OPTIONS,
        ],
        "standard_groups": [{"name": "", "fields": [
            "conda_env", "star_file", "gpuID", "ncpus", "method", "arch", "pretrained_model", "cube_size",
            "epochs", "input_column", "batch_size", "loss_func", "learning_rate", "save_interval",
            "learning_rate_min", "mw_weight", "apply_mw_x1", "mixed_precision", "CTF_mode",
            "clip_first_peak_mode", "bfactor", "isCTFflipped", "do_phaseflip_input", "noise_level",
            "noise_mode", "random_rot_weight", "with_preview", "prev_tomo_idx",
            "snrfalloff", "deconvstrength", "highpassnyquist",
        ]}],
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
                "IsonetDeconv/job001/tomograms.star",
                "The same (or equivalent) STAR file used for the Refine/Denoise training job.",
            ),
            {"key": "model", "field_type": "inputnode", "label": "Trained model checkpoint:", "default": "",
             "pattern": "*.pt",
             "help": "Path to a trained model (.pt), typically from an IsoNet2 – Refine (Train) or "
             "IsoNet2 – Denoise (Train) job's output directory. Required."},
            _GPU_IDS_OPTION,
            {"key": "input_column", "field_type": "text", "label": "Input STAR column:", "default": "rlnDeconvTomoName",
             "help": "STAR column used for input tomogram paths (only relevant for an isonet2-method model)."},
            {"key": "apply_mw_x1", "field_type": "boolean", "label": "Apply missing-wedge mask:", "default": True,
             "help": "Build and apply the missing-wedge mask to cubic inputs before prediction."},
            {"key": "isCTFflipped", "field_type": "boolean", "label": "Input already phase-flipped:", "default": False,
             "help": "Declare if input tomograms are already phase-flipped."},
            {"key": "padding_factor", "field_type": "slider", "label": "Padding factor:", "default": 1.5,
             "min": 1.0, "max": 4.0, "step": 0.1,
             "help": "Cubic padding factor used during tiling. Larger padding reduces seams but increases "
             "computation."},
            _TOMO_IDX_OPTION,
            {"key": "output_prefix", "field_type": "text", "label": "Output filename prefix:", "default": "",
             "help": "Prefix added to predicted MRC filenames."},
            {"key": "save_slices", "field_type": "boolean", "label": "Save preview slices/spectrum:", "default": True,
             "help": "Save orthoslice/spectrum preview images alongside each predicted tomogram."},
        ],
        "standard_groups": [{"name": "", "fields": [
            "conda_env", "star_file", "model", "gpuID", "input_column", "apply_mw_x1", "isCTFflipped",
            "padding_factor", "tomo_idx", "output_prefix", "save_slices",
        ]}],
    },
}
