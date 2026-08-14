"""
custom_jobs.py — wires the IMOD / Warp-M / DeepETPicker import bridges
(backend/converters/, built earlier in this project and unit-tested there)
into this app's Jobs list as three more job types, run the same way as
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

from converters import deepetpicker_bridge, imod_bridge, warp_bridge
from converters.star_io import backup_before_overwrite, write_particles

# Field definitions for the three custom jobs, in the same shape
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
            {"key": "mod_path", "field_type": "filename", "label": "IMOD .mod file:", "default": "", "pattern": "*.mod", "directory": ".", "help": "Path to the IMOD scattered-point model to convert."},
            {"key": "tomo_name", "field_type": "text", "label": "Tomogram name (rlnTomoName):", "default": "", "help": "Value to write into rlnTomoName for every particle from this .mod file."},
            {"key": "scale", "field_type": "slider", "label": "Coordinate scale factor:", "default": 1.0, "min": 0.01, "max": 10.0, "step": 0.01, "help": "Multiply IMOD model coordinates by this factor (e.g. to correct for a binning mismatch between the .mod and the RELION tomogram)."},
            {"key": "out_path", "field_type": "filename", "label": "Output particles.star:", "default": "particles.star", "pattern": "*.star", "directory": ".", "help": "Where to write the converted RELION particles STAR file."},
        ],
        "standard_fields": ["mod_path", "tomo_name", "scale", "out_path"],
        "advanced_groups": {},
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
        "description": "Diff and harmonize a Warp/M STAR export against RELION-5's column conventions",
        "options": [
            {"key": "warp_star_path", "field_type": "filename", "label": "Warp/M STAR file:", "default": "", "pattern": "*.star;*.tomostar", "directory": ".", "help": "A .tomostar or particle STAR file exported from Warp/M."},
            {"key": "block_name", "field_type": "text", "label": "STAR block name (optional):", "default": "", "help": "Leave blank to auto-detect if the file has only one block."},
            {"key": "out_path", "field_type": "filename", "label": "Output particles.star:", "default": "particles.star", "pattern": "*.star", "directory": ".", "help": "Where to write the harmonized RELION particles STAR file (only written if all required columns are already present or mapped)."},
        ],
        "standard_fields": ["warp_star_path", "block_name", "out_path"],
        "advanced_groups": {},
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
            {"key": "out_path", "field_type": "filename", "label": "Output particles.star:", "default": "particles.star", "pattern": "*.star", "directory": ".", "help": "Where to write the converted RELION particles STAR file."},
        ],
        "standard_fields": ["coords_path", "tomo_name", "binning_factor", "out_path"],
        "advanced_groups": {},
        "program_guess": "converters.deepetpicker_bridge.coords_to_relion_particles",
        "flags_used": [],
        "commands_source": "(no subprocess — see backend/converters/deepetpicker_bridge.py)",
        "is_custom": True,
    },
}


async def run_imod_import(project_dir: Path, values: dict) -> str:
    def work():
        df = imod_bridge.model_to_coordinates(
            values["mod_path"], values["tomo_name"], scale_xyz=(values["scale"],) * 3
        )
        out_path = project_dir / values["out_path"]
        if out_path.exists():
            backup = backup_before_overwrite(out_path)
            note = f" (existing file backed up to {backup})"
        else:
            note = ""
        write_particles(df, out_path, overwrite=True)
        return f"Converted {len(df)} particles from {values['mod_path']}\nWrote {out_path}{note}"

    return await asyncio.to_thread(work)


async def run_warp_import(project_dir: Path, values: dict) -> str:
    def work():
        block = values.get("block_name") or None
        df = warp_bridge.load_warp_star(values["warp_star_path"], block=block)
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
            out_path = project_dir / values["out_path"]
            write_particles(df, out_path, overwrite=True)
            lines.append(f"Wrote {out_path}")
        return "\n".join(lines)

    return await asyncio.to_thread(work)


async def run_deepetpicker_import(project_dir: Path, values: dict) -> str:
    def work():
        df = deepetpicker_bridge.coords_to_relion_particles(
            values["coords_path"], values["tomo_name"], binning_factor=values["binning_factor"]
        )
        out_path = project_dir / values["out_path"]
        if out_path.exists():
            backup = backup_before_overwrite(out_path)
            note = f" (existing file backed up to {backup})"
        else:
            note = ""
        write_particles(df, out_path, overwrite=True)
        return f"Converted {len(df)} particles from {values['coords_path']}\nWrote {out_path}{note}"

    return await asyncio.to_thread(work)


CUSTOM_JOB_RUNNERS = {
    "ImodImport": run_imod_import,
    "WarpImport": run_warp_import,
    "DeepETPickerImport": run_deepetpicker_import,
}
