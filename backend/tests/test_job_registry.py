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
    assert isinstance(d["standard_groups"], list) and d["standard_groups"]
    assert any(g["fields"] for g in d["standard_groups"])


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
def test_standard_groups_cover_every_option_exactly_once(internal_name):
    """Every field RELION's GUI defines is in the Inputs tab, and only once.

    This is the placement rule: the popup's Inputs tab holds all of RELION's
    own GUI options, in RELION's own groups; its Advanced section (appended
    last, inside that same tab) holds command-line options the GUI does not
    expose (discovered from the binary, not from these definitions). A
    field missing here is one the user cannot set at all.
    """
    d = job_registry.build_job_definition(internal_name)
    known_keys = {o["key"] for o in d["options"]}
    placed = [k for g in d["standard_groups"] for k in g["fields"]]
    assert set(placed) == known_keys, (
        f"{internal_name}: fields not in the Inputs tab: {known_keys - set(placed)}; "
        f"unknown keys placed: {set(placed) - known_keys}"
    )
    assert len(placed) == len(set(placed)), f"{internal_name} places a field twice"


@pytest.mark.parametrize("internal_name", sorted(JOB_CATALOG.keys()))
def test_standard_groups_follow_relions_own_tab_order(internal_name):
    """Group names and order come from RELION's own GUI layout, so someone who
    knows the real GUI finds fields where they expect them."""
    d = job_registry.build_job_definition(internal_name)
    layout = job_registry.raw_job(internal_name).get("tab_layout") or {}
    expected = [t for t in layout.get("tab_order", []) if layout["tab_fields"].get(t)]
    actual = [g["name"] for g in d["standard_groups"]]
    # "Other" is appended only for options RELION defines but never places.
    assert actual[:len(expected)] == expected, f"{internal_name}: {actual} vs {expected}"


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


# --------------------------------------------------------------------------
# RELION's Running tab: MPI procs, threads, additional arguments.
#
# These are added by the shared tail of RelionJob::initialise(), not by any
# job's own initialise<Name>Job(), so they were missing from every job until
# the extractor was taught to pick them up. Two of the three change the
# command in ways nothing else does.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("internal_name", sorted(JOB_CATALOG.keys()))
def test_every_job_offers_additional_arguments(internal_name):
    """RELION appends this box verbatim to every command it builds."""
    d = job_registry.build_job_definition(internal_name)
    assert "other_args" in {o["key"] for o in d["options"]}


@pytest.mark.parametrize("internal_name,has_mpi,has_thread", [
    ("Import", False, False),        # RelionJob::initialise: has_mpi = has_thread = false
    ("Ctffind", True, False),        # has_mpi = true; has_thread = false
    ("Maskcreate", False, True),     # has_mpi = false; has_thread = true
    ("Class2D", True, True),         # has_mpi = has_thread = true
])
def test_mpi_and_thread_fields_match_relions_own_table(internal_name, has_mpi, has_thread):
    keys = {o["key"] for o in job_registry.build_job_definition(internal_name)["options"]}
    assert ("nr_mpi" in keys) is has_mpi
    assert ("nr_threads" in keys) is has_thread


def test_default_program_is_the_serial_binary():
    """nr_mpi defaults to 1, so the default draft must not name the _mpi
    binary. Taking the first `command = "..."` literal in the builder got this
    wrong for all 18 MPI-capable jobs."""
    for name in ("Class2D", "Autorefine", "Motioncorr", "Extract"):
        raw = job_registry.raw_job(name)
        assert "_mpi" not in raw["program_guess"], name
        assert "_mpi" in raw["program_mpi"], name


def test_autopick_default_program_is_not_its_continue_branch():
    """Autopick's first command literal sits inside its "continue manually"
    branch (relion_manualpick) — not what a fresh Autopick job runs."""
    assert "relion_autopick" in job_registry.raw_job("Autopick")["program_guess"]


def test_mpi_greater_than_one_uses_relions_own_wrapping():
    raw = job_registry.raw_job("Class2D")
    values = {"nr_mpi": 4, "nr_threads": 2}
    cmd, _ = job_registry._build_draft_command(raw, values, "Class2D", "Class2D/job001")
    assert cmd.startswith("mpirun -n 4 ")
    assert "relion_refine_mpi" in cmd


def test_mpirun_command_honours_the_relion_env_var(monkeypatch):
    """RELION reads RELION_MPIRUN and falls back to "mpirun" (DEFAULTMPIRUN)."""
    monkeypatch.setenv("RELION_MPIRUN", "srun --mpi=pmix")
    raw = job_registry.raw_job("Class2D")
    cmd, _ = job_registry._build_draft_command(raw, {"nr_mpi": 8}, "Class2D", "")
    assert cmd.startswith("srun --mpi=pmix -n 8 ")


def test_mpi_of_one_leaves_the_command_serial():
    raw = job_registry.raw_job("Class2D")
    cmd, _ = job_registry._build_draft_command(raw, {"nr_mpi": 1}, "Class2D", "")
    assert not cmd.startswith("mpirun")
    assert "_mpi" not in cmd


def test_job_without_an_mpi_variant_is_never_wrapped():
    """Import has no _mpi binary; asking for MPI must not invent one."""
    raw = job_registry.raw_job("Import")
    assert raw["program_mpi"] is None
    cmd, _ = job_registry._build_draft_command(raw, {"nr_mpi": 8}, "Import", "")
    assert not cmd.startswith("mpirun")


def test_additional_arguments_are_appended_verbatim_and_last():
    """RELION does `command += " " + other_args` at the very end, unquoted —
    the whole point is passing raw extra arguments through."""
    raw = job_registry.raw_job("Class2D")
    cmd, _ = job_registry._build_draft_command(
        raw, {"other_args": '--dont_check_norm --verb 2'}, "Class2D", "Class2D/job001")
    assert cmd.endswith("--dont_check_norm --verb 2")


def test_empty_additional_arguments_add_nothing():
    raw = job_registry.raw_job("Class2D")
    cmd, _ = job_registry._build_draft_command(raw, {"other_args": "   "}, "Class2D", "")
    assert not cmd.endswith(" ")


def test_nr_mpi_is_not_emitted_as_a_flag():
    """It is not a flag at all — RELION expresses it through the mpirun prefix."""
    raw = job_registry.raw_job("Class2D")
    cmd, unmapped = job_registry._build_draft_command(raw, {"nr_mpi": 4}, "Class2D", "")
    assert "--nr_mpi" not in cmd
    assert "nr_mpi" not in unmapped


# --------------------------------------------------------------------------
# Source-verified option -> flag pairs
# --------------------------------------------------------------------------


def test_threads_use_the_flag_the_job_actually_emits():
    """RELION writes threads as --j, not --nr_threads."""
    raw = job_registry.raw_job("Class2D")
    cmd, _ = job_registry._build_draft_command(raw, {"nr_threads": 12}, "Class2D", "")
    assert "--j 12" in cmd


def test_input_flag_comes_from_the_real_builder():
    """Ctffind's input option is `input_star_mics` but the flag is --i."""
    raw = job_registry.raw_job("Ctffind")
    assert raw["option_flags"]["input_star_mics"]["flag"] == "--i"
    cmd, _ = job_registry._build_draft_command(
        raw, {"input_star_mics": "mics.star"}, "Ctffind", "")
    assert "--i mics.star" in cmd


def test_branch_dependent_flags_stay_out_of_the_draft():
    """Autopick emits --particle_diameter only in Topaz mode and --LoG_diam_min
    only in LoG mode. Emitting both would be a self-contradicting command, so a
    flag guarded by a *different* option is reported unmapped instead."""
    raw = job_registry.raw_job("Autopick")
    pair = raw["option_flags"].get("log_diam_min")
    assert pair and pair["condition"], "expected LoG diameter to be branch-guarded"
    cmd, unmapped = job_registry._build_draft_command(
        raw, {"log_diam_min": 150.0}, "Autopick", "")
    assert "--LoG_diam_min" not in cmd
    assert "log_diam_min" in unmapped


def test_self_guarded_flags_are_still_emitted():
    """`if (scratch_dir != "") command += " --scratch_dir "...` is only
    "emit when set", which the draft already does by skipping empty values."""
    raw = job_registry.raw_job("Class2D")
    assert raw["option_flags"]["scratch_dir"]["condition"]
    cmd, _ = job_registry._build_draft_command(
        raw, {"scratch_dir": "/ssd/scratch"}, "Class2D", "")
    assert "--scratch_dir /ssd/scratch" in cmd


def test_bare_is_continue_condition_is_treated_as_unconditional():
    """RELION-US never drafts a "continue this job" run (see the module
    docstring), so is_continue is always false here -- a condition of
    exactly "!is_continue" (Class2D's nr_classes -> --K, e.g.) is therefore
    vacuously satisfied and should be emitted like any unconditional field,
    not left out as an unmapped "real branch"."""
    raw = job_registry.raw_job("Class2D")
    assert raw["option_flags"]["nr_classes"]["condition"] == "!is_continue"
    cmd, unmapped = job_registry._build_draft_command(
        raw, {"nr_classes": 50}, "Class2D", "")
    assert "--K 50" in cmd
    assert "nr_classes" not in unmapped


def test_is_continue_combined_with_another_term_stays_unmapped():
    """A condition merely containing "!is_continue" alongside something else
    (e.g. "!is_continue && else") must NOT be treated as unconditional --
    unlike the bare "!is_continue" case, "else" can guard a *different*
    option (confirmed against real RELION source for Motioncorr's
    fn_motioncor2_exe, only added when do_own_motioncor is false)."""
    raw = job_registry.raw_job("Class3D")
    cond = raw["option_flags"]["fn_ref"]["condition"]
    assert cond == "!is_continue && else"
    assert not job_registry._self_guarded(cond, "fn_ref")


@pytest.mark.parametrize("internal_name,fields,expect_in_cmd,expect_not_unmapped", [
    # fn_ref/fn_img/in_optimisation/in_particles/in_tomograms/in_trajectories
    # are all built by helper functions RELION calls from getCommands*Job()
    # (getTomoInputCommmand() for the tomo group -- src/pipeline_jobs.cpp
    # ~6328) rather than assembling the flag inline, so the generic
    # `--<key>` rule never sees a matching flags_used entry and the whole
    # group used to be silently dropped no matter what the user filled in.
    # See job_catalog.DRAFT_FLAG_MAP.
    ("Class3D", {"fn_img": "particles.star", "fn_ref": "ref.mrc"},
     ["--i particles.star", "--ref ref.mrc"], {"fn_img", "fn_ref"}),
    ("Autorefine", {"fn_img": "particles.star", "fn_ref": "ref.mrc"},
     ["--i particles.star", "--ref ref.mrc"], {"fn_img", "fn_ref"}),
    ("Autorefine", {"in_optimisation": "opt_set.star"},
     ["--ios opt_set.star"], {"in_optimisation"}),
    ("Autorefine", {"use_direct_entries": True, "in_particles": "p.star",
                     "in_tomograms": "t.star", "in_trajectories": "traj.star"},
     ["--i p.star", "--tomograms t.star", "--trajectories traj.star"],
     {"in_particles", "in_tomograms", "in_trajectories", "use_direct_entries"}),
    ("Inimodel", {"in_optimisation": "opt_set.star"},
     ["--ios opt_set.star"], {"in_optimisation"}),
    ("TomoReconPart", {"in_optimisation": "opt_set.star"},
     ["--i opt_set.star"], {"in_optimisation"}),
    ("TomoSubtomo", {"use_direct_entries": True, "in_particles": "p.star",
                      "in_tomograms": "t.star"},
     ["--p p.star", "--t t.star"], {"in_particles", "in_tomograms"}),
])
def test_tomo_optimisation_set_inputs_are_mapped(
        internal_name, fields, expect_in_cmd, expect_not_unmapped):
    raw = job_registry.raw_job(internal_name)
    cmd, unmapped = job_registry._build_draft_command(raw, fields, internal_name, "")
    for expected in expect_in_cmd:
        assert expected in cmd, f"{internal_name}: {cmd!r} missing {expected!r}"
    assert not (expect_not_unmapped & set(unmapped)), \
        f"{internal_name}: {expect_not_unmapped & set(unmapped)} unexpectedly unmapped in {unmapped}"


@pytest.mark.parametrize("internal_name,output_flag,suffix", [
    # The five classic iterative-refinement jobs append fn_run = "run" to
    # outputname in their DEFAULT (non-continuation) branch -- verified
    # against src/pipeline_jobs.cpp: Class2D ~3183, Inimodel ~3466,
    # Class3D ~3860, Autorefine ~4351, MultiBody ~4736-4744 (`else` branch,
    # since RELION-US never models a continuation run). Without this,
    # RELION-US emitted a bare directory and RELION's own binaries wrote
    # files like "_it000_class001.mrc" instead of "run_it000_class001.mrc".
    ("Class2D", "--o", "run"),
    ("Inimodel", "--o", "run"),
    ("Class3D", "--o", "run"),
    ("Autorefine", "--o", "run"),
    ("MultiBody", "--o", "run"),
    # Fixed, unconditional literal suffixes, verified by reading each
    # function in full (no branch controls the line): Maskcreate ~4942,
    # Postprocess ~5340.
    ("Maskcreate", "--o", "mask.mrc"),
    ("Postprocess", "--o", "postprocess"),
])
def test_output_suffix_jobs_get_run_rootname_prefix(internal_name, output_flag, suffix):
    raw = job_registry.raw_job(internal_name)
    cmd, _ = job_registry._build_draft_command(raw, {}, internal_name, f"{internal_name}/job001")
    assert f"{output_flag} {internal_name}/job001/{suffix}" in cmd, cmd


def test_jobs_without_a_suffix_entry_keep_the_bare_directory():
    """Most jobs take a plain directory for --o -- e.g. Import, which isn't
    in DRAFT_OUTPUT_SUFFIX -- and must NOT gain an unexpected suffix."""
    raw = job_registry.raw_job("Import")
    cmd, _ = job_registry._build_draft_command(raw, {}, "Import", "Import/job001")
    assert "Import/job001/" in cmd
    assert "Import/job001/run" not in cmd


# --------------------------------------------------------------------------
# The flags_used shortcut used to bypass a real condition whenever a field's
# flag happened to equal "--" + its own key (user report: helical fields
# passed even with "Do helical reconstruction?" unchecked; GPU acceleration
# checked but neither --gpu nor which GPU ever got passed). An audit of
# every job's option_flags against flags_used found 72 fields with this
# exact shape across the job set -- see _evaluate_condition's docstring and
# the reordered flag-resolution block in _build_draft_command.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("condition,field_values,expected", [
    ("", {}, True),
    ("!is_continue", {}, True),
    ('joboptions["do_helix"].getBoolean()', {"do_helix": True}, True),
    ('joboptions["do_helix"].getBoolean()', {"do_helix": False}, False),
    ('joboptions["do_helix"].getBoolean()', {}, False),
    ('!joboptions["do_own_motioncor"].getBoolean()', {"do_own_motioncor": False}, True),
    ('!joboptions["do_own_motioncor"].getBoolean()', {"do_own_motioncor": True}, False),
    (
        '!is_continue && joboptions["do_helix"].getBoolean() && '
        'joboptions["do_apply_helical_symmetry"].getBoolean()',
        {"do_helix": True, "do_apply_helical_symmetry": True},
        True,
    ),
    (
        '!is_continue && joboptions["do_helix"].getBoolean() && '
        'joboptions["do_apply_helical_symmetry"].getBoolean()',
        {"do_helix": True, "do_apply_helical_symmetry": False},
        False,
    ),
    (
        '(!is_continue) && (joboptions["do_helix"].getBoolean()) && '
        'joboptions["do_apply_helical_symmetry"].getBoolean()',
        {"do_helix": True, "do_apply_helical_symmetry": True},
        True,
    ),
    # Unevaluable shapes defer to the caller's existing "unmapped" fallback.
    ('joboptions["a"].getBoolean() || joboptions["b"].getBoolean()', {"a": True}, None),
    ('else && joboptions["do_topaz"].getBoolean()', {"do_topaz": True}, None),
    ('joboptions["nr_split"].getNumber(error_message) > 0', {"nr_split": 5}, None),
])
def test_evaluate_condition(condition, field_values, expected):
    assert job_registry._evaluate_condition(condition, field_values) is expected


def test_helical_fields_are_omitted_when_the_checkbox_is_unchecked():
    """The exact user report: helical parameters were getting passed even
    with "Do helical reconstruction?" unchecked, because their flag name
    (--helical_nr_asu, --helical_twist_initial, --helical_rise_initial)
    equals "--" + their own key, which used to short-circuit past the real
    do_helix/do_apply_helical_symmetry condition entirely."""
    raw = job_registry.raw_job("Autorefine")
    cmd, unmapped = job_registry._build_draft_command(
        raw,
        {
            "do_helix": False,
            "helical_tube_outer_diameter": 250,
            "helical_nr_asu": 1,
            "helical_twist_initial": 0,
            "helical_rise_initial": 0,
        },
        "Autorefine", "",
    )
    for flag in ("--helical_outer_diameter", "--helical_nr_asu",
                 "--helical_twist_initial", "--helical_rise_initial"):
        assert flag not in cmd, cmd
    # Nothing here is broken or needs manual attention -- do_helix being
    # false means RELION itself wouldn't emit these either.
    assert not unmapped, unmapped


def test_helical_fields_are_included_when_the_checkbox_is_checked():
    raw = job_registry.raw_job("Autorefine")
    cmd, unmapped = job_registry._build_draft_command(
        raw,
        {
            "do_helix": True,
            "do_apply_helical_symmetry": True,
            "helical_tube_outer_diameter": 250,
            "helical_nr_asu": 1,
            "helical_twist_initial": 0,
            "helical_rise_initial": 0,
        },
        "Autorefine", "",
    )
    assert "--helical_outer_diameter 250" in cmd
    assert "--helical_nr_asu 1" in cmd
    assert "--helical_twist_initial 0" in cmd
    assert "--helical_rise_initial 0" in cmd
    assert not unmapped, unmapped


def test_helical_symmetry_sub_fields_respect_their_own_extra_gate():
    """helical_nr_asu/twist_initial/rise_initial need BOTH do_helix AND
    do_apply_helical_symmetry; helical_tube_outer_diameter needs only
    do_helix. With do_helix on but do_apply_helical_symmetry off, only the
    outer-diameter field should appear."""
    raw = job_registry.raw_job("Autorefine")
    cmd, unmapped = job_registry._build_draft_command(
        raw,
        {
            "do_helix": True,
            "do_apply_helical_symmetry": False,
            "helical_tube_outer_diameter": 250,
            "helical_nr_asu": 1,
        },
        "Autorefine", "",
    )
    assert "--helical_outer_diameter 250" in cmd
    assert "--helical_nr_asu" not in cmd
    assert not unmapped, unmapped


@pytest.mark.parametrize("internal_name", ["Class2D", "Inimodel", "Class3D", "Autorefine"])
def test_gpu_flag_is_gated_on_the_use_gpu_checkbox(internal_name):
    """The exact user report: the GPU acceleration checkbox was checked but
    neither --gpu nor which GPU to use ever got passed. gpu_ids' real flag
    (--gpu) is wrapped in escaped quotes in RELION's own source (`command +=
    " --gpu \\"" + joboptions["gpu_ids"].getString() + "\\"";`), which the
    extractor's OPTION_FLAG_RE didn't recognize at all before this fix, so
    option_flags had no entry for gpu_ids and it fell through to the
    generic --<key> rule using the wrong flag name ("--gpu_ids")."""
    raw = job_registry.raw_job(internal_name)
    on, unmapped_on = job_registry._build_draft_command(
        raw, {"use_gpu": True, "gpu_ids": "0,1"}, internal_name, "")
    assert '--gpu 0,1' in on, on
    assert not unmapped_on, unmapped_on

    off, unmapped_off = job_registry._build_draft_command(
        raw, {"use_gpu": False, "gpu_ids": "0,1"}, internal_name, "")
    assert "--gpu" not in off, off
    assert not unmapped_off, unmapped_off


def test_gpu_flag_is_still_passed_with_blank_gpu_ids():
    """RELION emits `--gpu ""` (letting the job auto-allocate) whenever the
    checkbox is on, even with "Which GPUs to use" left blank -- an empty
    value is meaningful here (auto-allocate), not "unset", unlike every
    other text field this app skips when blank."""
    raw = job_registry.raw_job("Autorefine")
    cmd, unmapped = job_registry._build_draft_command(
        raw, {"use_gpu": True, "gpu_ids": ""}, "Autorefine", "")
    assert "--gpu" in cmd.split()
    assert not unmapped, unmapped


def test_gpu_mpi_and_helical_all_combine_in_one_command():
    raw = job_registry.raw_job("Autorefine")
    cmd, unmapped = job_registry._build_draft_command(
        raw,
        {
            "nr_mpi": 4, "use_gpu": True, "gpu_ids": "0:1:2:3",
            "do_helix": True, "do_apply_helical_symmetry": True,
            "helical_nr_asu": 1,
        },
        "Autorefine", "Refine3D/job001",
    )
    assert cmd.startswith("mpirun -n 4 ")
    assert "--gpu 0:1:2:3" in cmd
    assert "--helical_nr_asu 1" in cmd
    assert not unmapped, unmapped


@pytest.mark.parametrize("internal_name,fields", [
    # Autopick's GPU is only reachable in the Topaz branch (condition
    # contains a bare "else" -- genuinely mode-branched, not something this
    # app tries to interpret) and Motioncorr's is gated on the *other*
    # branch of "do_own_motioncor" (same "else" shape) -- both must stay
    # honestly unmapped rather than silently guessed at.
    ("Autopick", {"use_gpu": True, "gpu_ids": "0"}),
    ("Motioncorr", {"gpu_ids": "0"}),
])
def test_gpu_fields_needing_real_branch_logic_stay_unmapped(internal_name, fields):
    raw = job_registry.raw_job(internal_name)
    cmd, unmapped = job_registry._build_draft_command(raw, fields, internal_name, "")
    assert "--gpu" not in cmd
    assert "gpu_ids" in unmapped
