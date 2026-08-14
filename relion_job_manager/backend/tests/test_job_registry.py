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
from job_catalog import CUSTOM_JOBS, JOB_CATALOG

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


def test_catalog_lists_all_relion_and_custom_jobs():
    catalog = job_registry.list_catalog()
    names = {j["internal_name"] for j in catalog}
    assert names == set(JOB_CATALOG.keys()) | set(CUSTOM_JOBS.keys())
    n_custom = sum(1 for j in catalog if j["is_custom"])
    assert n_custom == len(CUSTOM_JOBS)
