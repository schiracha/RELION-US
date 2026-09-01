"""
job_registry.py — combines job_catalog.py (curated display metadata) with
data/job_definitions_raw.json (extracted verbatim from RELION source, see
data/extract_job_definitions.py) into the structure the API and frontend
consume: one JobDefinition per job type.

Field placement follows one rule: **everything RELION's own GUI shows goes in
the popup's Inputs tab**, grouped under RELION's own tab names (I/O, CTF,
Optimisation, ..., Running) and in RELION's own order — `standard_groups`.
The Inputs tab's "Advanced" section (the last one, past Running/Other) is for
the opposite thing: command-line options the program accepts but the GUI
never exposes, the ones you would otherwise find by running the binary with
`--help` or reading the source. Those are discovered at runtime from the
installed RELION (see program_help.py), not from this file.

Also builds a best-effort DRAFT command per job. This is intentionally
NOT a full reimplementation of RELION's getCommands<Job>Job() C++ logic —
that logic has real per-job branching (see commands_source in the raw
data) that a mechanical port risks getting subtly wrong, which is exactly
the class of bug this whole app exists to get away from. Instead:

  - the draft command is built with one simple, transparent rule: for each
    active field, if a `--<field_key>` flag literally appears in that
    job's real getCommands source (flags_used, extracted verbatim), emit
    `--<field_key> <value>` (bare flag for booleans). This rule is
    correct for the (large) majority of RELION options, because RELION's
    own convention is overwhelmingly to name the flag after the internal
    option key.
  - fields where no matching `--<key>` flag was found in the real source
    are left OUT of the draft command and flagged in the API response
    (`unmapped_fields`) rather than guessed at.
  - the real commands_source (verbatim RELION C++, comments stripped) is
    always available alongside the draft, so you can cross-check by eye.
  - the draft is always shown in an editable box before anything runs —
    per your request, nothing executes except the exact string you approve.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from job_catalog import (
    CATEGORIES,
    CUSTOM_JOBS,
    JOB_CATALOG,
    TOMO_VARIANT_OF,
    boolean_select_labels,
    draft_commands_before,
    draft_extra_flags,
    draft_extra_output_args,
    draft_flag_condition_for,
    draft_flag_for,
    draft_flag_if_condition_false_for,
    draft_flag_is_negated,
    draft_numeric_value_for,
    draft_value_for,
    has_draft_numeric_transform,
    has_draft_value_transform,
    draft_is_suppressed,
    draft_output_flag,
    draft_output_suffix,
    draft_program_extra,
    draft_program_override,
    draft_suppress_output_flag,
    pipeline_type,
    synthetic_options,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Jobs that run a user-configured executable (DynaMight/ModelAngelo/External)
# rather than a hard-coded relion_* binary; see extract_job_definitions.py.
_EXE_PLACEHOLDER_RE = re.compile(r"^\{joboptions\.([A-Za-z0-9_]+)\}$")


@functools.lru_cache(maxsize=1)
def _load_raw() -> dict[str, Any]:
    """Parsed job_definitions_raw.json. Cached: it's ~500 KB (32 jobs, each
    embedding RELION's verbatim C++ command source) and is re-read for every
    job definition AND every draft recompute, which the frontend fires as the
    user edits a form. The file only changes when the extractor is re-run,
    which requires a restart anyway."""
    with open(DATA_DIR / "job_definitions_raw.json", encoding="utf-8") as f:
        return json.load(f)


def _standard_groups(raw_job: dict) -> list[dict]:
    """RELION's own GUI layout for this job, as ordered, named groups.

    Every field RELION's GUI shows goes in the job popup's Inputs tab, grouped
    under RELION's own tab names (I/O, CTF, Optimisation, ..., Running) and in
    RELION's own order. The Inputs tab's "Advanced" section is NOT for these
    -- it is for command-line options the GUI never exposes (see program_help.py).

    Falls back to one unnamed group holding every field if this job has no
    parsed tab_layout (the 3 custom import bridges, which define their own).
    """
    layout = raw_job.get("tab_layout")
    all_keys = [o["key"] for o in raw_job.get("options", [])]
    if not layout or not layout.get("tab_order"):
        return [{"name": "", "fields": all_keys}]

    tab_fields = layout["tab_fields"]
    groups = [
        {"name": name, "fields": list(tab_fields[name])}
        for name in layout["tab_order"]
        if tab_fields.get(name)
    ]
    # Anything RELION defines as a JobOption but never places in a tab would
    # otherwise be silently unreachable in the form.
    placed = {k for g in groups for k in g["fields"]}
    orphans = [k for k in all_keys if k not in placed]
    if orphans:
        groups.append({"name": "Other", "fields": orphans})
    return groups


def _field_default_value(option: dict) -> Any:
    ft = option["field_type"]
    if ft == "boolean":
        return option.get("default", False)
    if ft == "slider":
        return option.get("default", 0.0)
    if ft == "radio":
        opts = option.get("options", [])
        idx = option.get("default_index", 0)
        return opts[idx] if opts and 0 <= idx < len(opts) else (opts[0] if opts else "")
    # text / filename / inputnode
    return option.get("default", "")


# RELION's own MPI wrapping, from RelionJob::prepareFinalCommand() in
# pipeline_jobs.cpp: when "Number of MPI procs" > 1 and the command is a
# relion_ program with an `_mpi` binary, it prefixes
# `$RELION_MPIRUN -n <procs> `, defaulting to "mpirun" (DEFAULTMPIRUN in
# pipeline_jobs.h). The binary itself is swapped by each job's own builder,
# which is where program_mpi comes from -- not by appending "_mpi" here.
DEFAULT_MPIRUN = "mpirun"


def _mpirun_prefix(nr_mpi: int) -> list[str]:
    mpirun = os.environ.get("RELION_MPIRUN") or DEFAULT_MPIRUN
    return [mpirun, "-n", str(int(nr_mpi))]


def _self_guarded(condition: str, key: str) -> bool:
    """True if `condition` only tests this option's own value.

    RELION guards many appends with the option itself --
    `if (joboptions["scratch_dir"].getString() != "") command += " --scratch_dir "...`
    -- which means nothing more than "emit when set", something the draft
    already does by skipping empty values. Those are safe to emit. A condition
    naming a *different* option is a real branch (Topaz vs LoG picking, EM vs
    gradient refinement) and is left out of the draft instead of guessed at.
    """
    if not condition:
        return True
    # RELION-US never models a "continue this job" run (see module
    # docstring) -- every draft it builds is for a fresh job, so is_continue
    # is always false here. A condition of EXACTLY "!is_continue" (nothing
    # else combined with it) is therefore vacuously true in this app's
    # context, same as no condition at all. Deliberately narrow: this does
    # NOT extend to conditions merely containing "!is_continue" alongside
    # other terms (e.g. "!is_continue && else") -- an "else" token can guard
    # on a *different* option's boolean (confirmed for real: Motioncorr's
    # fn_motioncor2_exe is only added in the do_own_motioncor==false branch,
    # a condition extracted as the identical-looking bare "else"), so those
    # still need per-field verification against the real source instead
    # (see job_catalog.DRAFT_OVERRIDES for the fields that got it).
    if condition.strip() == "!is_continue":
        return True
    referenced = set(re.findall(r'joboptions\[\s*"([A-Za-z0-9_]+)"\s*\]', condition))
    if referenced - {key}:
        return False
    # A condition with no joboptions reference at all (e.g. `!is_continue`)
    # is about job state, not this field -- treat it as a real branch.
    return bool(referenced)


_BOOL_CLAUSE_RE = re.compile(
    r'^joboptions\[\s*"([A-Za-z0-9_]+)"\s*\]\.getBoolean\(\)$'
)

# Some jobs guard a command append with a bare local variable instead of the
# inline `joboptions["x"].getBoolean()` form -- RELION's own source computes
# `bool do_raw = joboptions["do_raw"].getBoolean();` once near the top of
# getCommandsImportJob and tests `if (do_raw)` later, so the extractor reads
# the condition text verbatim as "do_raw", which _BOOL_CLAUSE_RE never
# matches (confirmed for real: running an Import job silently dropped
# --angpix/--kV/--Cs/--Q0/--beamtilt_x/--beamtilt_y, all gated on exactly
# this pattern, and TomoAlign's --s_vel/--s_div on "do_motion"). Recognized
# only when the bare name is a real boolean option of THIS job (passed in as
# known_keys) -- an unrecognized identifier still falls through to None
# rather than risk resolving the wrong thing.
_BARE_IDENT_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)$')

# Some jobs guard a command append not on a boolean field's own value but on
# a plain substring test of a STRING field, via RELION's `FileName::contains`
# (filename.cpp ~141-148, a bare `rfind(str) > -1` check -- true if the
# literal appears anywhere in the string). Confirmed for real: Select's
# `FileName fnt = joboptions["fn_model"].getString(); if
# (fnt.contains("Class2D/")) { ... if (do_recenter) command += " --recenter
# "; }` (pipeline_jobs.cpp ~2980-2991) -- do_recenter's flag is only ever
# emitted when fn_model's value contains "Class2D/" (issue #23).
_CONTAINS_CLAUSE_RE = re.compile(
    r'^([A-Za-z_][A-Za-z0-9_]*)\.contains\("([^"]*)"\)$'
)


def _strip_matched_outer_parens(clause: str) -> str:
    """Strip one or more layers of parens that wrap the WHOLE clause (not
    just its start), e.g. "(joboptions[\"do_helix\"].getBoolean())" ->
    "joboptions[\"do_helix\"].getBoolean()". Stops as soon as the outer
    parens don't actually match end-to-end, so "(a)+(b)" is left alone."""
    while clause.startswith("(") and clause.endswith(")"):
        depth = 0
        wraps_all = True
        for i, ch in enumerate(clause):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(clause) - 1:
                    wraps_all = False
                    break
        if not wraps_all:
            break
        clause = clause[1:-1].strip()
    return clause


def _split_top_level_and(condition: str) -> list[str] | None:
    """Split `condition` on `&&` that sits outside any parentheses. None
    (defer to the caller's existing "unmapped" fallback) the moment the
    condition contains an `||` or the literal token `else` anywhere -- both
    mean real branch logic this app deliberately doesn't try to interpret
    (see the module docstring).

    Known, deliberate remaining limitation: a `||` nested inside parens,
    e.g. `(a || b) && c`, is invisible to this function's own naive
    `"||" in condition` check just as much as it is to
    _split_top_level_or's depth-tracking splitter (added alongside this
    note) -- neither looks inside parens for it. This is fine: the failure
    mode is always "returns None -> unmapped", never a wrong guess, and no
    current job in job_catalog.DRAFT_OVERRIDES needs it fixed."""
    if "||" in condition or re.search(r"\belse\b", condition):
        return None
    clauses: list[str] = []
    current: list[str] = []
    depth = 0
    i = 0
    while i < len(condition):
        ch = condition[i]
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif depth == 0 and condition[i : i + 2] == "&&":
            clauses.append("".join(current))
            current = []
            i += 1  # skip the second '&'
        else:
            current.append(ch)
        i += 1
    clauses.append("".join(current))
    return [c.strip() for c in clauses if c.strip()]


def _split_top_level_or(condition: str) -> list[str] | None:
    """Split `condition` on `||` that sits outside any parentheses -- same
    depth-tracking shape as _split_top_level_and, but for the operator
    that binds LOOSEST in C, so splitting on it first (before any `&&`
    splitting happens per branch) is the correct grammar decomposition:
    each resulting branch is itself an AND-clause (or a single term),
    evaluated independently by _evaluate_and_clauses. None (defer to the
    caller's "unmapped" fallback) the moment the condition contains the
    literal token `else` anywhere -- same reasoning as
    _split_top_level_and. A condition with no top-level `||` at all (the
    common case) still returns a single-element list holding the whole
    condition unchanged.
    """
    if re.search(r"\belse\b", condition):
        return None
    clauses: list[str] = []
    current: list[str] = []
    depth = 0
    i = 0
    while i < len(condition):
        ch = condition[i]
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif depth == 0 and condition[i : i + 2] == "||":
            clauses.append("".join(current))
            current = []
            i += 1  # skip the second '|'
        else:
            current.append(ch)
        i += 1
    clauses.append("".join(current))
    return [c.strip() for c in clauses if c.strip()]


def _evaluate_and_clauses(
    condition: str, field_values: dict[str, Any], known_keys: set[str] | None = None
) -> bool | None:
    """Best-effort evaluation of ONE `&&`-joined OR-branch (or a whole
    condition with no top-level `||` at all) against the CURRENT field
    values -- not a guess, but replaying the exact same
    `joboptions["X"].getBoolean()` check RELION's own getCommands*Job()
    would make. This is _evaluate_condition's original single-branch body,
    kept intact and given its own name so _evaluate_condition can dispatch
    across OR-branches (see its own docstring) before delegating each one
    here.

    known_keys: this job's real option keys (from `options_by_key`), used
    only to decide whether a bare identifier clause (see _BARE_IDENT_RE) is
    safe to resolve -- an identifier that isn't one of this job's own
    options is left unresolved rather than guessed at.

    Returns True/False when every top-level `&&`-joined clause in THIS
    branch is one of the recognized, safely-evaluable shapes: a boolean
    field's own value (optionally negated, as either
    `joboptions["x"].getBoolean()` or the bare local-variable form some
    jobs use instead -- see _BARE_IDENT_RE), a string field's substring
    test (optionally negated, as `x.contains("literal")` -- see
    _CONTAINS_CLAUSE_RE), the `is_continue` invariant (a fixed constant in
    this app -- see below), or `is_tomo`/`!is_tomo` (read from
    field_values["is_tomo"] when present, see below). Returns
    None the moment anything else shows up within this branch -- an
    `else` branch marker, a numeric/string comparison, a call this
    function doesn't recognize -- so the caller falls back to marking the
    field "unmapped" exactly as it did before this evaluator existed,
    rather than risk a wrong guess.
    """
    clauses = _split_top_level_and(condition)
    if clauses is None:
        return None
    result = True
    for raw_clause in clauses:
        clause = _strip_matched_outer_parens(raw_clause.strip())
        if clause in ("is_continue", "!is_continue"):
            # RELION-US never models a "continue this job" run (see module
            # docstring) -- is_continue is always false here, whichever form
            # the extracted condition text uses (some jobs test the inline
            # joboptions[...] expression, Import-style jobs test a bare
            # local bool computed from it earlier -- both read the same
            # underlying value, so both get the same fixed answer here).
            value = clause.startswith("!")  # "!is_continue" -> True, "is_continue" -> False
        elif clause in ("is_tomo", "!is_tomo"):
            # `is_tomo` is RelionJob's own GUI-launch-time flag (RELION's GUI
            # is started with plain `relion` or `relion --tomo`, gui_main
            # window.cpp's `_do_tomo`, threaded into RelionJob::initialise()
            # for the handful of job types -- Motioncorr/Ctffind/Inimodel/
            # Class3D/Autorefine/Postprocess -- that build a slightly
            # different option set and command per launch mode); it is NOT a
            # JobOption and never appears in a real job.star. Most of those
            # job types are unaffected here: RELION-US never launches
            # RELION's own tomo GUI, so for the Class3D/Autorefine/Inimodel
            # family is_tomo is inferred elsewhere from real field content
            # (see pipeline_bridge._is_tomo_job) and this draft-command path
            # never needs to know it. Motioncorr/Ctffind are different --
            # RELION-US has exactly ONE catalog entry for each, genuinely
            # used for both SPA and real tomography tilt-series input (no
            # separate tomo job type), so is_tomo is a real, user-set choice
            # here: the frontend's SPA/Tomo toggle for those two job types
            # sends it as field_values["is_tomo"]. Any job type that has no
            # such toggle never has that key in field_values, so this
            # defaults to False for it -- the same fixed answer as before.
            value = bool(field_values.get("is_tomo", False))
            if clause.startswith("!"):
                value = not value
        else:
            negated = clause.startswith("!")
            body = clause[1:].strip() if negated else clause
            m = _BOOL_CLAUSE_RE.match(body)
            if m:
                value = bool(field_values.get(m.group(1)))
            else:
                contains_m = _CONTAINS_CLAUSE_RE.match(body)
                if contains_m:
                    field_name, literal = contains_m.group(1), contains_m.group(2)
                    if known_keys is not None and field_name in known_keys:
                        value = literal in str(field_values.get(field_name) or "")
                    else:
                        return None
                else:
                    bare = _BARE_IDENT_RE.match(body)
                    if bare and known_keys is not None and bare.group(1) in known_keys:
                        value = bool(field_values.get(bare.group(1)))
                    else:
                        return None
            if negated:
                value = not value
        result = result and value
        if not result:
            # AND short-circuit: once one clause resolves false the whole
            # condition is false regardless of any later clause, including
            # one this evaluator wouldn't otherwise know how to parse (e.g.
            # Class3D/Autorefine/Inimodel's `sigma_tilt`, gated on
            # "!is_continue && is_tomo && sigma > 0." -- the numeric
            # comparison is unparseable on its own, but is_tomo already
            # being false settles the whole clause).
            return False
    return result


def _evaluate_condition(
    condition: str, field_values: dict[str, Any], known_keys: set[str] | None = None
) -> bool | None:
    """Best-effort evaluation of a RELION `if (...)` condition against the
    CURRENT field values. Splits the WHOLE condition into top-level `||`
    branches first (correct C precedence: `||` binds loosest), then
    evaluates each branch as its own `&&`-joined clause list via
    _evaluate_and_clauses (see its docstring for the recognized clause
    shapes). Short-circuits like real `||`: True the moment any branch is
    True (doesn't need every branch to be evaluable); if no branch is True
    but at least one couldn't be evaluated, the overall verdict is None
    (can't be sure); False only once every branch cleanly evaluates to
    False. A condition with no top-level `||` at all degenerates to
    exactly one branch, so this also covers what used to be
    _evaluate_condition's entire body for every plain-AND condition
    already in this app.

    known_keys: passed through unchanged to _evaluate_and_clauses.

    Returns None the moment anything unrecognized shows up anywhere -- an
    `else` branch marker, a numeric/string comparison, a call this
    function doesn't recognize, or an `||` whose branches straddle a
    paren boundary this app's splitting doesn't look inside (see
    _split_top_level_and's docstring) -- so the caller falls back to
    marking the field "unmapped" rather than risk a wrong guess.
    """
    if not condition:
        return True
    or_clauses = _split_top_level_or(condition)
    if or_clauses is None:
        return None
    saw_none = False
    for clause in or_clauses:
        verdict = _evaluate_and_clauses(clause, field_values, known_keys)
        if verdict is True:
            return True
        if verdict is None:
            saw_none = True
    return None if saw_none else False


def _build_draft_command(
    raw_job: dict,
    field_values: dict[str, Any],
    internal_name: str = "",
    output_subdir: str = "",
) -> tuple[str, list[str]]:
    """
    Returns (draft_command_string, unmapped_field_keys).
    See module docstring for the rule this follows.

    A curated, source-verified overlay (job_catalog.DRAFT_OVERRIDES) corrects
    the handful of jobs the generic rule gets wrong — RELION-5's Python tomo
    tools, whose hyphenated CLI flags (`--tilt-image-movie-pattern`) don't
    match their snake_case option keys (`movie_files`), and TomoImport,
    whose extracted program was the wrong (do_coords) branch. A mapped flag
    is authoritative: it's always emitted,
    bypassing the flags_used membership test (unreliable for these jobs).

    output_subdir: if given (e.g. "Import/job005"), the RELION-style output
    flag (`--o <subdir>/`, or `--output-directory` for the Python tomo
    tools) is inserted right after the program — matching RELION, which runs
    from the project root and passes the job's output directory as a
    project-root-relative path. Omitted for exe-placeholder jobs
    (DynaMight/ModelAngelo/External), whose output conventions differ and
    which this app doesn't try to guess.

    internal_name may be a job_catalog.TOMO_VARIANT_OF entry (TomoMotioncorr/
    TomoCtffind) -- _resolve_tomo_variant maps it to the real RELION job
    class (base_name) for every DRAFT_OVERRIDES/flags_used lookup below
    (those facts are about the shared RelionJob class, not which menu entry
    was clicked), and sets field_values["is_tomo"] from it directly, which
    _evaluate_condition reads for the is_tomo/!is_tomo-gated fields (see its
    own docstring). This overwrites any is_tomo already in field_values --
    internal_name is the single source of truth now, not a caller-supplied
    value.
    """
    base_name, is_tomo_variant = _resolve_tomo_variant(internal_name)
    field_values = {**field_values, "is_tomo": is_tomo_variant}
    program_override_value = draft_program_override(base_name, field_values)
    program = program_override_value or raw_job.get("program_guess") or "<unknown_program>"
    # A few jobs (DynaMight, ModelAngelo, External) don't hard-code a
    # binary — they run whatever executable path the user configured in a
    # JobOption (e.g. "Location of DynaMight executable:"). extract_job_
    # definitions.py surfaces that as the placeholder "{joboptions.<key>}";
    # resolve it against the actual field values here.
    placeholder_match = _EXE_PLACEHOLDER_RE.match(program)
    is_exe_placeholder = placeholder_match is not None
    if placeholder_match:
        exe_key = placeholder_match.group(1)
        configured = field_values.get(exe_key)
        # quoted like every other value below -- an executable path with a
        # space would otherwise produce a broken draft
        program = shlex.quote(str(configured)) if configured else (
            f"<set the '{exe_key}' field to this job's executable path>"
        )
    flags_used = set(raw_job.get("flags_used", []))
    option_flags = raw_job.get("option_flags", {})
    options_by_key = {o["key"]: o for o in raw_job.get("options", [])}

    # RELION's Running tab. nr_mpi and other_args are not ordinary flags:
    # RELION handles both outside the per-option loop, and so does this.
    try:
        nr_mpi = int(float(field_values.get("nr_mpi", 1) or 1))
    except (TypeError, ValueError):
        nr_mpi = 1
    program_mpi = raw_job.get("program_mpi")
    prefix: list[str] = []
    # Skipped entirely once a program_override already resolved `program`
    # for the CURRENT field values (see JobDraftOverride.program_override's
    # own docstring) -- otherwise this would blindly swap in raw_job's
    # single fixed program_mpi guess even for a branch the override chose
    # deliberately (e.g. Localres's ResMap mode, whose program is a
    # user-configured executable with no MPI form of its own at all;
    # program_mpi here is `relion_postprocess_mpi`, correct only for
    # Localres's OTHER branch, which never sets program_override_value).
    if nr_mpi > 1 and program_mpi and not is_exe_placeholder and program_override_value is None:
        # Same two conditions RELION applies: an _mpi binary exists for this
        # job, and more than one process was asked for.
        program = program_mpi
        prefix = _mpirun_prefix(nr_mpi)

    parts = prefix + [program]
    # A RELION subcommand-style positional token this job's CLI needs
    # immediately after the program name, ahead of the output flag and
    # every other flag -- see job_catalog.JobDraftOverride.program_extra.
    parts.extend(draft_program_extra(base_name, field_values, output_subdir))
    # RELION-style output directory (project-root-relative), inserted right
    # after the program name — mirrors how getCommands*Job() appends it.
    # Skipped for a job branch that takes no --o at all (see
    # JobDraftOverride.suppress_output_flag) on top of the existing
    # exe-placeholder skip.
    if output_subdir and not is_exe_placeholder and not draft_suppress_output_flag(base_name, field_values):
        subdir_arg = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
        # Some jobs don't take a bare directory here -- RELION appends a
        # literal suffix to form a file rootname prefix (e.g. "run" ->
        # ".../job001/run", so output files become "run_it000_..."  instead
        # of a bare directory that would otherwise produce a wrong,
        # un-prefixed filename like "_it000_..."). See
        # job_catalog.JobDraftOverride.output_suffix.
        suffix = draft_output_suffix(base_name)
        if suffix:
            subdir_arg += suffix
        parts.append(draft_output_flag(base_name))
        parts.append(shlex.quote(subdir_arg))
        # A compulsory-but-computed extra argument some jobs need right
        # after the output flag/subdir (e.g. Import's --ofile) -- see
        # job_catalog.JobDraftOverride.extra_output_args.
        parts.extend(draft_extra_output_args(base_name, field_values))
    unmapped = []

    for key, value in field_values.items():
        option = options_by_key.get(key)
        if option is None:
            continue
        # Options belonging to a non-default branch are omitted from the
        # default draft entirely (and not counted as unmapped).
        if draft_is_suppressed(base_name, key):
            continue
        # Handled outside this loop, exactly as RELION handles them.
        if key in ("nr_mpi", "other_args"):
            continue
        # A verified per-job flag override wins and is always emitted; only
        # fall back to the generic `--<key>` rule when no override exists.
        mapped = draft_flag_for(base_name, key)
        if mapped is not None:
            # A mapped flag is normally unconditional -- but the rare entry
            # that shares its flag with another mutually-exclusive option
            # (see DRAFT_OVERRIDES["Import"].flags) carries its own condition,
            # checked the same way as an extracted option_flags condition
            # below, since neither field's own empty-value skip can tell
            # which branch is actually active.
            mapped_condition = draft_flag_condition_for(base_name, key)
            if mapped_condition:
                verdict = _evaluate_condition(
                    mapped_condition, field_values, options_by_key.keys()
                )
                if verdict is False:
                    # Usually "don't emit this flag at all" -- but the rare
                    # entry whose value needs a DIFFERENT flag on the false
                    # branch (rather than being omitted), e.g. TomoImport's
                    # dose_rate, names one via flag_if_condition_false.
                    alt_flag = draft_flag_if_condition_false_for(base_name, key)
                    if alt_flag is None:
                        continue
                    mapped = alt_flag
                elif verdict is None:
                    unmapped.append(key)
                    continue
            flag = mapped
        else:
            # Always consult the exact flag+condition the extractor read
            # straight out of this job's own getCommands*Job() when one
            # exists for this key -- it's ground truth for both the real
            # flag name AND whatever actually guards it. Checking
            # flags_used FIRST (the raw "does '--<key>' appear anywhere in
            # the function" signal) used to short-circuit past this for
            # every option whose flag happens to equal "--" + its own key
            # (the common case), silently skipping the condition check --
            # confirmed for real: Autorefine's/Class3D's helical_nr_asu,
            # helical_twist_initial and 6 similar fields all have flag ==
            # "--" + key, so they were ALWAYS emitted unconditionally even
            # with "Do helical reconstruction?" unchecked (their real
            # condition depends on do_helix/do_apply_helical_symmetry/
            # do_local_search_helical_symmetry, not on themselves). An
            # audit against every job's option_flags found 72 fields with
            # this exact shape.
            pair = option_flags.get(key)
            if pair is not None:
                condition = pair.get("condition", "")
                if _self_guarded(condition, key):
                    flag = pair["flag"]
                elif "||" in condition:
                    # The extractor builds this condition string by
                    # concatenating RELION's own nested `if` blocks with
                    # `&&`, which silently drops the parens around an OUTER
                    # condition that itself contains a top-level `||`
                    # (confirmed for real: MultiBody's gpu_ids/nr_pool/
                    # nr_threads/scratch_dir and DynaMight's fn_star and
                    # friends are actually `if (OUTER_WITH_OR) { ... if
                    # (INNER) ... } }`, extracted as the flattened, wrongly-
                    # associating text "OUTER_WITH_OR && INNER" -- see
                    # pipeline_jobs.cpp's getCommandsMultiBodyJob ~111-196).
                    # _evaluate_condition's OR support (issue #15) assumes
                    # its input is a faithful transcription of one real `if`
                    # condition -- true for job_catalog.DRAFT_OVERRIDES'
                    # hand-verified mapped_condition (checked above, at the
                    # "not None" branch of `mapped`), NOT reliably true for
                    # this extracted, auto-flattened text. Stay deferred to
                    # "unmapped" for any extracted condition containing
                    # `||`, exactly as before OR support existed, rather
                    # than risk replaying a condition whose grouping isn't
                    # actually what RELION's source means.
                    unmapped.append(key)
                    continue
                else:
                    # Not self-guarded doesn't have to mean "give up": this
                    # app has the SAME field_values RELION's own
                    # getCommands*Job() would read, so replay simple
                    # boolean-gate conditions (see _evaluate_condition)
                    # against them instead of guessing.
                    verdict = _evaluate_condition(
                        condition, field_values, options_by_key.keys()
                    )
                    if verdict is True:
                        flag = pair["flag"]
                    elif verdict is False:
                        # RELION itself wouldn't emit this either right
                        # now -- the gate it depends on reads false.
                        # Silently omit (like draft_is_suppressed above),
                        # not "unmapped": there's nothing here to fix.
                        continue
                    else:
                        unmapped.append(key)
                        continue
            else:
                # No option_flags entry at all -- the extractor found no
                # clean `command += " --flag " + joboptions["key"]`
                # assignment to read a real flag/condition from. Fall back
                # to the blunt "does '--<key>' appear anywhere in this
                # function" signal (today's only remaining information),
                # treated as unconditional.
                flag = f"--{key}"
                if flag not in flags_used:
                    unmapped.append(key)
                    continue

        ft = option["field_type"]
        if ft == "boolean":
            emit = bool(value)
            if mapped is not None and draft_flag_is_negated(base_name, key):
                # A handful of curated flags fire when the checkbox is
                # UNCHECKED, not checked (RELION's own source guards them
                # with `if (!joboptions["key"].getBoolean())`, e.g.
                # "Use parallel disc I/O?" -> --no_parallel_disc_io only
                # when that box is OFF). See job_catalog.FlagOverride.negated.
                emit = not emit
            if emit:
                parts.append(flag)
            # RELION convention: absent flag == false; nothing to append otherwise
        else:
            if value is None or value == "":
                if not (key == "gpu_ids" and flag == "--gpu"):
                    continue
                # RELION emits `--gpu ""` (letting the job auto-allocate)
                # whenever "Use GPU acceleration?" is checked, even with
                # "Which GPUs to use" left blank -- confirmed via
                # pipeline_jobs.cpp's `command += " --gpu \"" +
                # joboptions["gpu_ids"].getString() + "\"";`, unconditional
                # on gpu_ids's own value once the use_gpu gate (evaluated
                # above, since flag can only resolve to "--gpu" here via
                # that gate reading true) holds. An empty string is a
                # meaningful, intentional value for this field, not "unset"
                # -- so it's passed through rather than skipped, unlike
                # every other text field.
                value = ""
            if has_draft_value_transform(base_name, key):
                # This field's value is a human-facing radio-button label
                # (e.g. "No rotation (0)"), not what RELION's own program
                # actually parses -- see job_catalog.DRAFT_OVERRIDES for why
                # passing the label through crashes relion_run_motioncorr.
                translated = draft_value_for(base_name, key, str(value))
                if translated is None:
                    unmapped.append(key)
                    continue
                value = translated
            elif has_draft_numeric_transform(base_name, key):
                # This field's raw value needs a RELION-side computation
                # (clamp, divide, positivity gate) before it's what the
                # program actually parses -- see
                # job_catalog.JobDraftOverride.numeric_transforms.
                try:
                    numeric_value = float(value)
                except (TypeError, ValueError):
                    unmapped.append(key)  # can't confidently resolve
                    continue
                transformed = draft_numeric_value_for(base_name, key, numeric_value)
                if transformed is None:
                    # RELION's own guard on the COMPUTED value (e.g.
                    # helical_range_distance <= 0) -- correctly omit this
                    # flag, not "can't resolve" (not unmapped).
                    continue
                value = transformed
            parts.append(flag)
            parts.append(shlex.quote(str(value)))

    # A job-level hook for values built from MULTIPLE fields with
    # computed/branch-dependent logic no single FlagOverride/value_
    # transform/numeric_transform can express -- e.g. Extract's
    # --bg_radius, computed from bg_diameter/extract_size and (when
    # do_rescale is on) rescale together. See
    # job_catalog.JobDraftOverride.extra_flags.
    parts.extend(draft_extra_flags(base_name, field_values, output_subdir))

    # RELION appends this verbatim at the end of the command
    # (`command += " " + joboptions["other_args"].getString();`) -- deliberately
    # unquoted, since the whole point is to pass raw extra arguments.
    extra = str(field_values.get("other_args") or "").strip()
    if extra:
        parts.append(extra)

    primary_command = " ".join(parts)
    # Complete, independent shell commands this job needs to run BEFORE the
    # one just built (e.g. Localres's ResMap-mode symlinks) -- see
    # job_catalog.JobDraftOverride.commands_before's own docstring for why
    # these are joined with " && " exactly like real RELION's own
    # prepareFinalCommand joins multiple commands.push_back() calls.
    before_commands = draft_commands_before(base_name, field_values, output_subdir)
    if before_commands:
        return " && ".join([*before_commands, primary_command]), unmapped
    return primary_command, unmapped


def _resolve_tomo_variant(internal_name: str) -> tuple[str, bool]:
    """(base_internal_name, is_tomo) for a job_catalog.TOMO_VARIANT_OF entry
    (TomoMotioncorr/TomoCtffind -- see its docstring), or (internal_name,
    False) unchanged for every other job type.

    data/job_definitions_raw.json and job_catalog.DRAFT_OVERRIDES are both
    keyed by the real RELION job CLASS (one entry each for Motioncorr/
    Ctffind, since RELION itself has only the one RelionJob class for
    either) -- callers use base_internal_name for those lookups, and
    is_tomo for field_values["is_tomo"] (see _build_draft_command below and
    job_runner._register_in_relion_pipeline), so which of the two menu
    entries the user actually picked is threaded through by VALUE rather
    than by giving the Tomo variant its own duplicate options table.
    """
    base = TOMO_VARIANT_OF.get(internal_name)
    if base:
        return base, True
    return internal_name, False


def raw_job(internal_name: str) -> dict:
    """The extracted RELION data for one job, as-is. Used by the Advanced
    section, which needs the job's program name and its already-exposed flags."""
    base, _is_tomo = _resolve_tomo_variant(internal_name)
    return _load_raw()[base]


def build_job_definition(internal_name: str, output_subdir: str = "") -> dict:
    base, _is_tomo = _resolve_tomo_variant(internal_name)
    raw = _load_raw()[base]
    meta = JOB_CATALOG[internal_name]  # (label_new, display_name, category, description) -- own entry, not base's
    label_new, display_name, category, description = meta

    # This app's own additions on top of RELION's real extracted options
    # (e.g. ModelAngelo's mask_path) -- see job_catalog.SYNTHETIC_OPTIONS
    # for why this exists at all and why it's kept separate from
    # DRAFT_OVERRIDES. [] for every job except the handful listed there, so
    # this is a no-op for the rest. Builds a shallow-copied `raw` variant
    # (never mutate raw["options"]/raw["tab_layout"] in place -- _load_raw()
    # caches and shares them across every request) with the synthetic
    # field(s) appended to both the options list and the "I/O" tab group,
    # so they render in the popup exactly like a real RELION field would.
    # Deliberately NOT passed to _build_draft_command below: a synthetic
    # field is handled entirely by its own job's program_extra/extra_flags
    # override (which reads field_values directly), never by the generic
    # per-option loop -- that loop only sees fields it's told about via
    # raw_job.get("options"), so leaving it out of `raw` itself is what
    # keeps it out of that loop, not an oversight.
    synthetic = synthetic_options(internal_name)
    raw_with_synthetic = raw
    if synthetic:
        raw_with_synthetic = dict(raw)
        raw_with_synthetic["options"] = [*raw.get("options", []), *synthetic]
        layout = raw.get("tab_layout")
        if layout and layout.get("tab_fields", {}).get("I/O"):
            raw_with_synthetic["tab_layout"] = {
                **layout,
                "tab_fields": {
                    **layout["tab_fields"],
                    "I/O": [*layout["tab_fields"]["I/O"], *(o["key"] for o in synthetic)],
                },
            }

    standard_groups = _standard_groups(raw_with_synthetic)
    options_by_key = {o["key"]: o for o in raw_with_synthetic.get("options", [])}
    default_values = {k: _field_default_value(o) for k, o in options_by_key.items()}
    draft_command, unmapped = _build_draft_command(
        raw, default_values, internal_name, output_subdir
    )
    # Copy (never mutate raw["options"] in place -- _load_raw() caches and
    # shares it across every request) any option this specific menu entry
    # wants offered as an explicit two-way dropdown instead of a checkbox --
    # see job_catalog.boolean_select_labels's own docstring.
    options = []
    for opt in raw_with_synthetic.get("options", []):
        labels = boolean_select_labels(internal_name, opt["key"])
        if labels:
            opt = {**opt, "boolean_labels": {"false": labels[0], "true": labels[1]}}
        options.append(opt)

    return {
        "internal_name": internal_name,
        "label_new": label_new,
        "display_name": display_name,
        "category": category,
        "description": description,
        "options": options,
        "standard_groups": standard_groups,
        "default_values": default_values,
        "program_guess": raw.get("program_guess"),
        "program_mpi": raw.get("program_mpi"),
        "flags_used": raw.get("flags_used", []),
        "commands_source": raw.get("commands_source", ""),
        "draft_command": draft_command,
        "unmapped_fields": unmapped,
        "output_subdir": output_subdir,
        "is_custom": False,
        "pipeline_type": pipeline_type(internal_name),
    }


def list_catalog() -> list[dict]:
    """Summary list for the Jobs sidebar: id, display name, category — no
    heavy per-job field data (fetched separately per job on open)."""
    out = []
    for internal_name, (label_new, display_name, category, description) in JOB_CATALOG.items():
        out.append(
            {
                "internal_name": internal_name,
                "label_new": label_new,
                "display_name": display_name,
                "category": category,
                "description": description,
                "is_custom": False,
                "pipeline_type": pipeline_type(internal_name),
            }
        )
    for internal_name, meta in CUSTOM_JOBS.items():
        out.append(
            {
                "internal_name": internal_name,
                "label_new": meta["label_new"],
                "display_name": meta["display_name"],
                "category": meta["category"],
                "description": meta["description"],
                "is_custom": True,
                "pipeline_type": pipeline_type(internal_name),
            }
        )
    return out


def categories() -> list[str]:
    return CATEGORIES
