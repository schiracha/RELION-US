"""
gui/app.py — portable companion GUI for RELION-5 tomography projects.

Run locally with:
    streamlit run gui/app.py

This is a browser-rendered front end (works identically on macOS/Linux/
Windows, no Qt install needed) for:
  - browsing a RELION-5 tomography project's STAR files as tables,
  - running the IMOD / Warp-M / DeepETPicker converters in converters/
    without touching a terminal,
  - generating and (optionally) submitting SLURM jobs for anything heavy,
    via the same slurm/submit.py helper the command line uses.

It does not replace RELION's own GUI for actually running relion_refine /
relion_tomo_* jobs — those still go through RELION (locally or, more
commonly for real datasets, as an sbatch job on Rivanna/Afton). This is the
layer around that: getting data in and out cleanly, and making the STAR
files legible without opening them in a text editor.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converters import deepetpicker_bridge, imod_bridge, warp_bridge
from converters.star_io import StarDocument, backup_before_overwrite, write_particles

st.set_page_config(page_title="RELION Tomo Bridge", layout="wide")
st.title("RELION-5 Tomography — Import/Export Bridge")
st.caption(
    "A companion GUI that reads/writes RELION's own STAR files and drives "
    "relion_* / IMOD / SLURM as subprocesses — it does not modify RELION "
    "itself. See docs/ARCHITECTURE.md for why."
)

tab_browse, tab_imod, tab_warp, tab_deepet, tab_cluster = st.tabs(
    ["Browse STAR files", "IMOD bridge", "Warp/M bridge", "DeepETPicker import", "Run on cluster"]
)

# ---------------------------------------------------------------------
# Browse STAR files
# ---------------------------------------------------------------------
with tab_browse:
    st.subheader("Open any RELION STAR file as a table")
    star_path_str = st.text_input(
        "Path to a .star file", key="browse_path", placeholder="/path/to/particles.star"
    )
    if star_path_str:
        path = Path(star_path_str).expanduser()
        try:
            doc = StarDocument.read(path)
            block_name = st.selectbox("Block", sorted(doc.blocks.keys()))
            df = doc.block(block_name)
            st.write(f"{len(df)} rows × {len(df.columns)} columns")
            st.dataframe(df, use_container_width=True)
        except FileNotFoundError:
            st.error(f"File not found: {path}")
        except Exception as exc:  # noqa: BLE001 - surfaced to the user directly
            st.error(f"Could not read {path}: {exc}")

# ---------------------------------------------------------------------
# IMOD bridge
# ---------------------------------------------------------------------
with tab_imod:
    st.subheader(".mod (3dmod) ↔ particles.star")
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**IMOD .mod → RELION particles.star**")
        mod_path_str = st.text_input(".mod file path", key="mod_in")
        tomo_name_in = st.text_input("Tomogram name (rlnTomoName)", key="tomo_name_mod_in")
        scale = st.number_input("Coordinate scale factor", value=1.0, key="mod_scale")
        if st.button("Convert .mod → particles.star"):
            try:
                df = imod_bridge.model_to_coordinates(
                    mod_path_str, tomo_name_in, scale_xyz=(scale, scale, scale)
                )
                st.success(f"Converted {len(df)} particles")
                st.dataframe(df, use_container_width=True)
                out_path = Path(mod_path_str).with_suffix(".particles.star")
                write_particles(df, out_path, overwrite=True)
                st.info(f"Wrote {out_path}")
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))

    with col2:
        st.markdown("**RELION particles.star → IMOD .mod (for 3dmod QC)**")
        particles_path_str = st.text_input("particles.star path", key="particles_in")
        tomo_name_out = st.text_input("Filter to rlnTomoName", key="tomo_name_mod_out")
        if st.button("Convert particles.star → .mod"):
            try:
                doc = StarDocument.read(particles_path_str)
                df = doc.block()
                out_mod = Path(particles_path_str).with_name(f"{tomo_name_out}_picks.mod")
                imod_bridge.coordinates_to_model(df, out_mod, tomo_name=tomo_name_out)
                st.success(f"Wrote {out_mod} — open it in 3dmod on top of the tomogram")
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))

# ---------------------------------------------------------------------
# Warp/M bridge
# ---------------------------------------------------------------------
with tab_warp:
    st.subheader("Warp/M STAR ↔ RELION-5 STAR")
    warp_path_str = st.text_input("Warp/M STAR file path", key="warp_path")
    if warp_path_str:
        try:
            df = warp_bridge.load_warp_star(warp_path_str)
            diff = warp_bridge.diff_columns(df)
            st.write("Column comparison against RELION's required columns:")
            st.json(diff)
            if diff["missing_from_source"]:
                st.warning(
                    "Some required RELION columns aren't present under their "
                    "RELION name. Supply a column_map (see "
                    "converters/warp_bridge.py DEFAULT_COLUMN_MAP) — send a "
                    "sample file and I'll fill in a verified mapping."
                )
            else:
                st.success("All required RELION columns already present — no mapping needed.")
            st.dataframe(df.head(50), use_container_width=True)
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))

# ---------------------------------------------------------------------
# DeepETPicker import
# ---------------------------------------------------------------------
with tab_deepet:
    st.subheader("DeepETPicker .coords → particles.star")
    mode = st.radio("Input", ["Single .coords file", "Directory of .coords files"], horizontal=True)
    binning = st.number_input("Binning factor (DeepETPicker voxel size / RELION tomogram pixel size)", value=1.0)

    if mode == "Single .coords file":
        coords_path_str = st.text_input("Path to .coords file", key="coords_single")
        tomo_name_de = st.text_input("Tomogram name (rlnTomoName)", key="tomo_name_de")
        if st.button("Convert .coords → particles.star", key="btn_single"):
            try:
                df = deepetpicker_bridge.coords_to_relion_particles(
                    coords_path_str, tomo_name_de, binning_factor=binning
                )
                st.dataframe(df, use_container_width=True)
                out_path = Path(coords_path_str).with_suffix(".particles.star")
                write_particles(df, out_path, overwrite=True)
                st.success(f"Wrote {out_path} ({len(df)} particles)")
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
    else:
        coords_dir_str = st.text_input("Directory containing *.coords files", key="coords_dir")
        if st.button("Convert directory → particles.star", key="btn_dir"):
            try:
                df = deepetpicker_bridge.batch_coords_directory_to_particles(
                    coords_dir_str, binning_factor=binning
                )
                st.dataframe(df, use_container_width=True)
                out_path = Path(coords_dir_str) / "combined_particles.star"
                write_particles(df, out_path, overwrite=True)
                st.success(f"Wrote {out_path} ({len(df)} particles across "
                           f"{df['rlnTomoName'].nunique()} tomograms)")
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))

# ---------------------------------------------------------------------
# SLURM
# ---------------------------------------------------------------------
with tab_cluster:
    st.subheader("Generate / submit a SLURM job (Rivanna/Afton)")
    st.caption(
        "Fills in the same sbatch templates the command line uses "
        "(slurm/submit.py), so a job launched from here is identical to "
        "one launched by hand."
    )
    template_choice = st.selectbox(
        "Template", ["template_relion_job.sbatch", "template_python_job.sbatch"]
    )
    account = st.text_input("Allocation account", key="slurm_account")
    job_name = st.text_input("Job name", value="tomo_bridge_job", key="slurm_job_name")
    dry_run = st.checkbox("Dry run (write script only, don't submit)", value=True)

    if st.button("Generate script"):
        import subprocess

        template_path = Path(__file__).resolve().parent.parent / "slurm" / template_choice
        cmd = [
            sys.executable,
            str(Path(__file__).resolve().parent.parent / "slurm" / "submit.py"),
            "--template", str(template_path),
            "--account", account or "ACCOUNT_NAME",
            "--job-name", job_name,
        ]
        if dry_run:
            cmd.append("--dry-run")
        result = subprocess.run(cmd, capture_output=True, text=True)
        st.code(result.stdout + result.stderr)
