"""
deepetpicker_jobs.py -- wires DeepETPicker (github.com/cbmi-group/DeepETPicker;
Liu et al. 2024, Nat Commun 15:2090, PMID 38453943) into the Jobs list as
three job types, one per `deepetpicker` command: DeepETPickerPrepare,
DeepETPickerTrain, DeepETPickerPredict.

Built the same way as isonet_jobs.py and for the same reason: training and
inference are multi-hour GPU runs, so they go through
job_runner.start_subprocess_job (local or SLURM) with an editable command box,
not through custom_jobs.py's in-process path. The pre-existing
DeepETPickerImport bridge is a different job (converting someone else's
.coords file) and is unchanged.

The command line these build lives in the DeepETPicker repository
(`python -m deepetpicker`, schiracha/DeepETPicker_Sc, see its
docs/PLAN_relion_io_webgui.md). Its contract, which this module relies on:

- every command takes --output_dir <JobDir>/jobNNN/ and writes only inside it
  (RELION-US creates that directory and owns numbering, run.out/run.err and
  the RELION_JOB_EXIT_* markers);
- inputs from an earlier job are plain project-relative paths;
- bad input exits non-zero with a one-line `error: ...` on stderr;
- `train` prints one `epoch=N name=value ...` line per epoch, writes
  metrics.csv, and keeps loss_full.png (train/val loss) up to date so the
  Progress tab can show it exactly as it does for IsoNet2 training.

Flags are rendered `--key value`, space separated, like isonet_jobs.py, and
for the same reason (`conda run` mangles `--key=value` on some conda
versions). Boolean flags are bare switches (`--refine_centroids`), omitted
when false, because DeepETPicker's argparse flags are `store_true`.
"""
from __future__ import annotations

import shlex

_ENV_OPTION = {
    "key": "conda_env", "field_type": "text", "label": "Conda environment name:", "default": "deepetpicker",
    "help": "The conda environment DeepETPicker is installed in (`pip install -e .` from the DeepETPicker "
            "checkout, which provides the `deepetpicker` command). The command is run as "
            "`conda run -n <this> deepetpicker ...`. The environment needs PyTorch and pytorch-lightning "
            "1.1.0 for Train and Predict; Prepare needs neither.",
}

_GPU_OPTION = {
    "key": "gpu", "field_type": "text", "label": "GPU ids:", "default": "0",
    "help": "Comma-separated GPU ids, e.g. \"0\" or \"0,1\". On SLURM, request the same number of GPUs.",
}

_TOMO_INPUTS = [
    {"key": "tomo_star", "field_type": "inputnode", "label": "Tomograms STAR file:", "default": "",
     "pattern": "*.star",
     "help": "RELION tomograms.star (e.g. from a reconstruction job). Give this OR a tomograms folder below, "
             "not both."},
    {"key": "tomo_dir", "field_type": "directory", "label": "OR tomograms folder:", "default": "",
     "help": "Folder of .mrc tomograms, named <tomogram name>.mrc."},
    {"key": "tomo_column", "field_type": "text", "label": "STAR column with tomogram file names:",
     "default": "rlnTomoReconstructedTomogram",
     "help": "Only used with a tomograms STAR file. This default has not been checked against every RELION-5 "
             "release; if it is wrong the job stops and lists the columns the file does have."},
]

DEEPETPICKER_JOB_DEFINITIONS = {
    "DeepETPickerPrepare": {
        "internal_name": "DeepETPickerPrepare",
        "subcommand": "prepare",
        "label_new": "custom.deepetpicker_prepare",
        "display_name": "DeepETPicker – Prepare",
        "category": "DeepETPicker (Beta)",
        "description": "Standardize tomograms and generate weak labels and occupancy maps from known particle "
                       "coordinates, as training data for DeepETPicker",
        "options": [
            _ENV_OPTION,
            *_TOMO_INPUTS,
            {"key": "coords_star", "field_type": "inputnode", "label": "Training particles STAR file:",
             "default": "", "pattern": "*.star",
             "help": "particles.star with rlnTomoName and rlnCoordinateX/Y/Z in pixels (rlnClassNumber is "
                     "the class, default 1). Give this OR a coordinates folder below."},
            {"key": "coords_dir", "field_type": "directory", "label": "OR coordinates folder:", "default": "",
             "help": "Folder of <tomogram name>.coords files, columns `class x y z` in voxels (a bare `x y z` "
                     "file is class 1)."},
            {"key": "coords_scale", "field_type": "text", "label": "Coordinate scale factor:", "default": "1.0",
             "help": "Multiply the coordinates by this to reach the voxel grid of the tomograms above (e.g. "
                     "picks made on a 10.5 Å/px tomogram used with a 6.3 Å/px one: 10.5 / 6.3 = 1.667)."},
            {"key": "num_cls", "field_type": "text", "label": "Number of classes:", "default": "1",
             "help": "Class ids in the coordinates must be 1 .. this number."},
            {"key": "label_type", "field_type": "radio", "label": "Weak-label shape:", "default": "gaussian",
             "options": ["gaussian", "sphere", "cubic"], "help": "Shape of the weak label painted at each particle."},
            {"key": "pixel_size", "field_type": "text", "label": "Tomogram pixel size (Å):", "default": "",
             "help": "Optional. With the particle diameter below, fills in the two diameters that are left "
                     "blank. DeepETPicker's README recommends the particle to span 20-30 voxels; the job warns "
                     "when it does not."},
            {"key": "particle_diameter_A", "field_type": "text", "label": "Particle diameter (Å):", "default": "",
             "help": "Optional, see above."},
            {"key": "label_diameter", "field_type": "text", "label": "Label diameter (voxels, odd):", "default": "",
             "help": "Diameter of the weak label. Usually about half the real particle diameter; must be odd. "
                     "Leave blank to derive it from pixel size and particle diameter."},
            {"key": "ocp_diameter", "field_type": "text", "label": "Particle diameter (voxels, odd):",
             "default": "",
             "help": "Real particle diameter in voxels, odd, one value per class separated by commas. Leave "
                     "blank to derive it from pixel size and particle diameter."},
            {"key": "norm_type", "field_type": "radio", "label": "Normalization:", "default": "standardization",
             "options": ["standardization", "normalization"],
             "help": "standardization: zero mean, unit standard deviation (the default and what the published "
                     "model used). normalization: scale to [0, 1]. Predict applies the same one."},
        ],
        "standard_groups": [
            {"name": "", "fields": ["conda_env"]},
            {"name": "Tomograms", "fields": ["tomo_star", "tomo_dir", "tomo_column"]},
            {"name": "Training coordinates", "fields": ["coords_star", "coords_dir", "coords_scale", "num_cls"]},
            {"name": "Labels", "fields": ["label_type", "pixel_size", "particle_diameter_A",
                                          "label_diameter", "ocp_diameter"]},
            {"name": "Normalization", "fields": ["norm_type"]},
        ],
    },
    "DeepETPickerTrain": {
        "internal_name": "DeepETPickerTrain",
        "subcommand": "train",
        "label_new": "custom.deepetpicker_train",
        "display_name": "DeepETPicker – Train",
        "category": "DeepETPicker (Beta)",
        "description": "Train the DeepETPicker network on the data of a DeepETPicker – Prepare job",
        "options": [
            _ENV_OPTION,
            {"key": "prepare_job", "field_type": "directory", "label": "Prepare job directory (required):",
             "default": "DeepETPickerPrepare/job001",
             "help": "The DeepETPicker – Prepare job to train on. It is only read, never modified."},
            {"key": "val_tomo", "field_type": "text", "label": "Validation tomogram name (required):",
             "default": "",
             "help": "Name of one annotated tomogram (the file name without .mrc) used for validation."},
            {"key": "train_tomos", "field_type": "text", "label": "Training tomogram names:", "default": "",
             "help": "Comma-separated names. Blank: every annotated tomogram except the validation one (with "
                     "a single annotated tomogram it trains and validates on itself)."},
            {"key": "batch_size", "field_type": "slider", "label": "Batch size:", "default": 8,
             "min": 1, "max": 64, "step": 1,
             "help": "Reduce if the GPU runs out of memory."},
            {"key": "patch_size", "field_type": "slider", "label": "Patch size (voxels):", "default": 72,
             "min": 32, "max": 128, "step": 8,
             "help": "Sub-volume size; a multiple of 8, at least 32. Not less than 64 is recommended."},
            {"key": "pad_size", "field_type": "slider", "label": "Padding (voxels):", "default": 12,
             "min": 4, "max": 24, "step": 1,
             "help": "Overlap-tile padding; 6-12 usual. Must be below half the patch size."},
            {"key": "lr", "field_type": "text", "label": "Learning rate:", "default": "0.001",
             "help": "Peak learning rate of the one-cycle schedule."},
            {"key": "max_epochs", "field_type": "slider", "label": "Epochs:", "default": 60,
             "min": 1, "max": 300, "step": 1,
             "help": "60 is usually sufficient."},
            {"key": "seg_thresh", "field_type": "slider", "label": "Segmentation threshold:", "default": 0.5,
             "min": 0.1, "max": 0.9, "step": 0.05,
             "help": "Threshold used for the validation metrics."},
            _GPU_OPTION,
            {"key": "resume", "field_type": "filename", "label": "Resume from checkpoint (optional):",
             "default": "", "pattern": "*.ckpt",
             "help": "Continue training from this checkpoint, e.g. an earlier job's checkpoints/last.ckpt."},
        ],
        "standard_groups": [
            {"name": "", "fields": ["conda_env", "prepare_job", "val_tomo", "train_tomos"]},
            {"name": "Network input", "fields": ["patch_size", "pad_size", "batch_size"]},
            {"name": "Optimisation", "fields": ["lr", "max_epochs", "seg_thresh"]},
            {"name": "Hardware", "fields": ["gpu"]},
            {"name": "Continue", "fields": ["resume"]},
        ],
    },
    "DeepETPickerPredict": {
        "internal_name": "DeepETPickerPredict",
        "subcommand": "predict",
        "label_new": "custom.deepetpicker_predict",
        "display_name": "DeepETPicker – Predict",
        "category": "DeepETPicker (Beta)",
        "description": "Pick particles in tomograms with a trained DeepETPicker model; writes native .coords and "
                       "a RELION particles.star",
        "options": [
            _ENV_OPTION,
            {"key": "train_job", "field_type": "directory", "label": "Train job directory (required):",
             "default": "DeepETPickerTrain/job002",
             "help": "The DeepETPicker – Train job whose model to use (checkpoints/best.ckpt unless a "
                     "checkpoint is given below)."},
            {"key": "model", "field_type": "filename", "label": "Checkpoint (optional):", "default": "",
             "pattern": "*.ckpt", "help": "Use this checkpoint instead of the Train job's best.ckpt."},
            *_TOMO_INPUTS,
            {"key": "already_standardized", "field_type": "boolean",
             "label": "Tomograms already standardized:", "default": False,
             "help": "Tick only if the inputs are the tomograms_std/ of a Prepare job. Otherwise they are "
                     "standardized here with the same method the model was trained with (writes one extra "
                     "copy of each tomogram into this job)."},
            {"key": "patch_size", "field_type": "slider", "label": "Patch size (voxels):", "default": 72,
             "min": 32, "max": 128, "step": 8, "help": "Normally the value used for training."},
            {"key": "pad_size", "field_type": "slider", "label": "Padding (voxels):", "default": 12,
             "min": 4, "max": 24, "step": 1, "help": "Normally the value used for training."},
            {"key": "batch_size", "field_type": "slider", "label": "Batch size:", "default": 8,
             "min": 1, "max": 64, "step": 1, "help": "Reduce if the GPU runs out of memory."},
            {"key": "seg_thresh", "field_type": "slider", "label": "Segmentation threshold:", "default": 0.5,
             "min": 0.1, "max": 0.9, "step": 0.05, "help": "Probability above which a voxel counts as particle."},
            _GPU_OPTION,
            {"key": "refine_centroids", "field_type": "boolean", "label": "Sub-voxel refinement (experimental):",
             "default": False,
             "help": "Move each pick to the centre of mass of the prediction around it, giving fractional "
                     "coordinates. Without this, picks are whole voxels. Not yet validated against ground "
                     "truth."},
            {"key": "relion_scale", "field_type": "text", "label": "Scale to RELION pixels:", "default": "1.0",
             "help": "Multiplies the coordinates written to particles.star: picking pixel size / RELION "
                     "tomogram pixel size (picked on 6.3 Å/px, RELION tomogram at 10.5 Å/px: 0.6). The "
                     "native coords/ files stay on the picking grid."},
        ],
        "standard_groups": [
            {"name": "", "fields": ["conda_env", "train_job", "model"]},
            {"name": "Tomograms", "fields": ["tomo_star", "tomo_dir", "tomo_column", "already_standardized"]},
            {"name": "Network input", "fields": ["patch_size", "pad_size", "batch_size", "seg_thresh"]},
            {"name": "Hardware", "fields": ["gpu"]},
            {"name": "Output", "fields": ["refine_centroids", "relion_scale"]},
        ],
    },
}

# job types whose Progress tab shows loss_full.png (see frontend/app.js ISONET_LOSS_JOB_TYPES)
DEEPETPICKER_LOSS_JOB_TYPES = {"DeepETPickerTrain"}


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _is_default(option: dict, value) -> bool:
    return _blank(value) or str(value) == str(option.get("default"))


def build_deepetpicker_command(internal_name: str, field_values: dict, output_subdir: str) -> str:
    """The full, editable shell command for one DeepETPicker job:
    `conda run -n <env> deepetpicker <subcommand> --output_dir <JobDir>/jobNNN/ ...`.

    Options equal to their declared default (or blank) are left off, as for
    IsoNet2; DeepETPicker records every effective parameter in the job's
    job_params.json regardless.
    """
    definition = DEEPETPICKER_JOB_DEFINITIONS[internal_name]
    field_values = field_values or {}
    env = str(field_values.get("conda_env") or "deepetpicker").strip() or "deepetpicker"
    subdir = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    parts = ["conda", "run", "--no-capture-output", "-n", shlex.quote(env), "deepetpicker",
             definition["subcommand"], "--output_dir", shlex.quote(subdir)]
    for option in definition["options"]:
        key = option["key"]
        if key == "conda_env":
            continue
        value = field_values.get(key, option.get("default"))
        if option["field_type"] == "boolean":
            if value is True or str(value).lower() == "true":
                parts.append(f"--{key}")
            continue
        if _is_default(option, value):
            continue
        parts.extend([f"--{key}", shlex.quote(str(value).strip())])
    return " ".join(parts)


def validate_deepetpicker_fields(internal_name: str, field_values: dict) -> list[str]:
    """Problems that would make the command fail at once, found before the
    job directory is created. The command line re-checks everything; this is
    only so the popup can say so immediately."""
    v = field_values or {}
    has = lambda k: not _blank(v.get(k))  # noqa: E731
    problems = []
    if internal_name == "DeepETPickerPrepare":
        if has("tomo_star") == has("tomo_dir"):
            problems.append("Give exactly one of a tomograms STAR file or a tomograms folder.")
        if has("coords_star") == has("coords_dir"):
            problems.append("Give exactly one of a particles STAR file or a coordinates folder.")
        if not ((has("label_diameter") and has("ocp_diameter")) or (has("pixel_size") and has("particle_diameter_A"))):
            problems.append("Give both diameters (in voxels), or the pixel size and particle diameter (in Å).")
        for k in ("label_diameter",):
            if has(k) and not _odd_int(v[k]):
                problems.append("Label diameter must be an odd whole number of voxels.")
        if has("ocp_diameter") and not all(_odd_int(x) for x in str(v["ocp_diameter"]).split(",")):
            problems.append("Particle diameter must be odd whole numbers of voxels, comma separated per class.")
    elif internal_name == "DeepETPickerTrain":
        if not has("prepare_job"):
            problems.append("A Prepare job directory is required.")
        if not has("val_tomo"):
            problems.append("A validation tomogram name is required.")
    elif internal_name == "DeepETPickerPredict":
        if not has("train_job"):
            problems.append("A Train job directory is required (it supplies the model's settings).")
        if has("tomo_star") == has("tomo_dir"):
            problems.append("Give exactly one of a tomograms STAR file or a tomograms folder.")
    return list(dict.fromkeys(problems))


def _odd_int(x) -> bool:
    try:
        n = int(str(x).strip())
    except ValueError:
        return False
    return n >= 3 and n % 2 == 1
