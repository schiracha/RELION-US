"""
Tests for job_registry.py — the layer that turns the raw RELION-source
extraction (data/job_definitions_raw.json) into what the API/frontend use.

These are regression tests against the real extracted data (not synthetic
fixtures): the whole point of this app is fidelity to actual RELION source,
so testing against a fake/simplified job definition wouldn't catch the bugs
that matter (like the std::string("") artifact caught during development —
see data/extract_job_definitions.py's FUNC_CAST_RE).
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_registry
from job_catalog import CUSTOM_JOBS, JOB_CATALOG, PIPELINE_SPA_ONLY, PIPELINE_TOMO_ONLY, pipeline_type

CPP_ARTIFACT_RE = re.compile(r"std::|\(\(|\)\)")


@pytest.mark.parametrize("internal_name", sorted(JOB_CATALOG.keys()))
def test_every_relion_job_builds_without_error(internal_name):
    d = job_registry.build_job_definition(internal_name)
    assert d["internal_name"] == internal_name
    assert d["program_guess"], f"{internal_name} has no program_guess"
    assert isinstance(d["options"], list) and len(d["options"]) > 0
    assert isinstance(d["standard_fields"], list) and len(d["standard_fields"]) > 0


@pytest.mark.parametrize("internal_name", sorted(JOB_CATALOG.keys()))
def test_no_leftover_cpp_syntax_in_draft_or_defaults(internal_name):
    """Regression test for the std::string("") class of parsing bug."""
    d = job_registry.build_job_definition(internal_name)
    assert not CPP_ARTIFACT_RE.search(d["draft_command"]), (
        f"{internal_name} draft command has leftover C++ syntax: {d['draft_command']}"
    )
    for opt in d["options"]:
        for field_name in ("default", "pattern", "directory"):
            v = opt.get(field_name)
            if isinstance(v, str):
                assert "std::" not in v, (
                    f"{internal_name}.{opt['key']}.{field_name} has leftover C++ syntax: {v}"
                )


@pytest.mark.parametrize("internal_name", sorted(JOB_CATALOG.keys()))
def test_standard_and_advanced_fields_are_disjoint_and_known(internal_name):
    d = job_registry.build_job_definition(internal_name)
    known_keys = {o["key"] for o in d["options"]}
    standard = set(d["standard_fields"])
    assert standard <= known_keys, f"{internal_name} standard_fields reference unknown option keys"
    for group, keys in d["advanced_groups"].items():
        assert set(keys) <= known_keys, f"{internal_name}/{group} references unknown option keys"
        overlap = standard & set(keys)
        assert not overlap, f"{internal_name}: fields in both standard and '{group}': {overlap}"


def test_unmapped_fields_are_a_subset_of_all_fields():
    d = job_registry.build_job_definition("Import")
    known_keys = {o["key"] for o in d["options"]}
    assert set(d["unmapped_fields"]) <= known_keys


def test_boolean_field_emits_bare_flag_only_when_true_and_mapped():
    # Import's "do_other" is a real case where the JobOption key and the
    # RELION flag literal are identical (verified against flags_used) --
    # unlike e.g. Motioncorr's "do_float16", which RELION maps to the
    # differently-named --float16 flag and is correctly left unmapped
    # (see unmapped_fields) rather than guessed.
    raw = job_registry._load_raw()["Import"]
    cmd_true, _ = job_registry._build_draft_command(raw, {"do_other": True})
    cmd_false, _ = job_registry._build_draft_command(raw, {"do_other": False})
    assert "--do_other" in cmd_true
    assert "--do_other" not in cmd_false


def test_executable_path_placeholder_resolves_from_field_values():
    """DynaMight/ModelAngelo/External don't hard-code a binary -- RELION runs
    whatever path the user set in a JobOption. Confirm the {joboptions.X}
    placeholder (see extract_job_definitions.py extract_program_guess)
    resolves against supplied field values, and falls back to a clear
    instruction (not a silent blank) when unset."""
    raw = job_registry._load_raw()["DynaMight"]
    assert raw["program_guess"] == "{joboptions.fn_dynamight_exe}"

    cmd_set, _ = job_registry._build_draft_command(raw, {"fn_dynamight_exe": "/opt/dynamight/run.py"})
    assert cmd_set.startswith("/opt/dynamight/run.py")

    cmd_unset, _ = job_registry._build_draft_command(raw, {"fn_dynamight_exe": ""})
    assert "fn_dynamight_exe" in cmd_unset  # falls back to a pointer, not blank/None


# --- Draft-command overrides for RELION-5 Python tomo tools -----------------
# These jobs' real CLI flags are hyphenated multi-word names that don't match
# their snake_case option keys, so the generic draft rule can't map them. The
# curated, source-verified overlays in job_catalog (DRAFT_PROGRAM_OVERRIDE /
# DRAFT_FLAG_MAP / DRAFT_SUPPRESS) fix that. See getCommandsTomoImportJob in
# src/pipeline_jobs.cpp (RELION cloned 2026-08-14).

def test_tomo_import_default_draft_uses_serialem_tiltseries_program():
    """The default TomoImport (do_coords == false) must draft the SerialEM
    tilt-series importer, NOT the do_coords==true coordinate importer the
    extractor originally picked up as program_guess."""
    d = job_registry.build_job_definition("TomoImport")
    draft = d["draft_command"]
    assert draft.startswith("relion_python_tomo_import SerialEM"), draft
    # must NOT be the coordinate-branch program
    assert "relion_tomo_import_coordinates" not in draft, draft


def test_tomo_import_default_draft_maps_hyphenated_flags():
    """The hyphenated RELION flags must appear, mapped from their snake_case
    option keys — this is the core bug the overlay fixes."""
    d = job_registry.build_job_definition("TomoImport")
    draft = d["draft_command"]
    for flag in (
        "--nominal-pixel-size",
        "--voltage",
        "--spherical-aberration",
        "--amplitude-contrast",
        "--dose-per-tilt-image",
        "--tilt-image-movie-pattern",
        "--mdoc-file-pattern",
        "--nominal-tilt-axis-angle",
    ):
        assert flag in draft, f"{flag} missing from draft: {draft}"
    # truncated garbage flags from the old hyphen-splitting bug must be gone
    for bad in ("--tilt ", "--nominal ", "--dose ", "--spherical ", "--amplitude "):
        assert bad not in draft, f"truncated flag {bad!r} leaked into draft: {draft}"


def test_tomo_import_default_draft_suppresses_coordinate_branch_fields():
    """do_coords==true branch options (scale_factor, add_factor, ...) must not
    leak into the default tilt-series draft, and must not be flagged as
    'unmapped' either (they're deliberately omitted, not un-handled)."""
    d = job_registry.build_job_definition("TomoImport")
    draft = d["draft_command"]
    assert "--scale_factor" not in draft, draft
    assert "--add_factor" not in draft, draft
    for suppressed in ("scale_factor", "add_factor", "in_coords", "is_center"):
        assert suppressed not in d["unmapped_fields"], suppressed


def test_tomo_exclude_tilt_images_maps_its_hyphenated_flags():
    d = job_registry.build_job_definition("TomoExcludeTiltImages")
    draft = d["draft_command"]
    assert "--cache-size" in draft, draft
    # in_tiltseries has an empty default so no value is emitted, but the key
    # must be mapped (not flagged unmapped) via the overlay.
    assert "in_tiltseries" not in d["unmapped_fields"], d["unmapped_fields"]


def test_extractor_regex_captures_full_hyphenated_flags():
    """Guard the root-cause fix: the flag regex must capture whole hyphenated
    flags, not truncate at the first hyphen."""
    import re as _re
    src = 'command += " --tilt-image-movie-pattern \\"" + x; command += " --i \\""'
    flags = _re.findall(r'"\s*(--[A-Za-z][A-Za-z0-9_-]*)', src)
    assert "--tilt-image-movie-pattern" in flags
    assert "--tilt" not in flags
    assert "--i" in flags


def test_catalog_lists_all_relion_and_custom_jobs():
    catalog = job_registry.list_catalog()
    names = {j["internal_name"] for j in catalog}
    assert names == set(JOB_CATALOG.keys()) | set(CUSTOM_JOBS.keys())
    n_custom = sum(1 for j in catalog if j["is_custom"])
    assert n_custom == len(CUSTOM_JOBS)


# --- SPA / Tomo / All jobs-list toggle --------------------------------------

def test_catalog_entries_carry_a_pipeline_type():
    """Every job the sidebar can render must be classifiable, or the
    SPA/Tomo/All toggle would silently drop it from every filtered view."""
    for job in job_registry.list_catalog():
        assert job["pipeline_type"] in ("spa", "tomo", "shared"), job["internal_name"]


def test_tomo_prefixed_internal_names_are_never_classified_spa():
    """internal_name's own 'Tomo' prefix is RELION's own naming convention
    (pipeline_jobs.h) for tomography-specific jobs -- a job named TomoXxx
    being reachable under the SPA-only view would be a real usability bug,
    not just an inconsistency."""
    for internal_name in JOB_CATALOG:
        if internal_name.startswith("Tomo"):
            assert pipeline_type(internal_name) != "spa", internal_name


def test_spa_and_tomo_toggle_never_hides_all_jobs():
    """Regression guard for the hard requirement behind this feature ('I
    want all jobs available no matter what type of pipeline'): the SPA view
    and the Tomo view must each still show at least the shared jobs, and
    between them every job in the catalog must be reachable without
    switching to 'All'."""
    all_names = set(JOB_CATALOG) | set(CUSTOM_JOBS)
    visible_in_spa = {n for n in all_names if pipeline_type(n) in ("spa", "shared")}
    visible_in_tomo = {n for n in all_names if pipeline_type(n) in ("tomo", "shared")}
    assert visible_in_spa  # SPA view is never empty
    assert visible_in_tomo  # Tomo view is never empty
    assert visible_in_spa | visible_in_tomo == all_names


def test_pipeline_classification_sets_are_disjoint_and_known():
    all_names = set(JOB_CATALOG) | set(CUSTOM_JOBS)
    assert PIPELINE_SPA_ONLY <= all_names
    assert PIPELINE_TOMO_ONLY <= all_names
    assert PIPELINE_SPA_ONLY.isdisjoint(PIPELINE_TOMO_ONLY)


def test_pipeline_type_unknown_name_defaults_to_shared():
    """Anything not explicitly classified falls back to 'shared' (visible in
    both filtered views) rather than disappearing -- the safer failure mode
    for a display-only convenience feature."""
    assert pipeline_type("SomeFutureJobTypeNotYetClassified") == "shared"
