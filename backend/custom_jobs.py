"""
custom_jobs.py — wires the IMOD / Warp-M / DeepETPicker / AreTomo2 import bridges
(backend/converters/, built earlier in this project and unit-tested there)
into this app's Jobs list as four more job types, run the same way as
every RELION job: a popup with an Inputs tab (standard inputs, plus an
Advanced section for options the GUI never shows), live output, and an
Alerts tab.

These don't spawn a relion_* subprocess — they call directly into
converters/ in a worker thread (via asyncio.to_thread, since pandas/
starfile calls are synchronous) and report a text summary as their "live
output". See job_runner.JobRunManager.start_custom_job.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import exclude_tilts
import job_registry
import manual_pick
import viz
from converters import aretomo_bridge, deepetpicker_bridge, imod_bridge, warp_bridge
from converters.star_io import backup_before_overwrite, write_particles


def _resolve_in(project_dir: Path, value: str) -> str:
    """Resolve a user-supplied INPUT path against the project directory when
    it's relative — matching RELION, where paths are project-root-relative.
    Absolute paths and empty values pass through unchanged."""
    if not value:
        return value
    p = Path(value)
    return str(p if p.is_absolute() else project_dir / p)


def _resolve_out(job_dir: Path, value: str, default: str) -> Path:
    """Resolve an OUTPUT path against this job's own output directory, the
    way a RELION job writes into its `--o <JobDir>/jobNNN/`. That keeps the
    Outputs tab, Clean and Delete (which all operate on the job dir) honest,
    and stops successive imports from silently overwriting one shared
    `particles.star` at the project root. An absolute path is still honoured
    verbatim, for the rare case someone wants output elsewhere."""
    raw = (value or "").strip() or default
    p = Path(raw)
    return p if p.is_absolute() else job_dir / p


def _opt_float(value) -> float | None:
    """Parse an optional numeric field (blank -> None)."""
    if value is None or str(value).strip() == "":
        return None
    return float(value)


# "Re-reference to a re-reconstructed tomogram" -- shared by every importer
# that writes voxel rlnCoordinateX/Y/Z (see converters/rereference.py for the
# geometry). All optional; leaving the tomograms STAR blank skips the step.
REREF_GROUP_NAME = "Re-reference to a different reconstruction (optional)"
REREF_OPTIONS = [
    {"key": "target_tomograms_star", "field_type": "filename", "label": "Tomograms STAR to re-reference to:", "default": "", "pattern": "*.star", "directory": ".", "help": "The tomograms.star of the reconstruction these picks should END UP on (e.g. Tomograms/job018/tomograms.star). Its size and binning for the tomogram named above are read from rlnTomoSizeX/Y/Z and rlnTomoTomogramBinning. Leave blank to skip. With only this set (no source fields) nothing moves and you just get an out-of-bounds check against that tomogram."},
    {"key": "src_size_x", "field_type": "text", "label": "Source volume X size (unbinned px):", "default": "", "help": "Size of the volume the picks were made on, in UNBINNED tilt-series pixels (RELION's own 'Unbinned tomogram width'; for a volume stored at binning b, voxels x b). Blank = same as the target."},
    {"key": "src_size_y", "field_type": "text", "label": "Source volume Y size (unbinned px):", "default": "", "help": "As above, for Y. Blank = same as the target."},
    {"key": "src_size_z", "field_type": "text", "label": "Source volume Z size / thickness (unbinned px):", "default": "", "help": "As above, for Z -- e.g. 2000 if the picks were made on a 2000 px thick reconstruction that you have since redone at 1000. Blank = same as the target."},
    {"key": "src_binning", "field_type": "text", "label": "Source volume binning:", "default": "", "help": "Binning of the volume the picks were made on (RELION's 'Binning for tomogram'). Blank = same as the target. When this differs, the coordinates are also scaled by source/target binning, so leave the binning/scale factor above at 1."},
    {"key": "drop_outside", "field_type": "boolean", "label": "Drop picks outside the new volume:", "default": True, "help": "Picks that fall outside the target volume (e.g. in a slab you cropped away by reducing the thickness) are removed and written to out_of_bounds.star in this job's directory. Unchecked, they are kept with their out-of-range coordinates. They are never squeezed back in."},
]
REREF_FIELD_KEYS = [o["key"] for o in REREF_OPTIONS]


def _apply_rereference(project_dir: Path, values: dict, df, tomo_name: str,
                       scale_value, scale_label: str, job_dir: Path):
    """Re-reference `df` (voxel coordinates) onto the target reconstruction
    when the user asked for it. Returns (df, summary_lines); a no-op
    returning (df, []) when no target tomograms STAR was given."""
    from converters import rereference

    star = (values.get("target_tomograms_star") or "").strip()
    if not star:
        return df, []
    if float(scale_value) != 1.0:
        raise ValueError(
            f"{scale_label} is {scale_value} but a target tomograms STAR is set: "
            f"the re-reference step already scales by source/target binning, so "
            f"leave {scale_label} at 1 (otherwise the coordinates would be scaled twice)."
        )
    dst = rereference.read_target_geometry(_resolve_in(project_dir, star), tomo_name)
    src = rereference.resolve_source_geometry(
        dst,
        _opt_float(values.get("src_size_x")), _opt_float(values.get("src_size_y")),
        _opt_float(values.get("src_size_z")), _opt_float(values.get("src_binning")),
    )
    drop = bool(values.get("drop_outside", True))
    kept, outside = rereference.rereference_coordinates(df, src, dst, drop_outside=drop)
    scales, shifts = rereference.axis_scale_and_shift(src, dst)

    lines = [
        f"Re-referenced {len(df)} picks: source {src.size_unbinned} unbinned px @ binning "
        f"{src.binning:g} -> target {dst.size_unbinned} @ {dst.binning:g} "
        f"({'/'.join('%.2f' % v for v in dst.size_voxels)} voxels).",
        f"Scale {scales[0]:.4f}; shift (X, Y, Z) = "
        f"({shifts[0]:+.2f}, {shifts[1]:+.2f}, {shifts[2]:+.2f}) voxels.",
    ]
    if src == dst:
        lines.append("Source geometry equals the target: no coordinates moved (out-of-bounds check only).")
    if len(outside):
        if drop:
            if kept.empty:
                raise ValueError(
                    f"All {len(df)} picks fall outside the target volume "
                    f"{'/'.join('%.0f' % v for v in dst.size_voxels)} voxels -- the source "
                    f"geometry is probably wrong (nothing written)."
                )
            import starfile
            job_dir.mkdir(parents=True, exist_ok=True)
            oob_path = job_dir / "out_of_bounds.star"
            starfile.write({"particles": outside}, oob_path, overwrite=True)
            lines.append(
                f"Dropped {len(outside)} of {len(df)} picks outside the target volume "
                f"(kept {len(kept)}); they are in {oob_path}."
            )
        else:
            lines.append(f"{len(outside)} of {len(df)} picks lie outside the target volume (kept, as requested).")
    else:
        lines.append(f"All {len(df)} picks are inside the target volume.")
    return kept, lines

# Field definitions for the custom jobs, in the same shape
# job_registry.build_job_definition() produces for real RELION jobs, so the
# frontend renders them identically. All fields are "standard" (no advanced
# tab needed — these are already small, focused jobs).


def _picker_definition(internal_name: str, real_job_name: str, label_new: str,
                        display_name: str, description: str, picker_kind: str,
                        category: str = "Particle Picking",
                        action_desc: str = "picking happens",
                        impl_module: str = "backend/manual_pick.py") -> dict:
    """Manualpick/TomoManualPick/TomoExcludeTiltImages all reuse the REAL
    RELION option list/layout (job_registry.raw_job(real_job_name), still
    present in data/job_definitions_raw.json even though job_catalog.
    JOB_CATALOG no longer lists any of them as a real subprocess job -- see
    job_catalog.py's CUSTOM_JOBS docstring) rather than hand-picking a
    subset. That keeps the Inputs tab identical to what real RELION's own
    GUI would show for the same job type, which is what makes the job.star
    this ultimately registers (see run_manual_pick/run_tomo_manual_pick/
    run_exclude_tilt_images below) a genuinely realistic one -- every field
    a real relion_manualpick/relion_python_tomo_pick/relion_tomo_exclude_
    tilt_images job would have, not just the handful this app's own in-
    browser UI actually uses.

    picker_kind ("spa" | "tomo" | "excludetilts") tags the popup so app.js
    knows to show the Picker button/embedded UI instead of (custom jobs')
    plain Run button, which in-browser UI to open (the orthoslice picker
    for spa/tomo, the tilt-image reviewer for excludetilts), and which
    input field names/API routes to call for it.
    """
    raw = job_registry.raw_job(real_job_name)
    return {
        "internal_name": internal_name,
        "label_new": label_new,
        "display_name": display_name,
        "category": category,
        "description": description,
        "options": raw.get("options", []),
        "standard_groups": job_registry._standard_groups(raw),
        # default_values is NOT set here -- main.py's _custom_job_definition()
        # always derives it fresh from each option's own "default" for every
        # custom job (the single source of truth for all of them, not just
        # these two), so anything set here would be dead and misleadingly
        # imply otherwise.
        "program_guess": (
            f"(no subprocess -- see {impl_module}; {action_desc} in "
            f"the in-browser viewer, not {raw.get('program_guess', 'the real binary')})"
        ),
        "flags_used": [],
        "commands_source": raw.get("commands_source", ""),
        "is_custom": True,
        "is_picker": True,
        "picker_kind": picker_kind,
    }


def _pick_filter_definition() -> dict:
    """Filter Picks: RELION's Select job in its select-on-value mode, with an
    interactive tuner. The four fields it shares with the real job (the input
    star, label, min, max) take their definitions from RELION's own Select job."""
    raw = job_registry.raw_job("Select")
    real = {o["key"]: o for o in raw.get("options", [])}
    options = [
        {**real["fn_data"], "label": "Particles / picks STAR file (required):", "pattern": "*.star",
         "help": "The particles.star (or a picks STAR) to filter: tomography (rlnTomoName) or single-particle "
                 "(rlnMicrographName). Any numeric column of it can be used to exclude picks."},
        {"key": "fn_preview", "field_type": "inputnode", "label": "Tomograms STAR (for the preview):", "default": "",
         "pattern": "*.star",
         "help": "Optional, tomography only: the tomograms.star that says which volume each rlnTomoName is, so the "
                 "tuner can show a tomogram with its picks. Not needed for single-particle picks (their "
                 "micrographs are named in the picks file). The counts, histogram and selection work without it."},
        {**real["select_label"], "default": "rlnAutopickFigureOfMerit",
         "help": real["select_label"]["help"] + " The tuner sets this when you apply a selection. Starts as "
                 "RELION's pick score (rlnAutopickFigureOfMerit); if the file has no such column the first "
                 "numeric column is used."},
        {**real["select_minval"], "help": real["select_minval"]["help"] + " Set by the tuner."},
        {**real["select_maxval"], "help": real["select_maxval"]["help"] + " Set by the tuner."},
    ]
    return {
        "internal_name": "PickFilter",
        "label_new": "relion.select.onvalue",
        "display_name": "Filter Picks (interactive)",
        "category": "Classification & Refinement",
        "description": "Tune which picks to keep by a numeric column, with a live histogram and an image preview",
        "options": options,
        "standard_groups": [
            {"name": "Input", "fields": ["fn_data", "fn_preview"]},
            {"name": "Selection (set by the tuner)", "fields": ["select_label", "select_minval", "select_maxval"]},
        ],
        "program_guess": "(no subprocess to start -- see backend/pick_filter.py; the selection is written with "
                         "RELION's own relion_star_handler --select, as the Select job's 'select based on "
                         "metadata values' branch does)",
        "flags_used": [],
        "commands_source": raw.get("commands_source", ""),
        "is_custom": True,
        "is_picker": True,
        "picker_kind": "pickfilter",
    }


CUSTOM_JOB_DEFINITIONS = {
    "PickFilter": _pick_filter_definition(),
    "Manualpick": _picker_definition(
        "Manualpick", "Manualpick", "relion.manualpick", "Manual Picking",
        "Manually pick particle coordinates from micrographs", "spa",
    ),
    "TomoManualPick": _picker_definition(
        "TomoManualPick", "TomoPickTomograms", "relion.picktomo", "Manual Picking (Tomo)",
        "Manually pick particles in tomograms", "tomo",
    ),
    "TomoExcludeTiltImages": _picker_definition(
        "TomoExcludeTiltImages", "TomoExcludeTiltImages", "relion.excludetilts",
        "Exclude Tilt Images", "Exclusion of bad tilt-images from tilt-series", "excludetilts",
        category="Tilt Series / Tomogram Reconstruction",
        action_desc="tilt-image review/exclusion happens",
        impl_module="backend/exclude_tilts.py",
    ),
    "ImodImport": {
        "internal_name": "ImodImport",
        "label_new": "custom.imod_import",
        "display_name": "Import from IMOD (.mod)",
        "category": "Import & Conversion",
        "description": "Convert an IMOD .mod point model into a RELION particles.star",
        "options": [
            {"key": "mod_path", "field_type": "filename", "label": "IMOD .mod file:", "default": "", "pattern": "*.mod", "directory": ".", "help": "Path to the IMOD scattered-point model to convert. Requires IMOD's own model2point on PATH (module load imod). AXIS CAVEAT: coordinates are copied X,Y,Z verbatim — if the .mod was drawn on a 'flipped' (trimvol -yz) or raw-tilt tomogram, its depth axis is Y, not Z, so Y and Z will be swapped relative to a RELION tomogram (which expects the 'rotated'/trimvol -rx orientation). Confirm your tomogram orientation before trusting Z."},
            {"key": "tomo_name", "field_type": "text", "label": "Tomogram name (rlnTomoName):", "default": "", "help": "Value to write into rlnTomoName for every particle from this .mod file."},
            {"key": "scale", "field_type": "slider", "label": "Coordinate scale factor:", "default": 1.0, "min": 0.01, "max": 10.0, "step": 0.01, "help": "Multiply IMOD model coordinates by this factor (e.g. to correct for a binning mismatch between the .mod and the RELION tomogram). IMOD model coords are 0-based pixels; Z carries IMOD's -0.5 half-pixel offset."},
            {"key": "swap_yz", "field_type": "boolean", "label": "Swap Y and Z:", "default": False, "help": "Swap the Y and Z coordinates. Use this when the .mod was built on a 'flipped' (trimvol -yz) or raw-tilt tomogram whose depth axis is Y, to move into RELION's depth-in-Z convention (see the .mod field note above). Handedness-changing."},
            {"key": "flip_x", "field_type": "boolean", "label": "Mirror X (needs X size):", "default": False, "help": "Mirror the X axis about the tomogram centre: x -> (X size - 1) - x, for 0-based coordinates. Requires the tomogram X dimension below."},
            {"key": "flip_y", "field_type": "boolean", "label": "Mirror Y (needs Y size):", "default": False, "help": "Mirror the Y axis about the tomogram centre: y -> (Y size - 1) - y, for 0-based coordinates. Requires the tomogram Y dimension below."},
            {"key": "flip_z", "field_type": "boolean", "label": "Mirror Z (needs Z size):", "default": False, "help": "Mirror the Z axis about the tomogram centre: z -> (Z size - 1) - z, for 0-based coordinates. Requires the tomogram Z dimension below."},
            {"key": "tomo_size_x", "field_type": "text", "label": "Tomogram X size (px, for mirroring):", "default": "", "help": "Tomogram X dimension in the SAME (scaled) pixels as the coordinates. Only needed if 'Mirror X' is checked."},
            {"key": "tomo_size_y", "field_type": "text", "label": "Tomogram Y size (px, for mirroring):", "default": "", "help": "Tomogram Y dimension in the SAME (scaled) pixels as the coordinates. Only needed if 'Mirror Y' is checked."},
            {"key": "tomo_size_z", "field_type": "text", "label": "Tomogram Z size (px, for mirroring):", "default": "", "help": "Tomogram Z dimension in the SAME (scaled) pixels as the coordinates. Only needed if 'Mirror Z' is checked. NB: this is the size along the axis BEFORE any Y/Z swap."},
            {"key": "out_path", "field_type": "filename", "label": "Output particles.star:", "default": "particles.star", "pattern": "*.star", "directory": ".", "help": "Where to write the converted RELION particles STAR file. Relative paths land in this job's own output directory (<JobDir>/jobNNN/), the way a RELION job writes into its --o directory; an absolute path is honoured as given."},
            *REREF_OPTIONS,
        ],
        "standard_groups": [
            {"name": "", "fields": ["mod_path", "tomo_name", "scale", "swap_yz", "flip_x", "flip_y", "flip_z", "tomo_size_x", "tomo_size_y", "tomo_size_z", "out_path"]},
            {"name": REREF_GROUP_NAME, "fields": REREF_FIELD_KEYS},
        ],
        "program_guess": "converters.imod_bridge.model_to_coordinates",
        "flags_used": [],
        "commands_source": "(no subprocess — see backend/converters/imod_bridge.py:model_to_coordinates)",
        "is_custom": True,
    },
    "WarpImport": {
        "internal_name": "WarpImport",
        "label_new": "custom.warp_import",
        "display_name": "Import from Warp/M",
        "category": "Import & Conversion",
        "description": "Diff and harmonize a Warp/M particle STAR export against RELION-5's column conventions",
        "options": [
            {"key": "warp_star_path", "field_type": "filename", "label": "Warp/M STAR file:", "default": "", "pattern": "*.star;*.tomostar", "directory": ".", "help": "A particle STAR file exported from Warp/M. NOTE: modern Warp 2.0 / WarpTools 'ts_export_particles' already writes a RELION-5 tomography optimisation set (matching_optimisation_set.star + matching.star + matching_tomograms.star) that RELION-5 opens directly — for that output you don't need this bridge. This bridge is for older/particle STARs that still need wrp*→rln* column harmonization. A .tomostar is per-tilt-series geometry (wrp* columns, no particle coordinates), not a particle file — the diff below will show every required particle column missing for a .tomostar, which is expected."},
            {"key": "block_name", "field_type": "text", "label": "STAR block name (optional):", "default": "", "help": "Leave blank to auto-detect if the file has only one block."},
            {"key": "out_path", "field_type": "filename", "label": "Output particles.star:", "default": "particles.star", "pattern": "*.star", "directory": ".", "help": "Where to write the harmonized RELION particles STAR file (only written if all required columns are already present or mapped). Relative paths land in this job's own output directory."},
        ],
        "standard_groups": [{"name": "", "fields": ["warp_star_path", "block_name", "out_path"]}],
        "program_guess": "converters.warp_bridge.diff_columns",
        "flags_used": [],
        "commands_source": "(no subprocess — see backend/converters/warp_bridge.py)",
        "is_custom": True,
    },
    "DeepETPickerImport": {
        "internal_name": "DeepETPickerImport",
        "label_new": "custom.deepetpicker_import",
        "display_name": "Import from DeepETPicker",
        "category": "Import & Conversion",
        "description": "Convert DeepETPicker .coords picks into a RELION particles.star",
        "options": [
            {"key": "coords_path", "field_type": "filename", "label": "DeepETPicker .coords file:", "default": "", "pattern": "*.coords", "directory": ".", "help": "class_id x y z, whitespace-separated, voxels."},
            {"key": "tomo_name", "field_type": "text", "label": "Tomogram name (rlnTomoName):", "default": "", "help": "Value to write into rlnTomoName for every particle from this .coords file."},
            {"key": "binning_factor", "field_type": "slider", "label": "Binning factor:", "default": 1.0, "min": 0.01, "max": 10.0, "step": 0.01, "help": "relion_coord = deepet_coord * binning_factor."},
            {"key": "swap_yz", "field_type": "boolean", "label": "Swap Y and Z:", "default": False, "help": "Swap Y and Z coordinates (handedness-changing). Use if DeepETPicker's tomogram axis convention differs from your RELION tomogram's depth-in-Z."},
            {"key": "flip_x", "field_type": "boolean", "label": "Mirror X (needs X size):", "default": False, "help": "Mirror X about the tomogram centre: x -> (X size - 1) - x, for 0-based coordinates. Requires the X dimension below."},
            {"key": "flip_y", "field_type": "boolean", "label": "Mirror Y (needs Y size):", "default": False, "help": "Mirror Y about the tomogram centre: y -> (Y size - 1) - y, for 0-based coordinates. Requires the Y dimension below."},
            {"key": "flip_z", "field_type": "boolean", "label": "Mirror Z (needs Z size):", "default": False, "help": "Mirror Z about the tomogram centre: z -> (Z size - 1) - z, for 0-based coordinates. Requires the Z dimension below."},
            {"key": "tomo_size_x", "field_type": "text", "label": "Tomogram X size (px, for mirroring):", "default": "", "help": "Tomogram X dimension in the SAME (binned) voxels as the coordinates. Only needed if 'Mirror X' is checked."},
            {"key": "tomo_size_y", "field_type": "text", "label": "Tomogram Y size (px, for mirroring):", "default": "", "help": "Tomogram Y dimension in the SAME (binned) voxels as the coordinates. Only needed if 'Mirror Y' is checked."},
            {"key": "tomo_size_z", "field_type": "text", "label": "Tomogram Z size (px, for mirroring):", "default": "", "help": "Tomogram Z dimension in the SAME (binned) voxels as the coordinates. Only needed if 'Mirror Z' is checked (size along the axis before any Y/Z swap)."},
            {"key": "out_path", "field_type": "filename", "label": "Output particles.star:", "default": "particles.star", "pattern": "*.star", "directory": ".", "help": "Where to write the converted RELION particles STAR file. Relative paths land in this job's own output directory (<JobDir>/jobNNN/), like a RELION job's --o directory; an absolute path is honoured as given."},
            *REREF_OPTIONS,
        ],
        "standard_groups": [
            {"name": "", "fields": ["coords_path", "tomo_name", "binning_factor", "swap_yz", "flip_x", "flip_y", "flip_z", "tomo_size_x", "tomo_size_y", "tomo_size_z", "out_path"]},
            {"name": REREF_GROUP_NAME, "fields": REREF_FIELD_KEYS},
        ],
        "program_guess": "converters.deepetpicker_bridge.coords_to_relion_particles",
        "flags_used": [],
        "commands_source": "(no subprocess — see backend/converters/deepetpicker_bridge.py)",
        "is_custom": True,
    },
    "AreTomoImport": {
        "internal_name": "AreTomoImport",
        "label_new": "custom.aretomo_import",
        "display_name": "Import from AreTomo2 (.aln)",
        "category": "Import & Conversion",
        "description": "Convert an AreTomo2 .aln alignment into IMOD-style .xf/.tlt (for RELION's IMOD tilt-series import)",
        "options": [
            {"key": "aln_path", "field_type": "filename", "label": "AreTomo2 .aln file:", "default": "", "pattern": "*.aln", "directory": ".", "help": "AreTomo2 global-alignment file (SEC ROT GMAG TX TY SMEAN SFIT SCALE BASE TILT). AreTomo3 .aln works too. If you still have AreTomo's own -OutImod output, prefer that directly; this is for when you only kept the .aln."},
            {"key": "out_prefix", "field_type": "text", "label": "Output name prefix:", "default": "aligned", "help": "Writes <prefix>.xf and <prefix>.tlt into this job's output directory. These are IMOD-format files RELION-5's IMOD tilt-series import (and IMOD itself) read."},
            {"key": "note", "field_type": "text", "label": "Note:", "default": "", "help": "Optional note recorded with the job."},
        ],
        "standard_groups": [{"name": "", "fields": ["aln_path", "out_prefix", "note"]}],
        "program_guess": "converters.aretomo_bridge.aln_to_imod",
        "flags_used": [],
        "commands_source": "(no subprocess — see backend/converters/aretomo_bridge.py; .xf mapping = teamtomo/alnfile df_to_xf, theta=-ROT)",
        "is_custom": True,
    },
}


async def run_imod_import(project_dir: Path, values: dict, job_dir: Path) -> str:
    def work():
        df = imod_bridge.model_to_coordinates(
            _resolve_in(project_dir, values["mod_path"]),
            values["tomo_name"],
            scale_xyz=(values["scale"],) * 3,
            swap_yz=bool(values.get("swap_yz")),
            flip_x=bool(values.get("flip_x")),
            flip_y=bool(values.get("flip_y")),
            flip_z=bool(values.get("flip_z")),
            tomo_size_x=_opt_float(values.get("tomo_size_x")),
            tomo_size_y=_opt_float(values.get("tomo_size_y")),
            tomo_size_z=_opt_float(values.get("tomo_size_z")),
        )
        df, reref_lines = _apply_rereference(
            project_dir, values, df, values["tomo_name"], values["scale"], "Coordinate scale factor", job_dir)
        out_path = _resolve_out(job_dir, values.get("out_path"), "particles.star")
        if out_path.exists():
            backup = backup_before_overwrite(out_path)
            note = f" (existing file backed up to {backup})"
        else:
            note = ""
        write_particles(df, out_path, overwrite=True)
        return "\n".join([f"Converted {len(df)} particles from {values['mod_path']}", *reref_lines,
                          f"Wrote {out_path}{note}"])

    return await asyncio.to_thread(work)


async def run_warp_import(project_dir: Path, values: dict, job_dir: Path) -> str:
    def work():
        block = values.get("block_name") or None
        df = warp_bridge.load_warp_star(_resolve_in(project_dir, values["warp_star_path"]), block=block)
        diff = warp_bridge.diff_columns(df)
        lines = [f"Loaded {len(df)} rows, {len(df.columns)} columns from {values['warp_star_path']}"]
        lines.append(f"Matched RELION columns: {diff['matched']}")
        lines.append(f"Missing (not yet mapped): {diff['missing_from_source']}")
        lines.append(f"Extra Warp/M-only columns: {diff['extra_in_source']}")
        if diff["missing_from_source"]:
            lines.append(
                "Not written: required RELION columns are missing. Supply a "
                "column_map in converters/warp_bridge.py DEFAULT_COLUMN_MAP "
                "(or edit the command/field set) once you've confirmed the "
                "correct source column names."
            )
        else:
            # harmonize_particle_star (not the raw df) so the
            # rlnMicrographName -> rlnTomoName alternate-column rename
            # actually applies before writing -- see its own docstring and
            # TOMOGRAM_NAME_COL_ALTERNATES in warp_bridge.py. With
            # DEFAULT_COLUMN_MAP still empty, this is a no-op for any df
            # that already has rlnTomoName directly.
            harmonized = warp_bridge.harmonize_particle_star(df)
            if "rlnTomoName" not in df.columns and "rlnTomoName" in harmonized.columns:
                # Surface this explicitly -- diff_columns' "Matched RELION
                # columns" line above only shows the alternate's ORIGINAL
                # name (rlnMicrographName), so without this a user could
                # miss that it's being written out under a different name.
                lines.append(
                    "Note: no rlnTomoName column found — using rlnMicrographName "
                    "as the tomogram identity (older RELION 3.0-style tomography "
                    "format; renamed to rlnTomoName in the output)."
                )
            out_path = _resolve_out(job_dir, values.get("out_path"), "particles.star")
            note = ""
            if out_path.exists():
                backup = backup_before_overwrite(out_path)
                note = f" (existing file backed up to {backup})"
            write_particles(harmonized, out_path, overwrite=True)
            lines.append(f"Wrote {out_path}{note}")
        return "\n".join(lines)

    return await asyncio.to_thread(work)


async def run_deepetpicker_import(project_dir: Path, values: dict, job_dir: Path) -> str:
    def work():
        df = deepetpicker_bridge.coords_to_relion_particles(
            _resolve_in(project_dir, values["coords_path"]),
            values["tomo_name"],
            binning_factor=values["binning_factor"],
            swap_yz=bool(values.get("swap_yz")),
            flip_x=bool(values.get("flip_x")),
            flip_y=bool(values.get("flip_y")),
            flip_z=bool(values.get("flip_z")),
            tomo_size_x=_opt_float(values.get("tomo_size_x")),
            tomo_size_y=_opt_float(values.get("tomo_size_y")),
            tomo_size_z=_opt_float(values.get("tomo_size_z")),
        )
        df, reref_lines = _apply_rereference(
            project_dir, values, df, values["tomo_name"], values["binning_factor"], "Binning factor", job_dir)
        out_path = _resolve_out(job_dir, values.get("out_path"), "particles.star")
        if out_path.exists():
            backup = backup_before_overwrite(out_path)
            note = f" (existing file backed up to {backup})"
        else:
            note = ""
        write_particles(df, out_path, overwrite=True)
        return "\n".join([f"Converted {len(df)} particles from {values['coords_path']}", *reref_lines,
                          f"Wrote {out_path}{note}"])

    return await asyncio.to_thread(work)


async def run_aretomo_import(project_dir: Path, values: dict, job_dir: Path) -> str:
    def work():
        aln_path = _resolve_in(project_dir, values["aln_path"])
        prefix = values.get("out_prefix") or "aligned"
        out_xf = _resolve_out(job_dir, f"{prefix}.xf", "aligned.xf")
        out_tlt = _resolve_out(job_dir, f"{prefix}.tlt", "aligned.tlt")
        summary = aretomo_bridge.aln_to_imod(aln_path, out_xf, out_tlt)
        lines = [
            f"Read AreTomo2 alignment: {values['aln_path']}",
            f"Wrote {summary['n_images']} transforms -> {out_xf}",
            f"Wrote {summary['n_images']} tilt angles -> {out_tlt}",
        ]
        if summary["raw_size"]:
            lines.append(f"RawSize (W H N): {summary['raw_size']}")
        if summary["n_dark_excluded"]:
            lines.append(
                f"Excluded {summary['n_dark_excluded']} dark image(s); original "
                f"indices: {summary['dark_indices_original']}"
            )
        lines.append(
            "Point RELION-5's IMOD tilt-series import at these .xf/.tlt. "
            "TX/TY were in pixels of the aligned stack — supply the correct "
            "pixel size downstream. The ROT/TX/TY -> .xf formula has been "
            "verified against AreTomo2's own -OutImod source code (see "
            "converters/aretomo_bridge.py's module docstring)."
        )
        return "\n".join(lines)

    return await asyncio.to_thread(work)


async def run_manual_pick(project_dir: Path, values: dict, job_dir: Path) -> str:
    """Doesn't pick anything itself -- just validates the input micrographs
    resolve to something real and reports how many, so the popup's summary
    line isn't blank. The actual picking happens afterward, through the
    Picker button (app.js), which reads/writes this job's own directory via
    /api/manual-pick/{run_id}/spa/* -- see manual_pick.py.

    main.py passes stays_running=True for this job (is_picker), so a
    successful return here leaves the run "running", not "completed" --
    see job_runner.JobRunManager.start_custom_job's own docstring. The user
    ends the session explicitly (the "Done" button, job_runner.set_status)
    when they're actually finished picking, and can come back to a
    "completed" one non-destructively (job_runner.resume_run, the toolbar's
    "Continue" action -- reads whatever's already there, changes nothing).

    Overwrite reuses this SAME function (start_custom_job's overwrite_
    run_id path), which is why clearing prior picks lives HERE rather than
    behind a separate "is this an overwrite" flag: a fresh run's directory
    is always empty, so clear_spa_picks is a no-op there and only actually
    removes anything on a genuine Overwrite -- matching real RELION's own
    "Overwrite re-runs into the SAME directory" semantics (gui_mainwindow.
    cpp's cb_toggle_overwrite_continue), the destructive counterpart to
    Continue above.
    """
    fn_in = values.get("fn_in", "")
    if not fn_in:
        raise ValueError("Input micrographs field is required.")

    def work():
        removed = manual_pick.clear_spa_picks(job_dir)
        mics = manual_pick.list_spa_micrographs(project_dir, fn_in)
        cleared_note = f"Cleared {removed} existing pick file(s) from a previous run.\n" if removed else ""
        return (
            f"{cleared_note}"
            f"Found {len(mics)} micrograph(s) in {fn_in}.\n"
            f"Use the Picker button above to open the viewer and start picking "
            f"-- picks save into this job's own directory as you go, and this "
            f"job's outputs stay valid for Extract to read even after you close "
            f"the picker."
        )

    return await asyncio.to_thread(work)


async def run_tomo_manual_pick(project_dir: Path, values: dict, job_dir: Path) -> str:
    """Tomogram counterpart of run_manual_pick above -- same stays_running /
    Done / Continue / Overwrite-clears-first reasoning (see its docstring).
    Only point picking (pick_mode "Particles") is wired up on the viewer
    side right now; other pick_mode choices (helical filaments, spheres,
    surfaces) still round-trip into job.star (real RELION option, kept for
    a native-GUI-repair later) but the picker doesn't yet do anything
    different for them."""
    in_tomoset = values.get("in_tomoset", "")
    if not in_tomoset:
        raise ValueError("Input tomograms.star field is required.")

    def work():
        removed = manual_pick.clear_tomo_picks(job_dir)
        tomograms = viz._tomograms_from_star(project_dir, viz._safe(project_dir, in_tomoset))
        if not tomograms:
            raise ValueError(
                f"No tomogram volumes found in {in_tomoset}: it needs rlnTomoName plus one of "
                + ", ".join(col for col, _ in viz.VOLUME_COLUMNS) + ".")
        cleared_note = f"Cleared {removed} existing pick file(s) from a previous run.\n" if removed else ""
        kinds = sorted({t["volume"] for t in tomograms})
        shown = {"denoised": "denoised", "reconstructed": "reconstructed", "half1": "Half 1 (even/odd)"}
        warnings = "".join(f"{w}\n" for w in viz._volume_warnings(tomograms))
        return (
            f"{cleared_note}"
            f"Found {len(tomograms)} tomogram(s) in {in_tomoset} "
            f"(showing the {' / '.join(shown[k] for k in kinds)} volume).\n{warnings}"
            f"Use the Picker button above to open the viewer and start picking "
            f"-- picks save into this job's own directory as you go, and this "
            f"job's outputs (particles.star + optimisation_set.star) stay valid "
            f"for TomoSubtomo to read even after you close the picker."
        )

    return await asyncio.to_thread(work)


async def run_exclude_tilt_images(project_dir: Path, values: dict, job_dir: Path) -> str:
    """Tilt-image-exclusion counterpart of run_manual_pick/run_tomo_manual_
    pick above -- same stays_running / Done / Continue / Overwrite-clears-
    first reasoning (see run_manual_pick's docstring).

    Unlike picking, there's an obvious non-destructive default with nothing
    chosen yet: keep EVERY tilt image (relion_tomo_exclude_tilt_images' own
    napari widget starts the exact same way -- nothing pre-excluded until
    the user acts on it). So, unlike an unpicked SPA/tomo job (which has
    literally no particles yet), this writes that full pass-through
    immediately (exclude_tilts.write_passthrough) rather than leaving the
    job's output missing until the user opens the reviewer -- the job's
    output (selected_tilt_series.star) stays valid for downstream jobs
    (Reconstruct Tomograms, TomoSubtomo, ...) to read even if the reviewer
    is never opened at all.
    """
    in_tiltseries = values.get("in_tiltseries", "")
    if not in_tiltseries:
        raise ValueError("Input tilt series field is required.")

    def work():
        removed, n_series = exclude_tilts.reset_and_write_passthrough(project_dir, job_dir, in_tiltseries)
        cleared_note = f"Cleared {removed} existing output file(s) from a previous run.\n" if removed else ""
        return (
            f"{cleared_note}"
            f"Found {n_series} tilt series in {in_tiltseries}.\n"
            f"Every tilt image is kept by default -- 'nothing excluded' is a "
            f"legitimate choice on its own. Use the Picker button above to "
            f"review each tilt series and exclude bad images -- this job's "
            f"output (selected_tilt_series.star) stays valid for downstream "
            f"jobs (Reconstruct Tomograms, TomoSubtomo, ...) to read either way."
        )

    return await asyncio.to_thread(work)


async def run_pick_filter(project_dir: Path, values: dict, job_dir: Path) -> str:
    """Starts a Filter Picks session: reads the picks file, and writes a first
    particles.star with the job's current label and range (RELION's defaults keep
    every pick, so a job closed untouched is a harmless pass-through, like Exclude
    Tilt Images' keep-everything start). The tuning itself happens in the browser
    (the tuner button), which re-writes the output with /api/pick-filter/{run}/apply.

    `values` is the run's own field_values dict, so the label this settles on is
    what the job records. main.py passes stays_running=True (is_picker): the user
    ends the session with Done, as for the picking jobs."""
    import pick_filter

    star = str(values.get("fn_data") or "").strip()
    if not star:
        raise ValueError("The particles / picks STAR file is required.")

    def work():
        table = pick_filter.load_table(project_dir, star)
        columns = {c["name"] for c in table.numeric_columns()}
        if not columns:
            raise ValueError(f"{star} has no numeric column that varies, so there is nothing to filter on.")
        label = str(values.get("select_label") or "").strip()
        note = ""
        if label not in columns:
            chosen = table.default_column()
            if label:
                note = f"{label} is not a numeric column of {star}; starting with {chosen}.\n"
            label = chosen
            values["select_label"] = label
            values["select_minval"], values["select_maxval"] = "", ""
        lo, hi = pick_filter.parse_range(label, values.get("select_minval"), values.get("select_maxval"), table)
        result = pick_filter.apply_filter(project_dir, star, job_dir, label, lo, hi)
        pick_filter.record_selection(job_dir, values, label, lo, hi)
        what = f"{label} from {pick_filter._number(lo)} to {pick_filter._number(hi)}"
        return (f"{note}Read {result['total']:,} picks from {star}; kept {result['kept']:,} ({what}).\n"
                "Use the tuner button above to change the selection: it re-writes this job's particles.star "
                "each time you apply, and Done keeps what is there.")

    return await asyncio.to_thread(work)


CUSTOM_JOB_RUNNERS = {
    "PickFilter": run_pick_filter,
    "Manualpick": run_manual_pick,
    "TomoManualPick": run_tomo_manual_pick,
    "TomoExcludeTiltImages": run_exclude_tilt_images,
    "ImodImport": run_imod_import,
    "WarpImport": run_warp_import,
    "DeepETPickerImport": run_deepetpicker_import,
    "AreTomoImport": run_aretomo_import,
}
