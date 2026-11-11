"""
aretomo_jobs.py -- wires AreTomo2 (github.com/czimaginginstitute/AreTomo2;
Zheng et al. 2022, J Struct Biol X 6:100068) into the Jobs list as one job
type, AreTomo2: marker-free tilt-series alignment, optional CTF estimation and
dose weighting, and tomogram reconstruction, all from one command line.

Built the same way as isonet_jobs.py and deepetpicker_jobs.py, for the same
reason (a GPU run of minutes to hours that must work locally and on SLURM): it
goes through job_runner.start_subprocess_job with an editable command box, not
through custom_jobs.py's in-process path. This is a different job from the
pre-existing AreTomoImport bridge, which only converts an `.aln` you already
have into IMOD `.xf`/`.tlt`; that one is unchanged and still the way to feed an
alignment into RELION's own IMOD import. (RELION-5's own Align Tilt Series job
can also call AreTomo2 itself, from a tilt-series STAR file; this job is for
when you have plain tilt-series stacks and want AreTomo2's own outputs.)

What the command looks like. AreTomo2 takes ONE stack per run (`-InMrc`) and
writes `<name>.mrc`, `<name>.aln`, the `<name>_Imod/` folder (`-OutImod`) and so
on next to `-OutMrc`. A single stack gives a plain one-line command. A folder
of stacks gives a visible POSIX-sh `for` loop (the job runs under /bin/sh, so
nothing bash-specific) that runs the same line for each stack, carries on past
a failed stack, and exits non-zero at the end if any failed:

    rc=0; for f in tilt_series/*.mrc; do ...; AreTomo -InMrc "$f" -OutMrc "$o" ... \
        && [ -s "$o" ] || rc=1; done; exit $rc

Both forms also test that the output file exists. AreTomo2 exits 0 after
printing "Error: tilt angles cannot be found." (checked on the installed
build), so its exit status alone cannot tell a failed stack from a good one.

Flag facts below were read from AreTomo2's own source (CInput.cpp, the
`AreTomo2 --help` text, CFindCtfMain::CheckInput, CProcessThread.cpp) and its
User Manual 1.1, not remembered:

- tilt angles come from the first float of each extended-header record unless
  `-AngFile` (column 1 angles, optional column 2 acquisition index, which dose
  weighting and the RELION csv need) or `-TiltRange min max` says otherwise;
- CTF estimation runs only when -Kv, -Cs and -PixSize are all non-zero, and
  dose weighting needs -PixSize, -Kv, -ImgDose and the angle file's second
  column;
- `-Align 0` skips alignment (input already aligned); `-AlnFile` loads an
  earlier alignment and implies it;
- `-OutImod` 1/2/3 write the IMOD-style folder for RELION 4+ / Warp / aligned
  stacks; `-VolZ 0` writes the aligned tilt series instead of a volume.

Flags equal to AreTomo2's own default are left off, except the handful that
describe the job (-VolZ, -OutBin, -AlignZ, -Gpu, -OutImod), which are always
written so the command says what it does. Values are `-Flag value`, space
separated; AreTomo2's parser has no `=` form.
"""
from __future__ import annotations

import shlex
from pathlib import Path

_INPUT_EXTENSIONS = ["mrc", "st", "mrcs"]

_ANGLE_HEADER = "MRC extended header"
_ANGLE_RANGE = "Tilt range"
_ANGLE_FILE = "Angle file"

_RECON_SART = "SART"
_RECON_WBP = "WBP"

# Radio options whose value is a one-line explanation led by AreTomo2's own number
# ("1: ..."), because the form's radio control shows the option text itself.
_OUTIMOD_OPTIONS = [
    "0: none",
    "1: RELION 4/5 (original stack + .xf/.tlt/.csv, dark images listed)",
    "2: Warp (dark images removed, ordered by tilt angle)",
    "3: aligned stack (global + local alignment applied, unit .xf)",
]
_TILTCOR_OPTIONS = [
    "0: measure; correct for alignment only",
    "1: measure; correct for alignment and reconstruction",
    "-1: do not measure or correct",
]
_TILTAXIS_REFINE_OPTIONS = [
    "1: refine and let it vary over the series",
    "0: refine, one value for the whole series",
    "-1: use the value as given",
]


def _lead(option_value) -> str:
    """"-1: do not measure" -> "-1"; a bare "1" or 1 -> "1"."""
    return str(option_value).split(":", 1)[0].strip()


DEFAULT_EXE = "AreTomo"

ARETOMO_JOB_DEFINITIONS = {
    "AreTomo2": {
        "internal_name": "AreTomo2",
        "label_new": "custom.aretomo2",
        "display_name": "AreTomo2 – Align & Reconstruct",
        "category": "Tilt Series / Tomogram Reconstruction",
        "description": "Marker-free tilt-series alignment, optional CTF estimation and dose weighting, and "
                       "tomogram reconstruction with AreTomo2 (one stack or a folder of stacks)",
        "options": [
            {"key": "aretomo_exe", "field_type": "text", "label": "AreTomo2 executable:", "default": DEFAULT_EXE,
             "help": "The AreTomo2 command, as found on PATH (here a link named AreTomo pointing at the "
                     "AreTomo2 binary), or a full path to it. It needs an NVIDIA GPU."},

            # ---- input ------------------------------------------------------
            {"key": "tilt_series", "field_type": "filename", "label": "Tilt-series stack:", "default": "",
             "pattern": "*.mrc", "directory": ".",
             "help": "One tilt series as a single MRC stack (all projections in one file). Give this OR a "
                     "folder of stacks below, not both."},
            {"key": "tilt_series_dir", "field_type": "directory", "label": "OR folder of stacks:", "default": "",
             "help": "Folder of tilt-series stacks. Each one is aligned in turn, in the same job, and written "
                     "as <stack name>.mrc plus its own .aln and files."},
            {"key": "input_ext", "field_type": "radio", "label": "Stack extension (folder only):",
             "default": "mrc", "options": _INPUT_EXTENSIONS,
             "help": "Which files in the folder are tilt series."},

            # ---- tilt angles ------------------------------------------------
            {"key": "angle_source", "field_type": "radio", "label": "Tilt angles come from:",
             "default": _ANGLE_HEADER, "options": [_ANGLE_HEADER, _ANGLE_RANGE, _ANGLE_FILE],
             "help": "MRC extended header: the first float of each header record, as written by SerialEM and "
                     "most IMOD stacks. If a stack has none AreTomo2 exits at once. Tilt range: for a "
                     "fixed-step series whose images are sorted by angle. Angle file: a text file with the "
                     "angle in column 1, in stack order, and optionally the 1-based acquisition order in "
                     "column 2 (needed for dose weighting and the RELION .csv)."},
            {"key": "ang_file", "field_type": "filename", "label": "Angle file (single stack):", "default": "",
             "pattern": "*.tlt *.rawtlt *.txt", "directory": ".",
             "help": "Used when the angle source is Angle file and a single stack is given."},
            {"key": "ang_ext", "field_type": "text", "label": "Angle file extension (folder):",
             "default": "rawtlt",
             "help": "Used when the angle source is Angle file and a folder is given: each stack's angles are "
                     "read from <stack name>.<this> in the same folder (IMOD's naming: TS_01.st + "
                     "TS_01.rawtlt)."},
            {"key": "tilt_min", "field_type": "text", "label": "Tilt range minimum (°):", "default": "-60",
             "help": "Used when the angle source is Tilt range."},
            {"key": "tilt_max", "field_type": "text", "label": "Tilt range maximum (°):", "default": "60",
             "help": "Used when the angle source is Tilt range."},

            # ---- alignment --------------------------------------------------
            {"key": "align", "field_type": "boolean", "label": "Run alignment:", "default": True,
             "help": "Untick (-Align 0) to skip alignment: the input must then already be aligned, e.g. to "
                     "reconstruct a stack aligned and CTF-corrected elsewhere."},
            {"key": "aln_file", "field_type": "filename", "label": "Reuse alignment (.aln, single stack):",
             "default": "", "pattern": "*.aln", "directory": ".",
             "help": "Load an earlier alignment instead of computing one (-AlnFile); alignment is then "
                     "skipped. Useful to reconstruct again with other settings. Single stack only."},
            {"key": "align_z", "field_type": "slider", "label": "Alignment thickness (unbinned voxels):",
             "default": 600, "min": 100, "max": 3000, "step": 50,
             "help": "-AlignZ: height of the temporary volume used for projection matching. Aim for about the "
                     "sample thickness and keep it below the reconstruction thickness."},
            {"key": "tilt_axis", "field_type": "text", "label": "Tilt axis (°):", "default": "0",
             "help": "Initial tilt-axis angle, counter-clockwise from the image y axis. 0 lets AreTomo2 "
                     "search the full range; a value you trust is refined within ±3° instead. Try one if "
                     "the automatic result is poor, particularly with large translational shifts."},
            {"key": "tilt_axis_refine", "field_type": "radio", "label": "Tilt axis refinement:",
             "default": _TILTAXIS_REFINE_OPTIONS[0], "options": _TILTAXIS_REFINE_OPTIONS,
             "help": "The second value of -TiltAxis. -1 uses the angle above as given, with no refinement."},
            {"key": "tilt_cor", "field_type": "radio", "label": "Tilt-angle offset:",
             "default": _TILTCOR_OPTIONS[0], "options": _TILTCOR_OPTIONS,
             "help": "-TiltCor. 0 (AreTomo2's default) measures the offset between the recorded and true tilt "
                     "angles and applies it to the alignment only. 1 also applies it to the reconstruction, "
                     "which tilts the tomogram and shifts particle z coordinates, so it is not recommended "
                     "before subtomogram averaging. -1 switches measurement and correction off."},
            {"key": "tilt_offset", "field_type": "text", "label": "Tilt offset to use (°, optional):",
             "default": "",
             "help": "A tilt offset you know: given as the second value of -TiltCor, it replaces the "
                     "measurement. Leave blank to have AreTomo2 measure it."},
            {"key": "patch_x", "field_type": "slider", "label": "Local-alignment patches in x:", "default": 0,
             "min": 0, "max": 20, "step": 1,
             "help": "-Patch: number of patches across the image in which local (beam-induced) motion is "
                     "measured and corrected. 0 turns local alignment off. Typical: 5 and 4."},
            {"key": "patch_y", "field_type": "slider", "label": "Local-alignment patches in y:", "default": 0,
             "min": 0, "max": 20, "step": 1, "help": "See above; both must be non-zero to switch it on."},
            {"key": "roi_file", "field_type": "filename", "label": "Region-of-interest file (optional):",
             "default": "", "pattern": "*.txt", "directory": ".",
             "help": "-RoiFile: two columns x y, one region of interest per line (origin at the image's "
                     "lower-left corner), for focused alignment of an isolated or sparse sample. Single "
                     "stack only."},
            {"key": "crop_x", "field_type": "slider", "label": "Crop volume x (needs ROI file):", "default": 0,
             "min": 0, "max": 20000, "step": 1,
             "help": "-CropVol: crop the volume to this size across the tilt axis, centred on the ROIs. "
                     "Only has an effect together with a ROI file. 0 = no crop."},
            {"key": "crop_y", "field_type": "slider", "label": "Crop volume y (needs ROI file):", "default": 0,
             "min": 0, "max": 20000, "step": 1, "help": "Size along the tilt axis. See above."},
            {"key": "dark_tol", "field_type": "slider", "label": "Dark-image tolerance:", "default": 0.7,
             "min": 0.05, "max": 0.99, "step": 0.01,
             "help": "-DarkTol, in (0, 1): how strictly dark tilt images are removed before alignment. "
                     "Higher is more restrictive."},
            {"key": "bft_global", "field_type": "text", "label": "B-factor, global alignment:", "default": "500",
             "help": "-Bft: B-factor of the low-pass filter used in the cross-correlation."},
            {"key": "bft_local", "field_type": "text", "label": "B-factor, local alignment:", "default": "500",
             "help": "Second value of -Bft."},
            {"key": "intp_cor", "field_type": "boolean", "label": "Interpolation-loss correction:",
             "default": True,
             "help": "-IntpCor: compensate for the information lost to linear interpolation. On by default."},

            # ---- reconstruction ---------------------------------------------
            {"key": "vol_z", "field_type": "slider", "label": "Reconstruction thickness (unbinned voxels):",
             "default": 1200, "min": 0, "max": 6000, "step": 50,
             "help": "-VolZ: z height of the tomogram, in unbinned voxels. Roughly sample thickness plus "
                     "200 voxels. 0 reconstructs nothing and writes the aligned tilt series instead."},
            {"key": "out_bin", "field_type": "slider", "label": "Output binning:", "default": 4,
             "min": 1, "max": 16, "step": 1,
             "help": "-OutBin: binning of the written tomogram (or aligned stack). AreTomo2's own default is "
                     "1; 4 is a more practical start: a 4k x 4k x 1200 tomogram is ~80 GB unbinned. Binning "
                     "needs the pixel size below only for the output header and dose weighting."},
            {"key": "recon_method", "field_type": "radio", "label": "Reconstruction method:",
             "default": _RECON_SART, "options": [_RECON_SART, _RECON_WBP],
             "help": "SART (AreTomo2's default) or weighted back projection (-Wbp 1)."},
            {"key": "sart_iter", "field_type": "slider", "label": "SART iterations:", "default": 20,
             "min": 1, "max": 100, "step": 1, "help": "First value of -Sart."},
            {"key": "sart_proj", "field_type": "slider", "label": "SART projections per update:", "default": 5,
             "min": 1, "max": 50, "step": 1, "help": "Second value of -Sart."},
            {"key": "recon_min", "field_type": "text", "label": "Reconstruct from tilt (°):", "default": "-90",
             "help": "-ReconRange: tilt images outside [min, max] are left out of the reconstruction."},
            {"key": "recon_max", "field_type": "text", "label": "Reconstruct up to tilt (°):", "default": "90",
             "help": "Upper end of -ReconRange."},
            {"key": "flip_vol", "field_type": "boolean", "label": "Save volume as x-y slices (-FlipVol):",
             "default": False,
             "help": "By default the volume is saved as x-z slices, like IMOD's; this saves x-y slices (z "
                     "slowest) instead."},
            {"key": "flip_int", "field_type": "boolean", "label": "Flip contrast (-FlipInt):", "default": False,
             "help": "Make dense structure white instead of dark."},

            # ---- microscope: dose weighting and CTF -------------------------
            {"key": "pix_size", "field_type": "text", "label": "Pixel size of the input (Å):", "default": "",
             "help": "-PixSize. Needed for dose weighting and CTF estimation, and stored in the output "
                     "headers. Leave blank to skip both."},
            {"key": "kv", "field_type": "text", "label": "Voltage (kV):", "default": "",
             "help": "-Kv, e.g. 300. Needed for dose weighting and CTF estimation."},
            {"key": "img_dose", "field_type": "text", "label": "Dose per tilt image (e/Å²):", "default": "",
             "help": "-ImgDose: the dose of one exposure, not the accumulated dose. Dose weighting also needs "
                     "pixel size, voltage and an angle file whose second column is the acquisition order. "
                     "Leave blank for no dose weighting. The output is then dose-weighted, so do not "
                     "reconstruct a dose-weighted aligned stack again with dose weighting on."},
            {"key": "cs", "field_type": "text", "label": "Spherical aberration (mm):", "default": "",
             "help": "-Cs, e.g. 2.7. CTF estimation runs only when pixel size, voltage and this are all "
                     "given; it writes <name>_ctf.mrc (spectra) and a CTF text file."},
            {"key": "amp_contrast", "field_type": "text", "label": "Amplitude contrast:", "default": "0.07",
             "help": "-AmpContrast, for CTF estimation."},
            {"key": "ext_phase_guess", "field_type": "text", "label": "Phase-shift guess (°, optional):",
             "default": "",
             "help": "First value of -ExtPhase: guess of the phase shift of a phase plate. Leave blank "
                     "without a phase plate."},
            {"key": "ext_phase_range", "field_type": "text", "label": "Phase-shift search range (°):",
             "default": "0", "help": "Second value of -ExtPhase. Only used when a guess is given."},

            # ---- output / hardware -----------------------------------------
            {"key": "out_imod", "field_type": "radio", "label": "Write IMOD-style files:",
             "default": _OUTIMOD_OPTIONS[1], "options": _OUTIMOD_OPTIONS,
             "help": "-OutImod: a <name>_Imod/ folder next to the output with .xf, .tlt, .csv, newst.com and "
                     "tilt.com, to bootstrap subtomogram averaging. 1 suits RELION 4/5, 2 suits Warp, 3 "
                     "writes the aligned stack (local alignment included) with unit transforms. Note: with "
                     "the AreTomo2 build tested here (1.1.x, from git), 2 and 3 crashed with a segmentation "
                     "fault on a synthetic series while 0 and 1 ran fine; if yours does too, use 1. The "
                     "Import from AreTomo2 (.aln) job does the .xf/.tlt step for you if you only have the "
                     ".aln."},
            {"key": "gpu", "field_type": "text", "label": "GPU ids:", "default": "0",
             "help": "-Gpu: one or more GPU ids separated by spaces or commas, e.g. \"0\" or \"0 1\". Several "
                     "GPUs share the local-alignment patches. On SLURM, request the same number of GPUs."},
        ],
        "standard_groups": [
            {"name": "", "fields": ["aretomo_exe"]},
            {"name": "Tilt series", "fields": ["tilt_series", "tilt_series_dir", "input_ext"]},
            {"name": "Tilt angles", "fields": ["angle_source", "ang_file", "ang_ext", "tilt_min", "tilt_max"]},
            {"name": "Alignment", "fields": ["align", "aln_file", "align_z", "tilt_axis", "tilt_axis_refine",
                                             "tilt_cor", "tilt_offset", "dark_tol", "bft_global", "bft_local",
                                             "intp_cor"]},
            {"name": "Local alignment", "fields": ["patch_x", "patch_y", "roi_file", "crop_x", "crop_y"]},
            {"name": "Reconstruction", "fields": ["vol_z", "out_bin", "recon_method", "sart_iter", "sart_proj",
                                                  "recon_min", "recon_max", "flip_vol", "flip_int"]},
            {"name": "Dose weighting and CTF", "fields": ["pix_size", "kv", "img_dose", "cs", "amp_contrast",
                                                          "ext_phase_guess", "ext_phase_range"]},
            {"name": "Output and hardware", "fields": ["out_imod", "gpu"]},
        ],
    },
}

_BY_KEY = {o["key"]: o for o in ARETOMO_JOB_DEFINITIONS["AreTomo2"]["options"]}

PLACEHOLDER_STACK = "TILT_SERIES.mrc"


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _text(values: dict, key: str) -> str:
    value = values.get(key, _BY_KEY[key].get("default", ""))
    return "" if value is None else str(value).strip()


def _number(values: dict, key: str):
    """The field as a float, or None when blank or not a number."""
    raw = _text(values, key)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _fmt(x: float) -> str:
    return f"{x:g}"


def _true(values: dict, key: str) -> bool:
    value = values.get(key, _BY_KEY[key].get("default"))
    return value is True or str(value).lower() == "true"


def _same(a, b) -> bool:
    """Equal as numbers where both are numbers ("0" == 0.0 == "0.00"), else as text."""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _stem(path: str) -> str:
    """Name of the output files for one stack: AreTomo2 names its .aln, _Imod
    folder, _ctf.mrc and so on after -OutMrc. IMOD stacks are often .st or
    .mrc.st; only the last extension goes, as the loop's ${b%.*} does."""
    name = Path(path).name
    return name.rsplit(".", 1)[0] if "." in name else name


def _gpu_ids(values: dict) -> list:
    return [g for g in _text(values, "gpu").replace(",", " ").split() if g] or ["0"]


def _common_flags(values: dict, single: bool) -> list:
    """Every flag except -InMrc/-OutMrc/-AngFile, which differ per stack."""
    v = values
    flags: list = []

    # always written: the settings that define the job
    flags += ["-VolZ", str(int(_number(v, "vol_z") if _number(v, "vol_z") is not None else 1200))]
    flags += ["-OutBin", _fmt(_number(v, "out_bin") if _number(v, "out_bin") is not None else 4)]
    flags += ["-AlignZ", str(int(_number(v, "align_z") if _number(v, "align_z") is not None else 600))]
    flags += ["-Gpu", *_gpu_ids(v)]
    flags += ["-OutImod", _lead(_text(v, "out_imod") or _OUTIMOD_OPTIONS[1])]

    if _text(v, "angle_source") == _ANGLE_RANGE:
        flags += ["-TiltRange", _text(v, "tilt_min"), _text(v, "tilt_max")]

    if not _true(v, "align") and not (single and _text(v, "aln_file")):
        flags += ["-Align", "0"]
    if single and _text(v, "aln_file"):
        flags += ["-AlnFile", shlex.quote(_text(v, "aln_file"))]

    refine = _lead(_text(v, "tilt_axis_refine") or "1")
    axis = _text(v, "tilt_axis") or "0"
    if not _same(axis, 0) or refine != "1":
        flags += ["-TiltAxis", axis, refine]

    cor = _lead(_text(v, "tilt_cor") or "0")
    offset = _text(v, "tilt_offset")
    if offset:
        flags += ["-TiltCor", cor, offset]
    elif cor != "0":
        flags += ["-TiltCor", cor]

    px, py = _number(v, "patch_x") or 0, _number(v, "patch_y") or 0
    if px or py:
        flags += ["-Patch", str(int(px)), str(int(py))]
    if single and _text(v, "roi_file"):
        flags += ["-RoiFile", shlex.quote(_text(v, "roi_file"))]
        cx, cy = _number(v, "crop_x") or 0, _number(v, "crop_y") or 0
        if cx or cy:
            flags += ["-CropVol", str(int(cx)), str(int(cy))]

    dark = _text(v, "dark_tol")
    if dark and not _same(dark, 0.7):
        flags += ["-DarkTol", dark]
    bg, bl = _text(v, "bft_global") or "500", _text(v, "bft_local") or "500"
    if not (_same(bg, 500) and _same(bl, 500)):
        flags += ["-Bft", bg, bl]
    if not _true(v, "intp_cor"):
        flags += ["-IntpCor", "0"]

    if _text(v, "recon_method") == _RECON_WBP:
        flags += ["-Wbp", "1"]
    else:
        it, pr = _text(v, "sart_iter") or "20", _text(v, "sart_proj") or "5"
        if not (_same(it, 20) and _same(pr, 5)):
            flags += ["-Sart", it, pr]
    lo, hi = _text(v, "recon_min") or "-90", _text(v, "recon_max") or "90"
    if not (_same(lo, -90) and _same(hi, 90)):
        flags += ["-ReconRange", lo, hi]
    if _true(v, "flip_vol"):
        flags += ["-FlipVol", "1"]
    if _true(v, "flip_int"):
        flags += ["-FlipInt", "1"]

    for key, flag in (("pix_size", "-PixSize"), ("kv", "-Kv"), ("img_dose", "-ImgDose"), ("cs", "-Cs")):
        if _text(v, key):
            flags += [flag, _text(v, key)]
    amp = _text(v, "amp_contrast")
    if amp and not _same(amp, 0.07):
        flags += ["-AmpContrast", amp]
    if _text(v, "ext_phase_guess"):
        flags += ["-ExtPhase", _text(v, "ext_phase_guess"), _text(v, "ext_phase_range") or "0"]
    return flags


def build_aretomo_command(internal_name: str, field_values: dict, output_subdir: str,
                          project_dir=None) -> str:
    """The full, editable shell command for one AreTomo2 job.

    A single stack: `AreTomo -InMrc <stack> -OutMrc <JobDir>/jobNNN/<stack name>.mrc ...`.
    A folder of stacks: a `for` loop running that for each (see the module
    docstring). project_dir is accepted for symmetry with the other external
    jobs and is not needed."""
    v = field_values or {}
    exe = _text(v, "aretomo_exe") or DEFAULT_EXE
    subdir = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
    folder = _text(v, "tilt_series_dir")
    use_file = _text(v, "angle_source") == _ANGLE_FILE

    if folder and not _text(v, "tilt_series"):
        ext = _text(v, "input_ext") or "mrc"
        flags = " ".join(_common_flags(v, single=False))
        angle = f' -AngFile "${{f%.*}}.{_text(v, "ang_ext") or "rawtlt"}"' if use_file else ""
        qfolder = shlex.quote(folder.rstrip("/") or "/")
        return (
            f'rc=0; for f in {qfolder}/*.{ext}; do '
            f'[ -e "$f" ] || {{ echo "no tilt series match: $f" >&2; rc=1; continue; }}; '
            f'b=$(basename "$f"); b="${{b%.*}}"; o={shlex.quote(subdir)}"$b".mrc; echo "=== AreTomo2: $b ==="; '
            f'{shlex.quote(exe)} -InMrc "$f" -OutMrc "$o"{angle} {flags}'
            f' && [ -s "$o" ] || {{ echo "AreTomo2 failed or wrote no output for $f" >&2; rc=1; }}; done; exit $rc'
        )

    stack = _text(v, "tilt_series") or PLACEHOLDER_STACK
    parts = [shlex.quote(exe), "-InMrc", shlex.quote(stack),
             "-OutMrc", shlex.quote(f"{subdir}{_stem(stack)}.mrc")]
    if use_file and _text(v, "ang_file"):
        parts += ["-AngFile", shlex.quote(_text(v, "ang_file"))]
    parts += _common_flags(v, single=True)
    out = shlex.quote(f"{subdir}{_stem(stack)}.mrc")
    return (" ".join(parts) + f" && [ -s {out} ]"
            f' || {{ echo "AreTomo2 failed or wrote no output: {out}" >&2; exit 1; }}')


def validate_aretomo_fields(internal_name: str, field_values: dict, project_dir=None) -> list[str]:
    """Problems that would make the command fail at once or silently do the
    wrong thing, found before the job directory is created. AreTomo2's parser
    swallows many bad values without complaint, so this is stricter than it."""
    v = field_values or {}
    has = lambda k: not _blank(v.get(k))  # noqa: E731
    problems = []

    if has("tilt_series") == has("tilt_series_dir"):
        problems.append("Give exactly one of a tilt-series stack or a folder of stacks.")
    folder = has("tilt_series_dir")

    source = _text(v, "angle_source")
    if source == _ANGLE_FILE:
        if folder and not _text(v, "ang_ext"):
            problems.append("Give the extension of the per-stack angle files (e.g. rawtlt).")
        if not folder and has("tilt_series") and not has("ang_file"):
            problems.append("Angle source is Angle file but no angle file is given.")
    elif source == _ANGLE_RANGE:
        lo, hi = _number(v, "tilt_min"), _number(v, "tilt_max")
        if lo is None or hi is None:
            problems.append("The tilt range needs a minimum and a maximum angle, as numbers.")
        elif lo >= hi:
            problems.append("The tilt range minimum must be below its maximum.")

    if folder and (has("aln_file") or has("roi_file")):
        problems.append("A reused alignment (.aln) and a region-of-interest file belong to one tilt series: "
                        "use them with a single stack, not a folder.")
    if has("crop_x") or has("crop_y"):
        if (_number(v, "crop_x") or 0) or (_number(v, "crop_y") or 0):
            if not has("roi_file"):
                problems.append("Volume cropping only works together with a region-of-interest file.")
    if not _true(v, "align") and has("aln_file"):
        problems.append("Run alignment is off but an alignment file is given: pick one (a given .aln already "
                        "skips alignment).")

    for key, label in (("vol_z", "Reconstruction thickness"), ("align_z", "Alignment thickness"),
                       ("out_bin", "Output binning"), ("pix_size", "Pixel size"), ("kv", "Voltage"),
                       ("img_dose", "Dose per image"), ("cs", "Spherical aberration"),
                       ("tilt_axis", "Tilt axis"), ("tilt_offset", "Tilt offset"),
                       ("recon_min", "Reconstruction minimum tilt"), ("recon_max", "Reconstruction maximum tilt"),
                       ("bft_global", "Global B-factor"), ("bft_local", "Local B-factor"),
                       ("amp_contrast", "Amplitude contrast"), ("ext_phase_guess", "Phase-shift guess"),
                       ("ext_phase_range", "Phase-shift range"), ("dark_tol", "Dark-image tolerance")):
        if has(key) and _number(v, key) is None:
            problems.append(f"{label} must be a number.")

    for key, label in (("vol_z", "Reconstruction thickness"), ("pix_size", "Pixel size"),
                       ("img_dose", "Dose per image"), ("kv", "Voltage"), ("cs", "Spherical aberration")):
        n = _number(v, key)
        if n is not None and n < 0:
            problems.append(f"{label} cannot be negative.")
    vz, az = _number(v, "vol_z"), _number(v, "align_z")
    if vz and az and az > vz:
        problems.append("Alignment thickness must not exceed the reconstruction thickness.")
    ob = _number(v, "out_bin")
    if ob is not None and ob < 1:
        problems.append("Output binning must be at least 1.")
    dt = _number(v, "dark_tol")
    if dt is not None and not 0 < dt < 1:
        problems.append("Dark-image tolerance must be between 0 and 1.")
    lo, hi = _number(v, "recon_min"), _number(v, "recon_max")
    if lo is not None and hi is not None and lo >= hi:
        problems.append("The reconstruction tilt range minimum must be below its maximum.")

    if has("img_dose") and (_number(v, "img_dose") or 0) > 0:
        missing = [name for key, name in (("pix_size", "pixel size"), ("kv", "voltage")) if not has(key)]
        if missing:
            problems.append("Dose weighting needs the " + " and ".join(missing) + " as well as the dose per image.")
        if source != _ANGLE_FILE:
            problems.append("Dose weighting needs the tilt angles from an angle file, whose second column is "
                            "the acquisition order.")
    if has("cs") and not (has("pix_size") and has("kv")):
        problems.append("CTF estimation needs the pixel size and voltage as well as Cs.")

    gpus = _text(v, "gpu").replace(",", " ").split()
    if not gpus or not all(g.isdigit() for g in gpus):
        problems.append("GPU ids must be whole numbers, e.g. 0 or 0 1.")
    ext = _text(v, "ang_ext")
    if ext and not ext.replace("_", "").replace("-", "").isalnum():
        problems.append("The angle file extension may only contain letters, digits, - and _.")
    return list(dict.fromkeys(problems))
