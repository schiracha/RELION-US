"""
Tests for job_catalog.py's DRAFT_OVERRIDES -- the single, typed per-job
override table (JobDraftOverride/FlagOverride) job_registry._build_draft_command
consults for every job the generic `--<key>` rule gets wrong. This consolidates
what used to be seven separate parallel tables (DRAFT_FLAG_MAP,
DRAFT_NEGATED_FLAGS, DRAFT_PROGRAM_OVERRIDE, DRAFT_SUPPRESS,
DRAFT_VALUE_TRANSFORM, DRAFT_OUTPUT_FLAG, DRAFT_OUTPUT_SUFFIX), each keyed
independently by internal_name -- into one, so a job's exceptions live in one
place. These tests pin the accessor functions' contract directly, independent
of _build_draft_command's own (much larger) test_job_registry.py coverage.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_catalog
from job_catalog import (
    DRAFT_OVERRIDES,
    FlagOverride,
    JobDraftOverride,
    draft_extra_flags,
    draft_extra_output_args,
    draft_flag_condition_for,
    draft_flag_for,
    draft_flag_if_condition_false_for,
    draft_flag_is_negated,
    draft_is_suppressed,
    draft_output_flag,
    draft_output_suffix,
    draft_numeric_value_for,
    draft_program_extra,
    draft_program_override,
    draft_value_for,
    has_draft_numeric_transform,
    has_draft_value_transform,
)


# --------------------------------------------------------------------------
# A job with no DRAFT_OVERRIDES entry at all -- every accessor's default,
# "use the generic rule" answer.
# --------------------------------------------------------------------------


def test_unlisted_job_gets_every_accessors_default():
    assert "Localres" not in DRAFT_OVERRIDES  # not one of the overridden jobs
    assert draft_flag_for("Localres", "anything") is None
    assert draft_flag_condition_for("Localres", "anything") is None
    assert draft_flag_is_negated("Localres", "anything") is False
    assert draft_flag_if_condition_false_for("Localres", "anything") is None
    assert draft_program_override("Localres") is None
    assert draft_is_suppressed("Localres", "anything") is False
    assert draft_output_flag("Localres") == "--o"
    assert draft_output_suffix("Localres") is None
    assert draft_extra_output_args("Localres", {}) == []
    assert draft_extra_flags("Localres", {}) == []
    assert has_draft_value_transform("Localres", "anything") is False
    assert draft_value_for("Localres", "anything", "x") is None
    assert has_draft_numeric_transform("Localres", "anything") is False
    assert draft_numeric_value_for("Localres", "anything", 1.0) is None


# --------------------------------------------------------------------------
# A key that IS in DRAFT_OVERRIDES but not this particular job's override --
# same defaults, not a KeyError.
# --------------------------------------------------------------------------


def test_unmapped_key_on_an_overridden_job_still_gets_defaults():
    assert "Motioncorr" in DRAFT_OVERRIDES
    assert draft_flag_for("Motioncorr", "bfactor") is None
    assert draft_is_suppressed("Motioncorr", "bfactor") is False
    assert has_draft_value_transform("Motioncorr", "bfactor") is False


# --------------------------------------------------------------------------
# FlagOverride: plain, conditioned, and negated shapes
# --------------------------------------------------------------------------


def test_plain_flag_override_has_no_condition_and_is_not_negated():
    assert draft_flag_for("Motioncorr", "do_dose_weighting") == "--dose_weighting"
    assert draft_flag_condition_for("Motioncorr", "do_dose_weighting") is None
    assert draft_flag_is_negated("Motioncorr", "do_dose_weighting") is False


def test_ctffind_use_nodw_is_spa_only():
    # use_noDW is genuinely SPA-only in real RELION (initialiseCtffindJob
    # only creates this JobOption `if (!is_tomo)`) -- unlike Motioncorr's
    # do_dose_weighting above, this override carries a real condition so it
    # stops being emitted once the SPA/Tomo toggle (frontend TOMO_TOGGLE_JOBS)
    # sends field_values["is_tomo"] = True. See job_catalog.py's Ctffind
    # entry for the full reasoning.
    assert draft_flag_for("Ctffind", "use_noDW") == "--use_noDW"
    assert draft_flag_condition_for("Ctffind", "use_noDW") == "!is_tomo"
    assert draft_flag_is_negated("Ctffind", "use_noDW") is False


def test_conditioned_flag_override_exposes_its_condition():
    assert draft_flag_for("Import", "fn_in_raw") == "--i"
    assert draft_flag_condition_for("Import", "fn_in_raw") == "do_raw"
    assert draft_flag_for("Import", "fn_in_other") == "--i"
    assert draft_flag_condition_for("Import", "fn_in_other") == "do_other"


def test_negated_flag_override():
    assert draft_flag_for("Class2D", "do_parallel_discio") == "--no_parallel_disc_io"
    assert draft_flag_is_negated("Class2D", "do_parallel_discio") is True


def test_flag_override_with_an_alternate_false_branch_flag():
    # dose_rate goes out under one of two real flags depending on a sibling
    # checkbox, rather than being simply present/absent -- see job_catalog's
    # TomoImport entry and https://github.com/schiracha/RELION-US/issues/16.
    assert draft_flag_for("TomoImport", "dose_rate") == "--dose-per-tilt-image"
    assert draft_flag_condition_for("TomoImport", "dose_rate") == "!dose_is_per_movie_frame"
    assert draft_flag_if_condition_false_for("TomoImport", "dose_rate") == "--dose-per-movie-frame"
    # A field with only the common (no alternate) shape still answers None,
    # not a KeyError or an empty string.
    assert draft_flag_if_condition_false_for("Ctffind", "use_noDW") is None
    # A non-negated flag on the SAME job must not be affected.
    assert draft_flag_is_negated("Class2D", "do_preread_images") is False


# --------------------------------------------------------------------------
# program / output_flag / output_suffix
# --------------------------------------------------------------------------


def test_program_override():
    assert draft_program_override("TomoImport") == "relion_python_tomo_import SerialEM"
    assert draft_program_override("Import") is None  # no override needed


def test_output_flag_override_vs_default():
    assert draft_output_flag("Import") == "--odir"
    assert draft_output_flag("TomoImport") == "--output-directory"
    assert draft_output_flag("Class2D") == "--o"  # falls back to the default


def test_output_suffix_override():
    assert draft_output_suffix("Class2D") == "run"
    assert draft_output_suffix("Maskcreate") == "mask.mrc"
    assert draft_output_suffix("Import") is None


# --------------------------------------------------------------------------
# suppress
# --------------------------------------------------------------------------


def test_suppress_membership():
    assert draft_is_suppressed("Autopick", "use_gpu") is True
    assert draft_is_suppressed("Autopick", "some_other_field") is False


# --------------------------------------------------------------------------
# value_transforms
# --------------------------------------------------------------------------


def test_value_transform_lookup_and_unknown_label():
    assert has_draft_value_transform("Motioncorr", "gain_rot") is True
    assert draft_value_for("Motioncorr", "gain_rot", "90 degrees (1)") == "1"
    # A label that isn't one of the known choices resolves to None (the
    # caller then marks the field unmapped) rather than a KeyError or a
    # guessed value.
    assert draft_value_for("Motioncorr", "gain_rot", "some future label") is None


# --------------------------------------------------------------------------
# extra_output_args (Import's --ofile, the one callable slot in the table --
# what used to be a bare `if internal_name == "Import":` special case
# inline in job_registry._build_draft_command)
# --------------------------------------------------------------------------


def test_extra_output_args_import_do_raw_multiframe():
    assert draft_extra_output_args("Import", {"do_raw": True, "is_multiframe": True}) == \
        ["--ofile", "movies.star"]


def test_extra_output_args_import_do_raw_single_frame():
    assert draft_extra_output_args("Import", {"do_raw": True, "is_multiframe": False}) == \
        ["--ofile", "micrographs.star"]


def test_extra_output_args_import_do_other_returns_nothing():
    """do_other's --ofile is derived from fn_in_other itself -- genuine
    per-node-type branch logic this app deliberately doesn't reconstruct
    (same policy as TomoImport's do_coords branch)."""
    assert draft_extra_output_args("Import", {"do_raw": False, "do_other": True}) == []


def test_extra_output_args_default_job_returns_nothing():
    assert draft_extra_output_args("Motioncorr", {}) == []


# --------------------------------------------------------------------------
# extra_flags (Extract's --bg_radius and --helical_nr_asu/--helical_rise
# fallback -- a value/branch computed from MULTIPLE fields, not just one
# option's own value or a single flag's presence/absence; issues #17/#18)
# --------------------------------------------------------------------------


def test_extra_flags_extract_bg_radius():
    # "--norm" itself is a separate, plain FlagOverride (Extract's own
    # entry) -- not part of extra_flags' output, so it still appears even
    # if this computation bails out (see _extract_bg_radius_flags).
    assert draft_extra_flags("Extract", {"do_norm": True, "bg_diameter": -1, "extract_size": 128}) == \
        ["--bg_radius", "48"]


def test_extra_flags_extract_bg_radius_omitted_when_do_norm_off():
    assert draft_extra_flags("Extract", {"do_norm": False, "bg_diameter": -1, "extract_size": 128}) == []


def test_extra_flags_extract_helical_fallback():
    assert draft_extra_flags(
        "Extract",
        {"do_extract_helix": True, "do_extract_helical_tubes": True, "do_cut_into_segments": False},
    ) == ["--helical_nr_asu", "1", "--helical_rise", "1"]


def test_extra_flags_extract_helical_fallback_absent_once_cutting_into_segments():
    assert draft_extra_flags(
        "Extract",
        {"do_extract_helix": True, "do_extract_helical_tubes": True, "do_cut_into_segments": True},
    ) == []


def test_extra_flags_default_job_returns_nothing():
    assert draft_extra_flags("Motioncorr", {}) == []


# --------------------------------------------------------------------------
# program_extra (TomoDenoiseTomograms's cryoCARE:train/predict subcommand
# token -- a value inserted BEFORE output_flag/subdir, unlike extra_flags
# and extra_output_args; issue #24)
# --------------------------------------------------------------------------


def test_program_extra_tomodenoise_train_mode():
    assert draft_program_extra("TomoDenoiseTomograms", {"do_cryocare_train": True}) == \
        ["cryoCARE:train"]


def test_program_extra_tomodenoise_predict_mode():
    assert draft_program_extra("TomoDenoiseTomograms", {"do_cryocare_predict": True}) == \
        ["cryoCARE:predict"]


def test_program_extra_tomodenoise_neither_mode_returns_nothing():
    assert draft_program_extra("TomoDenoiseTomograms", {}) == []


def test_program_extra_default_job_returns_nothing():
    assert draft_program_extra("Motioncorr", {}) == []


def test_extra_flags_tomodenoise_ntiles():
    assert draft_extra_flags("TomoDenoiseTomograms", {
        "do_cryocare_predict": True, "ntiles_x": "2", "ntiles_y": "3", "ntiles_z": "4",
    }) == ["--n-tiles", "2", "3", "4"]


def test_extra_flags_tomodenoise_ntiles_omitted_when_predict_off():
    assert draft_extra_flags("TomoDenoiseTomograms", {
        "do_cryocare_predict": False, "ntiles_x": "2", "ntiles_y": "3", "ntiles_z": "4",
    }) == []


def test_extra_flags_tomodenoise_ntiles_omitted_when_train_also_checked():
    """do_cryocare_train/do_cryocare_predict are independent checkboxes
    real RELION only guards with a hard error this app doesn't replicate
    -- _tomo_denoise_subcommand_tokens gives do_cryocare_train precedence
    for the subcommand token, so --n-tiles (a predict-only flag) must not
    ride along under a "cryoCARE:train" subcommand."""
    assert draft_extra_flags("TomoDenoiseTomograms", {
        "do_cryocare_predict": True, "do_cryocare_train": True,
        "ntiles_x": "2", "ntiles_y": "3", "ntiles_z": "4",
    }) == []


def test_extra_flags_tomodenoise_ntiles_shell_metacharacters_are_quoted():
    """ntiles_x/y/z are plain user-editable text fields, not sliders --
    their raw values must be shell-quoted the same way every other
    free-text field on the command line is (job_registry._build_draft_
    command's ordinary per-option loop), since extra_flags output isn't
    quoted by its caller and the resulting draft command can go straight
    to a subprocess shell."""
    result = draft_extra_flags("TomoDenoiseTomograms", {
        "do_cryocare_predict": True,
        "ntiles_x": "2$(touch /tmp/pwned)", "ntiles_y": "3", "ntiles_z": "4",
    })
    assert result == ["--n-tiles", "'2$(touch /tmp/pwned)'", "3", "4"]


# --------------------------------------------------------------------------
# The dataclasses themselves: frozen (immutable, hashable) and defaulted
# --------------------------------------------------------------------------


def test_job_draft_override_defaults_are_all_falsy_empty():
    override = JobDraftOverride()
    assert override.program is None
    assert override.program_extra is None
    assert override.output_flag is None
    assert override.output_suffix is None
    assert override.flags == {}
    assert override.suppress == frozenset()
    assert override.value_transforms == {}
    assert override.numeric_transforms == {}
    assert override.extra_output_args is None
    assert override.extra_flags is None


def test_flag_override_and_job_draft_override_are_frozen():
    flag = FlagOverride("--x")
    try:
        flag.flag = "--y"
        assert False, "FlagOverride should be immutable"
    except AttributeError:
        pass
    override = JobDraftOverride()
    try:
        override.program = "whatever"
        assert False, "JobDraftOverride should be immutable"
    except AttributeError:
        pass


def test_every_draft_overrides_entry_is_a_job_draft_override():
    for name, override in DRAFT_OVERRIDES.items():
        assert isinstance(override, JobDraftOverride), name
        for key, flag_override in override.flags.items():
            assert isinstance(flag_override, FlagOverride), f"{name}.{key}"


# --------------------------------------------------------------------------
# numeric_transforms
# --------------------------------------------------------------------------


def test_numeric_transform_clamp_then_divide():
    assert has_draft_numeric_transform("Class3D", "range_tilt") is True
    assert draft_numeric_value_for("Class3D", "range_tilt", 120.0) == 30.0  # clamped to 90, /3
    assert draft_numeric_value_for("Class3D", "range_rot", -10.0) == 0.0    # clamped to 0, /3


def test_numeric_transform_positivity_gate_returns_none():
    assert has_draft_numeric_transform("Class3D", "helical_range_distance") is True
    assert draft_numeric_value_for("Class3D", "helical_range_distance", -5.0) is None
    assert draft_numeric_value_for("Class3D", "helical_range_distance", 9.0) == 3.0
    assert has_draft_numeric_transform("Class3D", "some_other_field") is False
