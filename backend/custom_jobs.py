"""
custom_jobs.py — wires the IMOD / Warp-M / DeepETPicker / AreTomo2 import bridges
(backend/converters/, built earlier in this project and unit-tested there)
into this app's Jobs list as four more job types, run the same way as
every RELION job: a popup with standard inputs, an Advanced tab, live
output, and an Errors tab.

These don't spawn a relion_* subprocess — they call directly into
converters/ in a worker thread (via asyncio.to_thread, since pandas/
starfile calls are synchronous) and report a text summary as their "live
output". See job_runner.JobRunManager.start_custom_job.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

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

# Field definitions for the custom jobs, in the same shape
# job_registry.build_job_definition() produces for real RELION jobs, so the
# frontend renders them identically. All fields are "standard" (no advanced
# tab needed — these are already small, focused jobs).

CUSTOM_JOB_DEFINITIONS = {
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
        ],
        "standard_groups": [{"name": "", "fields": ["mod_path", "tomo_name", "scale", "swap_yz", "flip_x", "flip_y", "flip_z", "tomo_size_x", "tomo_size_y", "tomo_size_z", "out_path"]}],
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
        ],
        "standard_groups": [{"name": "", "fields": ["coords_path", "tomo_name", "binning_factor", "swap_yz", "flip_x", "flip_y", "flip_z", "tomo_size_x", "tomo_size_y", "tomo_size_z", "out_path"]}],
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
        out_path = _resolve_out(job_dir, values.get("out_path"), "particles.star")
        if out_path.exists():
            backup = backup_before_overwrite(out_path)
            note = f" (existing file backed up to {backup})"
        else:
            note = ""
        write_particles(df, out_path, overwrite=True)
        return f"Converted {len(df)} particles from {values['mod_path']}\nWrote {out_path}{note}"

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
            out_path = _resolve_out(job_dir, values.get("out_path"), "particles.star")
            note = ""
            if out_path.exists():
                backup = backup_before_overwrite(out_path)
                note = f" (existing file backed up to {backup})"
            write_particles(df, out_path, overwrite=True)
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
        out_path = _resolve_out(job_dir, values.get("out_path"), "particles.star")
        if out_path.exists():
            backup = backup_before_overwrite(out_path)
            note = f" (existing file backed up to {backup})"
        else:
            note = ""
        write_particles(df, out_path, overwrite=True)
        return f"Converted {len(df)} particles from {values['coords_path']}\nWrote {out_path}{note}"

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
            "pixel size downstream. Validate against an AreTomo -OutImod .xf "
            "if exactness matters (see the .aln field help)."
        )
        return "\n".join(lines)

    return await asyncio.to_thread(work)


CUSTOM_JOB_RUNNERS = {
    "ImodImport": run_imod_import,
    "WarpImport": run_warp_import,
    "DeepETPickerImport": run_deepetpicker_import,
    "AreTomoImport": run_aretomo_import,
}
