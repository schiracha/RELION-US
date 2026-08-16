"""
job_registry.py — combines job_catalog.py (curated display metadata) with
data/job_definitions_raw.json (extracted verbatim from RELION source, see
data/extract_job_definitions.py) into the structure the API and frontend
consume: one JobDefinition per job type.

Field placement follows one rule: **everything RELION's own GUI shows goes in
the popup's top panel**, grouped under RELION's own tab names (I/O, CTF,
Optimisation, ..., Running) and in RELION's own order — `standard_groups`.
The popup's "Advanced" tab is for the opposite thing: command-line options the
program accepts but the GUI never exposes, the ones you would otherwise find by
running the binary with `--help` or reading the source. Those are discovered at
runtime from the installed RELION (see program_help.py), not from this file.

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
    draft_flag_for,
    draft_is_suppressed,
    draft_output_flag,
    draft_program_override,
    pipeline_type,
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

    Every field RELION's GUI shows goes in the job popup's top panel, grouped
    under RELION's own tab names (I/O, CTF, Optimisation, ..., Running) and in
    RELION's own order. The popup's "Advanced" tab is NOT for these -- it is
    for command-line options the GUI never exposes (see program_help.py).

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
    referenced = set(re.findall(r'joboptions\[\s*"([A-Za-z0-9_]+)"\s*\]', condition))
    if referenced - {key}:
        return False
    # A condition with no joboptions reference at all (e.g. `!is_continue`)
    # is about job state, not this field -- treat it as a real branch.
    return bool(referenced)


def _build_draft_command(
    raw_job: dict,
    field_values: dict[str, Any],
    internal_name: str = "",
    output_subdir: str = "",
) -> tuple[str, list[str]]:
    """
    Returns (draft_command_string, unmapped_field_keys).
    See module docstring for the rule this follows.

    Two curated, source-verified overlays (job_catalog.DRAFT_PROGRAM_OVERRIDE
    and DRAFT_FLAG_MAP) correct the handful of jobs the generic rule gets
    wrong — RELION-5's Python tomo tools, whose hyphenated CLI flags
    (`--tilt-image-movie-pattern`) don't match their snake_case option keys
    (`movie_files`), and TomoImport, whose extracted program was the wrong
    (do_coords) branch. A mapped flag is authoritative: it's always emitted,
    bypassing the flags_used membership test (unreliable for these jobs).

    output_subdir: if given (e.g. "Import/job005"), the RELION-style output
    flag (`--o <subdir>/`, or `--output-directory` for the Python tomo
    tools) is inserted right after the program — matching RELION, which runs
    from the project root and passes the job's output directory as a
    project-root-relative path. Omitted for exe-placeholder jobs
    (DynaMight/ModelAngelo/External), whose output conventions differ and
    which this app doesn't try to guess.
    """
    program = draft_program_override(internal_name) or raw_job.get("program_guess") or "<unknown_program>"
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
    if nr_mpi > 1 and program_mpi and not is_exe_placeholder:
        # Same two conditions RELION applies: an _mpi binary exists for this
        # job, and more than one process was asked for.
        program = program_mpi
        prefix = _mpirun_prefix(nr_mpi)

    parts = prefix + [program]
    # RELION-style output directory (project-root-relative), inserted right
    # after the program name — mirrors how getCommands*Job() appends it.
    if output_subdir and not is_exe_placeholder:
        subdir_arg = output_subdir if output_subdir.endswith("/") else output_subdir + "/"
        parts.append(draft_output_flag(internal_name))
        parts.append(shlex.quote(subdir_arg))
    unmapped = []

    for key, value in field_values.items():
        option = options_by_key.get(key)
        if option is None:
            continue
        # Options belonging to a non-default branch are omitted from the
        # default draft entirely (and not counted as unmapped).
        if draft_is_suppressed(internal_name, key):
            continue
        # Handled outside this loop, exactly as RELION handles them.
        if key in ("nr_mpi", "other_args"):
            continue
        # A verified per-job flag override wins and is always emitted; only
        # fall back to the generic `--<key>` rule (gated on flags_used) when
        # no override exists for this key.
        mapped = draft_flag_for(internal_name, key)
        if mapped is not None:
            flag = mapped
        else:
            flag = f"--{key}"
            if flag not in flags_used:
                # Second chance: this job's own builder may append the option
                # under a flag that isn't "--" + key (--i for input_star_mics,
                # --Box for box, --j for nr_threads), extracted verbatim from
                # the source. Only when it isn't inside a branch that depends
                # on some *other* option -- those stay unmapped rather than
                # producing a command with contradictory flags.
                pair = option_flags.get(key)
                if pair and _self_guarded(pair.get("condition", ""), key):
                    flag = pair["flag"]
                else:
                    unmapped.append(key)
                    continue

        ft = option["field_type"]
        if ft == "boolean":
            if value:
                parts.append(flag)
            # RELION convention: absent flag == false; nothing to append otherwise
        else:
            if value is None or value == "":
                continue
            parts.append(flag)
            parts.append(shlex.quote(str(value)))

    # RELION appends this verbatim at the end of the command
    # (`command += " " + joboptions["other_args"].getString();`) -- deliberately
    # unquoted, since the whole point is to pass raw extra arguments.
    extra = str(field_values.get("other_args") or "").strip()
    if extra:
        parts.append(extra)

    return " ".join(parts), unmapped


def raw_job(internal_name: str) -> dict:
    """The extracted RELION data for one job, as-is. Used by the Advanced tab,
    which needs the job's program name and its already-exposed flags."""
    return _load_raw()[internal_name]


def build_job_definition(internal_name: str, output_subdir: str = "") -> dict:
    raw = _load_raw()[internal_name]
    meta = JOB_CATALOG[internal_name]  # (label_new, display_name, category, description)
    label_new, display_name, category, description = meta

    standard_groups = _standard_groups(raw)
    options_by_key = {o["key"]: o for o in raw.get("options", [])}
    default_values = {k: _field_default_value(o) for k, o in options_by_key.items()}
    draft_command, unmapped = _build_draft_command(
        raw, default_values, internal_name, output_subdir
    )

    return {
        "internal_name": internal_name,
        "label_new": label_new,
        "display_name": display_name,
        "category": category,
        "description": description,
        "options": raw.get("options", []),
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
