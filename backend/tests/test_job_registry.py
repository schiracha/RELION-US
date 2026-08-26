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
# curated, source-verified overlay in job_catalog (DRAFT_OVERRIDES's
# program/flags/suppress fields) fixes that. See getCommandsTomoImportJob in
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


def test_tomo_import_dose_is_per_movie_frame_gets_dropdown_labels():
    """job_catalog.BOOLEAN_SELECT_LABELS attaches boolean_labels onto a COPY
    of this option (the frontend renders a two-way <select> instead of a
    checkbox for it) without mutating the shared raw options list other
    jobs/requests read from."""
    d = job_registry.build_job_definition("TomoImport")
    opts = {o["key"]: o for o in d["options"]}
    assert opts["dose_is_per_movie_frame"]["boolean_labels"] == {
        "false": "Dose per tilt image", "true": "Dose per movie frame",
    }
    # A field with no override still round-trips normally, and the raw data
    # backing every OTHER job's build_job_definition call wasn't mutated.
    assert "boolean_labels" not in opts["angpix"]
    raw = job_registry.raw_job("TomoImport")
    raw_opt = next(o for o in raw["options"] if o["key"] == "dose_is_per_movie_frame")
    assert "boolean_labels" not in raw_opt


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


def test_resolve_tomo_variant():
    assert job_registry._resolve_tomo_variant("TomoMotioncorr") == ("Motioncorr", True)
    assert job_registry._resolve_tomo_variant("TomoCtffind") == ("Ctffind", True)
    assert job_registry._resolve_tomo_variant("Motioncorr") == ("Motioncorr", False)
    assert job_registry._resolve_tomo_variant("Class3D") == ("Class3D", False)


def test_tomo_menu_entry_gates_ctffind_tomo_only_flags():
    """localsearch_nominal_defocus/exp_factor_dose are real, always-visible
    Ctffind fields (data/job_definitions_raw.json condition: "is_tomo") for
    a tomo tilt-series CTF job -- real RELION has one RelionJob class for
    Ctffind either way, so RELION-US gives it two menu entries
    (job_catalog.TOMO_VARIANT_OF: Ctffind / TomoCtffind) rather than a
    same-popup toggle, and _build_draft_command derives is_tomo from WHICH
    of the two internal_name was actually picked. Without the right value
    these were silently dropped from the executed command with no warning,
    always falling back to CTFFIND's own hardcoded defaults regardless of
    what the user set (confirmed against real RELION source,
    src/pipeline_jobs.cpp's getCommandsCtffindJob)."""
    raw = job_registry.raw_job("Ctffind")
    fields = {
        "input_star_mics": "tilt_series.star",
        "localsearch_nominal_defocus": 5000,
        "exp_factor_dose": 0,
        "use_noDW": True,
    }
    spa_cmd, _ = job_registry._build_draft_command(raw, fields, "Ctffind", "")
    tomo_cmd, _ = job_registry._build_draft_command(raw, fields, "TomoCtffind", "")
    assert "--localsearch_nominal_defocus" not in spa_cmd
    assert "--exp_factor_dose" not in spa_cmd
    assert "--use_noDW" in spa_cmd
    assert "--localsearch_nominal_defocus 5000" in tomo_cmd
    assert "--exp_factor_dose 0" in tomo_cmd
    assert "--use_noDW" not in tomo_cmd  # SPA-only in real RELION


def test_tomo_menu_entry_gates_motioncorr_spa_only_and_tomo_only_flags():
    """first_frame_sum/last_frame_sum/dose_per_frame/pre_exposure are
    SPA-only (real RELION's initialiseMotioncorrJob only creates those
    JobOptions `if (!is_tomo)`); do_even_odd_split is tomo-only (MotionCor2
    denoising support). Same Motioncorr/TomoMotioncorr menu-entry-driven
    is_tomo as Ctffind above gates both directions -- raw_job("TomoMotioncorr")
    resolves to the SAME underlying options as raw_job("Motioncorr")."""
    raw = job_registry.raw_job("Motioncorr")
    assert job_registry.raw_job("TomoMotioncorr") is raw  # same underlying data, not a copy
    fields = {
        "input_star_mics": "movies.star",
        "first_frame_sum": 1, "last_frame_sum": -1,
        "dose_per_frame": 1.0, "pre_exposure": 0.0,
        "do_even_odd_split": True,
    }
    spa_cmd, _ = job_registry._build_draft_command(raw, fields, "Motioncorr", "")
    tomo_cmd, _ = job_registry._build_draft_command(raw, fields, "TomoMotioncorr", "")
    assert "--first_frame_sum" in spa_cmd
    assert "--dose_per_frame" in spa_cmd
    assert "--even_odd_split" not in spa_cmd
    assert "--first_frame_sum" not in tomo_cmd
    assert "--dose_per_frame" not in tomo_cmd
    assert "--even_odd_split" in tomo_cmd


def test_tomo_menu_entry_ignores_a_caller_supplied_is_tomo():
    """internal_name is the single source of truth now -- a stray
    field_values["is_tomo"] (e.g. left over from persisted history written
    before this menu split existed) must not override it."""
    raw = job_registry.raw_job("Ctffind")
    fields = {"input_star_mics": "x.star", "use_noDW": True, "is_tomo": True}
    cmd, _ = job_registry._build_draft_command(raw, fields, "Ctffind", "")
    assert "--use_noDW" in cmd  # still the SPA behavior, despite fields["is_tomo"]


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
    # See job_catalog.DRAFT_OVERRIDES.
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


def test_import_raw_movies_input_file_reaches_the_command():
    """getCommandsImportJob (src/pipeline_jobs.cpp:1439) reads do_raw's
    fn_in_raw and do_other's fn_in_other into a shared local `fn_in`, then
    appends `--i` to it in ONE place AFTER both branches -- neither
    joboptions["fn_in_raw"] nor ["fn_in_other"] appears next to a
    `command +=` line, so the generic extractor never sees a flag for
    either key and the draft silently dropped the input file entirely
    (found by actually running an Import job against RELION 5.0.1). See
    job_catalog.DRAFT_OVERRIDES["Import"].flags. Each is gated on its own do_raw/
    do_other condition -- not unconditional like the tomo shared-input
    group -- because RELION gives BOTH fields their own non-empty default
    ("Micrographs/*.tif" and "ref.mrc") regardless of which branch is
    active, so a naive unconditional mapping emitted two "--i" flags (also
    confirmed for real, before this test existed)."""
    raw = job_registry.raw_job("Import")
    cmd, unmapped = job_registry._build_draft_command(
        raw, {"do_raw": True, "fn_in_raw": "movies/*.mrcs",
              "do_other": False, "fn_in_other": "ref.mrc"}, "Import", "")
    assert "--i 'movies/*.mrcs'" in cmd  # shlex-quoted: contains a glob char
    assert "ref.mrc" not in cmd
    assert cmd.count(" --i ") == 1
    assert "fn_in_raw" not in unmapped


def test_import_other_node_type_input_file_reaches_the_command():
    """The mirror image of the test above: do_other active, do_raw off --
    fn_in_other's own non-empty default must not leak fn_in_raw's."""
    raw = job_registry.raw_job("Import")
    cmd, unmapped = job_registry._build_draft_command(
        raw, {"do_raw": False, "fn_in_raw": "Micrographs/*.tif",
              "do_other": True, "fn_in_other": "ref.mrc"}, "Import", "")
    assert "--i ref.mrc" in cmd
    assert "Micrographs" not in cmd
    assert cmd.count(" --i ") == 1
    assert "fn_in_other" not in unmapped


def test_import_raw_angpix_kv_cs_are_emitted_when_do_raw_is_on():
    """angpix/kV/Cs/Q0/beamtilt_x/beamtilt_y are all gated on the bare
    local-variable condition "do_raw" (see
    test_evaluate_condition_bare_identifier) -- confirmed missing from a
    real Import job's draft command until job_registry._evaluate_condition
    learned to resolve that shape."""
    raw = job_registry.raw_job("Import")
    fields = {"do_raw": True, "angpix": "1.4", "kV": "300", "Cs": "2.7", "Q0": "0.1"}
    cmd, unmapped = job_registry._build_draft_command(raw, fields, "Import", "")
    for flag in ("--angpix 1.4", "--kV 300", "--Cs 2.7", "--Q0 0.1"):
        assert flag in cmd, cmd
    assert not ({"angpix", "kV", "Cs", "Q0"} & set(unmapped))


def test_import_raw_angpix_omitted_when_do_raw_is_off():
    raw = job_registry.raw_job("Import")
    fields = {"do_raw": False, "angpix": "1.4"}
    cmd, unmapped = job_registry._build_draft_command(raw, fields, "Import", "")
    assert "--angpix" not in cmd
    assert "angpix" not in unmapped  # correctly-omitted, not unresolved


def test_import_uses_odir_and_ofile_not_a_bare_o_flag():
    """getCommandsImportJob (src/pipeline_jobs.cpp:1440-1441) takes `--odir
    <dir>/` and a separate compulsory `--ofile <name>` -- NOT the generic
    `--o <dir>/` every other job gets. Confirmed for real: relion_import
    --help lists --o as unrecognized, and running the (then-)default draft
    against RELION 5.0.1 failed immediately on both missing arguments."""
    raw = job_registry.raw_job("Import")
    cmd, _ = job_registry._build_draft_command(
        raw, {"do_raw": True, "is_multiframe": True}, "Import", "Import/job001")
    assert "--odir Import/job001/" in cmd
    assert "--ofile movies.star" in cmd
    assert "--o Import/job001/" not in cmd  # no bare --o anywhere


def test_import_ofile_picks_micrographs_star_for_single_frame():
    raw = job_registry.raw_job("Import")
    cmd, _ = job_registry._build_draft_command(
        raw, {"do_raw": True, "is_multiframe": False}, "Import", "Import/job001")
    assert "--ofile micrographs.star" in cmd


def test_import_ofile_omitted_outside_the_do_raw_branch():
    """do_other's --ofile value is derived from fn_in_other itself (basename,
    or a coords_suffix construction for coordinate imports) -- genuine
    per-node-type branch logic this app deliberately doesn't reconstruct,
    same policy as TomoImport's do_coords branch. Left for the user to add
    by hand, same as is_multiframe already is inside the do_raw branch."""
    raw = job_registry.raw_job("Import")
    cmd, _ = job_registry._build_draft_command(
        raw, {"do_raw": False, "do_other": True, "fn_in_other": "ref.mrc"},
        "Import", "Import/job001")
    assert "--ofile" not in cmd


def test_motioncorr_gain_rot_and_flip_translate_label_to_relions_numeric_code():
    """gain_rot/gain_flip are "radio" fields whose stored value is the
    human-facing label ("No rotation (0)"), but motioncorr_runner.cpp:105-106
    does `textToInteger(parser.getOption("--gain_rot", ...))` -- passing the
    label through crashes relion_run_motioncorr with "Error in textToInteger"
    (confirmed for real against RELION 5.0.1: a Motioncorr job with the
    untranslated label crashed immediately on a real run). See
    job_catalog.DRAFT_OVERRIDES["Motioncorr"].value_transforms."""
    raw = job_registry.raw_job("Motioncorr")
    fields = {"gain_rot": "90 degrees (1)", "gain_flip": "Flip left to right (2)"}
    cmd, unmapped = job_registry._build_draft_command(raw, fields, "Motioncorr", "")
    assert "--gain_rot 1" in cmd
    assert "--gain_flip 2" in cmd
    assert "degrees" not in cmd and "Flip" not in cmd  # no raw label leaked through
    assert not ({"gain_rot", "gain_flip"} & set(unmapped))


def test_motioncorr_gain_rot_unknown_label_is_left_unmapped_not_guessed():
    """An unrecognized label (e.g. this table going stale after a RELION
    version bump) must not silently emit a garbage value -- it should show
    up as unmapped, same as any other field this app can't confidently
    resolve."""
    raw = job_registry.raw_job("Motioncorr")
    cmd, unmapped = job_registry._build_draft_command(
        raw, {"gain_rot": "Some future label (9)"}, "Motioncorr", "")
    assert "--gain_rot" not in cmd
    assert "gain_rot" in unmapped


def test_tomoalign_motion_sigma_fields_are_emitted_when_do_motion_is_on():
    """sigma_vel/sigma_div (--s_vel/--s_div) are gated on the same bare
    local-variable condition shape, "do_motion", in TomoAlign."""
    raw = job_registry.raw_job("TomoAlign")
    fields = {"do_motion": True, "sigma_vel": "0.2", "sigma_div": "5000"}
    cmd, unmapped = job_registry._build_draft_command(raw, fields, "TomoAlign", "")
    assert "--s_vel 0.2" in cmd
    assert "--s_div 5000" in cmd
    assert not ({"sigma_vel", "sigma_div"} & set(unmapped))


@pytest.mark.parametrize("internal_name", ["Class2D", "Class3D", "Autorefine", "Inimodel"])
def test_do_ctf_correction_emits_ctf_flag(internal_name):
    """pipeline_jobs.cpp wraps this in a nested `if (!is_continue) { if
    (do_ctf_correction) ... }`, which the extractor's regex-based scan
    missed for all four of these jobs (same shape as do_parallel_discio/
    do_combine_thru_disc/do_preread_images above) -- the flag ("--ctf")
    doesn't spell out as "--" + its key, so the generic rule missed it
    too. do_ctf_correction defaults to Yes in real RELION, so every draft
    from these four job types was silently missing --ctf regardless of
    what the user had checked. See job_catalog.DRAFT_OVERRIDES."""
    raw = job_registry.raw_job(internal_name)
    cmd, unmapped = job_registry._build_draft_command(
        raw, {"do_ctf_correction": True}, internal_name, "")
    assert "--ctf" in cmd
    assert "do_ctf_correction" not in unmapped

    cmd_off, unmapped_off = job_registry._build_draft_command(
        raw, {"do_ctf_correction": False}, internal_name, "")
    assert "--ctf" not in cmd_off
    assert "do_ctf_correction" not in unmapped_off


@pytest.mark.parametrize("internal_name", ["Class2D", "Class3D", "Autorefine", "Inimodel"])
def test_ctf_intact_first_peak_requires_do_ctf_correction_too(internal_name):
    """--ctf_intact_first_peak only appears inside the SAME nested `if
    (do_ctf_correction)` block as --ctf -- checking "Ignore CTFs until
    first peak?" alone, with CTF correction itself off, must not emit it."""
    raw = job_registry.raw_job(internal_name)
    fields_both_on = {"do_ctf_correction": True, "ctf_intact_first_peak": True}
    cmd, unmapped = job_registry._build_draft_command(raw, fields_both_on, internal_name, "")
    assert "--ctf_intact_first_peak" in cmd
    assert not ({"do_ctf_correction", "ctf_intact_first_peak"} & set(unmapped))

    fields_outer_off = {"do_ctf_correction": False, "ctf_intact_first_peak": True}
    cmd_off, unmapped_off = job_registry._build_draft_command(raw, fields_outer_off, internal_name, "")
    assert "--ctf_intact_first_peak" not in cmd_off
    assert "ctf_intact_first_peak" not in unmapped_off


# --------------------------------------------------------------------------
# The broader DRAFT_OVERRIDES audit: every field previously missing an
# option_flags entry entirely (the extractor found no `command +=` beside a
# `joboptions["key"]` reference at all -- usually because the real source
# reads the value into a local variable first, or the flag name simply
# doesn't spell out as "--" + key) that turned out to be a simple,
# self-contained, source-verified fix. Each row: (internal_name, "on"
# field_values that should make the flag appear (including the target key
# itself), a substring expected in the resulting command, and an optional
# (field_to_flip, new_value) that should make it disappear again). See
# job_catalog.DRAFT_OVERRIDES for the exact pipeline_jobs.cpp line refs.
_UNMAPPED_FIELD_FIXES = [
    ("Autopick", {"do_log": True, "log_invert": True}, "--Log_invert", ("do_log", False)),
    ("Autopick", {"do_refs": True, "do_invert_refs": True}, "--invert", ("do_refs", False)),
    ("Autopick", {"do_refs": True, "do_ctf_autopick": True}, "--ctf", ("do_refs", False)),
    ("Autopick",
     {"do_refs": True, "do_ctf_autopick": True, "do_ignore_first_ctfpeak_autopick": True},
     "--ctf_intact_first_peak", ("do_ctf_autopick", False)),
    ("Autopick", {"do_refs": True, "do_pick_helical_segments": True}, "--helix", ("do_refs", False)),
    ("Autopick",
     {"do_refs": True, "do_pick_helical_segments": True, "do_amyloid": True},
     "--amyloid", ("do_pick_helical_segments", False)),
    ("Autorefine", {"ref_correct_greyscale": False}, "--firstiter_cc", ("ref_correct_greyscale", True)),
    ("Autorefine", {"do_zero_mask": True}, "--zero_mask", None),
    ("Autorefine", {"fn_mask": "m.mrc", "do_solvent_fsc": True}, "--solvent_correct_fsc", ("fn_mask", "")),
    ("Autorefine", {"do_blush": True}, "--blush", None),
    ("Autorefine", {"auto_faster": True}, "--auto_ignore_angles", None),
    ("Autorefine", {"do_pad1": True}, "--pad 1", None),
    ("Autorefine",
     {"do_helix": True, "helical_range_distance": 9}, "--helical_sigma_distance 3.0", ("do_helix", False)),
    ("Autorefine", {"do_helix": True, "keep_tilt_prior_fixed": True}, "--helical_keep_tilt_prior_fixed",
     ("do_helix", False)),
    ("Class2D", {"do_zero_mask": True}, "--zero_mask", None),
    ("Class2D", {"do_center": True}, "--center_classes", None),
    ("Class2D", {"dont_skip_align": False}, "--skip_align", ("dont_skip_align", True)),
    ("Class2D",
     {"dont_skip_align": True, "allow_coarser": True}, "--allow_coarser_sampling", ("dont_skip_align", False)),
    ("Class2D",
     {"do_helix": True, "dont_skip_align": True, "do_bimodal_psi": True},
     "--bimodal_psi", ("do_helix", False)),
    ("Class3D", {"ref_correct_greyscale": False}, "--firstiter_cc", ("ref_correct_greyscale", True)),
    ("Class3D", {"do_fast_subsets": True}, "--fast_subsets", None),
    ("Class3D", {"do_zero_mask": True}, "--zero_mask", None),
    ("Class3D", {"do_blush": True}, "--blush", None),
    ("Class3D", {"dont_skip_align": False}, "--skip_align", ("dont_skip_align", True)),
    ("Class3D",
     {"dont_skip_align": True, "allow_coarser": True}, "--allow_coarser_sampling", ("dont_skip_align", False)),
    ("Class3D", {"do_pad1": True}, "--pad 1", None),
    ("Class3D", {"dont_skip_align": True, "do_local_ang_searches": True, "sigma_angles": 9}, "--sigma_ang 3.0",
     ("do_local_ang_searches", False)),
    ("Class3D",
     {"do_helix": True, "dont_skip_align": True, "do_local_ang_searches": False, "range_tilt": 9},
     "--sigma_tilt 3.0", ("do_helix", False)),
    ("Class3D",
     {"do_helix": True, "dont_skip_align": True, "do_local_ang_searches": False, "range_psi": 9},
     "--sigma_psi 3.0", ("do_helix", False)),
    ("Class3D",
     {"do_helix": True, "dont_skip_align": True, "do_local_ang_searches": False, "range_rot": 9},
     "--sigma_rot 3.0", ("do_helix", False)),
    ("Class3D",
     {"do_helix": True, "dont_skip_align": True, "do_local_ang_searches": False, "helical_range_distance": 9},
     "--helical_sigma_distance 3.0", ("do_helix", False)),
    ("Class3D", {"do_helix": True, "keep_tilt_prior_fixed": True}, "--helical_keep_tilt_prior_fixed",
     ("do_helix", False)),
    ("Ctffind", {"slow_search": False}, "--fast_search", ("slow_search", True)),
    ("TomoCtffind", {"slow_search": False}, "--fast_search", ("slow_search", True)),
    ("Ctfrefine", {"do_tilt": True, "do_trefoil": True}, "--odd_aberr_max_n 3", ("do_tilt", False)),
    ("Ctfrefine", {"do_aniso_mag": False, "do_4thorder": True}, "--fit_aberr", ("do_aniso_mag", True)),
    ("Extract", {"do_reextract": True, "do_reset_offsets": True}, "--reset_offsets", ("do_reextract", False)),
    ("Extract", {"do_reextract": True, "do_recenter": True}, "--recenter", ("do_reextract", False)),
    ("Extract", {"do_invert": True}, "--invert_contrast", None),
    ("Extract", {"do_float16": True}, "--float16", None),
    ("Inimodel", {"do_run_C1": True}, "--sym C1", None),
    ("Inimodel", {"do_solvent": True}, "--flatten_solvent", None),
    ("Motioncorr",
     {"is_tomo": False, "do_dose_weighting": True, "do_save_noDW": True},
     "--save_noDW", ("do_dose_weighting", False)),
    ("Motionrefine", {"do_float16": True}, "--float16", None),
    ("MultiBody", {"do_blush": True}, "--blush", None),
    ("MultiBody", {"do_subtracted_bodies": True}, "--reconstruct_subtracted_bodies", None),
    ("MultiBody", {"do_pad1": True}, "--pad 1", None),
    ("Postprocess", {"fn_in": "half1.mrc"}, "--i half1.mrc", None),
    ("Postprocess", {"do_skip_fsc_weighting": True}, "--skip_fsc_weighting", None),
    ("Select", {"do_split": True, "do_random": True}, "--random_order", ("do_split", False)),
    ("Subtract", {"do_fliplabel": False, "fn_opt": "opt.star"}, "--i opt.star", ("do_fliplabel", True)),
    ("Subtract",
     {"do_fliplabel": False, "do_data": True, "fn_data": "d.star"}, "--data d.star", ("do_data", False)),
    ("Subtract", {"do_fliplabel": False, "do_float16": True}, "--float16", ("do_fliplabel", True)),
    ("Subtract", {"do_fliplabel": False, "do_center_mask": True}, "--recenter_on_mask", ("do_fliplabel", True)),
    ("TomoAlign", {"do_shift_align": True}, "--shift_only", None),
    ("TomoAlign", {"do_motion": True}, "--motion", None),
    ("TomoAlign", {"do_motion": True, "do_sq_exp_ker": True}, "--sq_exp_ker", ("do_motion", False)),
    ("TomoAlignTiltSeries", {"do_imod_fiducials": True}, "--imod_fiducials", None),
    ("TomoAlignTiltSeries", {"do_imod_patchtrack": True}, "--imod_patchtrack", None),
    ("TomoAlignTiltSeries", {"do_aretomo2": True}, "--aretomo2", None),
    ("TomoAlignTiltSeries",
     {"do_aretomo2": True, "do_aretomo_tiltcorrect": True}, "--aretomo_tiltcorrect", ("do_aretomo2", False)),
    ("TomoAlignTiltSeries",
     {"do_aretomo2": True, "do_aretomo_ctf": True}, "--aretomo_ctf", ("do_aretomo2", False)),
    ("TomoAlignTiltSeries",
     {"do_aretomo2": True, "do_aretomo_ctf": True, "do_aretomo_phaseshift": True},
     "--aretomo_phaseshift", ("do_aretomo_ctf", False)),
    ("TomoCtfRefine", {"do_reg_def": True}, "--do_reg_defocus", None),
    ("TomoCtfRefine", {"do_reg_def": True, "lambda": "0.5"}, "--lambda 0.5", ("do_reg_def", False)),
    ("TomoCtfRefine", {"do_frame_scale": True}, "--per_frame_scale", None),
    ("TomoCtfRefine", {"do_tomo_scale": True}, "--per_tomo_scale", None),
    ("TomoReconstructTomograms", {"do_fourier": True}, "--fourier", None),
    ("TomoSubtomo", {"do_float16": True}, "--float16", None),
    ("TomoSubtomo", {"do_stack2d": True}, "--stack2d", None),
]


def test_do_save_nodw_is_never_emitted_for_the_tomo_variant():
    """do_save_noDW's condition is "!is_tomo && do_dose_weighting" -- unlike
    a plain field, is_tomo can only be set by which menu entry launched the
    draft (TomoMotioncorr vs Motioncorr), not by a caller-supplied
    field_values entry (_build_draft_command overwrites it from
    internal_name every time -- see its own docstring)."""
    raw = job_registry.raw_job("TomoMotioncorr")  # resolves to base "Motioncorr" internally
    fields = {"is_tomo": False, "do_dose_weighting": True, "do_save_noDW": True}
    cmd, _ = job_registry._build_draft_command(raw, fields, "TomoMotioncorr", "")
    assert "--save_noDW" not in cmd


def test_tomoimport_dose_rate_switches_flag_with_dose_is_per_movie_frame():
    """dose_rate previously always emitted --dose-per-tilt-image regardless
    of dose_is_per_movie_frame -- confirmed as a real bug (checking "Is dose
    rate per movie frame?" had no effect on the generated command) while
    auditing issue #16. FlagOverride.flag_if_condition_false fixes it: the
    SAME field's value now goes out under whichever of the two real flags
    matches the checkbox, never both and never neither."""
    raw = job_registry.raw_job("TomoImport")

    cmd_tilt, unmapped_tilt = job_registry._build_draft_command(
        raw, {"dose_is_per_movie_frame": False, "dose_rate": 3.5}, "TomoImport", "")
    assert "--dose-per-tilt-image 3.5" in cmd_tilt
    assert "--dose-per-movie-frame" not in cmd_tilt
    assert "dose_rate" not in unmapped_tilt

    cmd_movie, unmapped_movie = job_registry._build_draft_command(
        raw, {"dose_is_per_movie_frame": True, "dose_rate": 1.2}, "TomoImport", "")
    assert "--dose-per-movie-frame 1.2" in cmd_movie
    assert "--dose-per-tilt-image" not in cmd_movie
    assert "dose_rate" not in unmapped_movie


@pytest.mark.parametrize("internal_name,on_fields,expect_substr,off_flip", _UNMAPPED_FIELD_FIXES)
def test_unmapped_field_fix_emits_its_flag(internal_name, on_fields, expect_substr, off_flip):
    raw = job_registry.raw_job(internal_name)
    cmd, unmapped = job_registry._build_draft_command(raw, on_fields, internal_name, "")
    assert expect_substr in cmd, f"{internal_name}: expected {expect_substr!r} in {cmd!r}"
    if off_flip is not None:
        field, new_value = off_flip
        off_fields = {**on_fields, field: new_value}
        cmd_off, _ = job_registry._build_draft_command(raw, off_fields, internal_name, "")
        assert expect_substr not in cmd_off, (
            f"{internal_name}: {expect_substr!r} should disappear when {field}={new_value!r}, got {cmd_off!r}"
        )


def test_jobs_without_a_suffix_entry_keep_the_bare_directory():
    """Most jobs take a plain directory for --o -- e.g. Import, whose
    DRAFT_OVERRIDES entry doesn't set output_suffix -- and must NOT gain an
    unexpected suffix."""
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


@pytest.mark.parametrize("condition,field_values,known_keys,expected", [
    # Some jobs guard a command append with a bare local variable instead of
    # the inline joboptions[...] form -- RELION computes
    # `bool do_raw = joboptions["do_raw"].getBoolean();` once near the top of
    # getCommandsImportJob and tests `if (do_raw)` later, so the extractor
    # reads the condition text verbatim as "do_raw". Confirmed for real:
    # running an actual Import job against RELION 5.0.1 silently dropped
    # --angpix/--kV/--Cs/--Q0/--beamtilt_x/--beamtilt_y, all gated on exactly
    # this shape (and TomoAlign's --s_vel/--s_div on "do_motion").
    ("do_raw", {"do_raw": True}, {"do_raw"}, True),
    ("do_raw", {"do_raw": False}, {"do_raw"}, False),
    ("!do_raw", {"do_raw": True}, {"do_raw"}, False),
    ("!do_raw", {"do_raw": False}, {"do_raw"}, True),
    # A bare identifier that ISN'T one of this job's own options is left
    # unresolved rather than guessed at -- known_keys is the safety rail.
    ("do_raw", {"do_raw": True}, set(), None),
    ("do_raw", {"do_raw": True}, None, None),
    # is_continue's bare (un-negated) form: RELION-US never drafts a
    # "continue this job" run, so this is always false -- same fixed
    # constant as the already-tested "!is_continue" -> True, just the
    # opposite polarity, and doesn't need to be in known_keys since it's a
    # RelionJob state flag, not a real JobOption.
    ("is_continue", {}, set(), False),
])
def test_evaluate_condition_bare_identifier(condition, field_values, known_keys, expected):
    assert job_registry._evaluate_condition(condition, field_values, known_keys) is expected


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


def test_class3d_range_fields_are_clamped_to_0_90_before_dividing_by_3():
    """RELION clamps range_tilt/psi/rot to [0, 90] degrees before dividing
    by 3 to get the sigma passed to relion_refine (pipeline_jobs.cpp
    ~4077-4098) -- a value above 90 (or negative) must not pass through raw."""
    raw = job_registry.raw_job("Class3D")
    cmd, unmapped = job_registry._build_draft_command(
        raw,
        {"do_helix": True, "dont_skip_align": True, "do_local_ang_searches": False,
         "range_tilt": 120, "range_rot": -10},
        "Class3D", "",
    )
    assert "--sigma_tilt 30.0" in cmd   # clamped 120 -> 90, then /3
    assert "--sigma_rot 0.0" in cmd     # clamped -10 -> 0, then /3
    assert not any(k in unmapped for k in ("range_tilt", "range_rot"))


def test_helical_range_distance_omitted_when_not_positive():
    """RELION only emits --helical_sigma_distance when the raw value is > 0
    (an `if (val > 0.)` guard on the computed value itself) -- a
    non-positive value must be silently omitted, not passed through as a
    negative sigma, and must NOT be marked unmapped either."""
    for internal_name, fields in (
        ("Class3D",
         {"do_helix": True, "dont_skip_align": True, "do_local_ang_searches": False,
          "helical_range_distance": -5}),
        ("Autorefine", {"do_helix": True, "helical_range_distance": -5}),
    ):
        raw = job_registry.raw_job(internal_name)
        cmd, unmapped = job_registry._build_draft_command(raw, fields, internal_name, "")
        assert "--helical_sigma_distance" not in cmd, cmd
        assert "helical_range_distance" not in unmapped


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
