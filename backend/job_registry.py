"""
job_registry.py — combines job_catalog.py (curated display metadata) with
data/job_definitions_raw.json (extracted verbatim from RELION source, see
data/extract_job_definitions.py) into the structure the API and frontend
consume: one JobDefinition per job type, with fields split into "standard"
(RELION's own first GUI tab for that job — usually I/O) and "advanced"
(every later tab, kept as named groups) per the user's request for
"standard inputs the way relion does" plus "access to all the options via
an advanced menu".

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

import json
import re
import shlex
from pathlib import Path
from typing import Any

from job_catalog import CATEGORIES, CUSTOM_JOBS, JOB_CATALOG

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load_raw() -> dict[str, Any]:
    with open(DATA_DIR / "job_definitions_raw.json") as f:
        return json.load(f)


def _split_standard_advanced(raw_job: dict) -> tuple[list[str], dict[str, list[str]]]:
    """
    Returns (standard_field_keys, {advanced_tab_name: [field_keys]}).
    Standard = RELION's own first tab for this job (tab_layout.tab_order[0]).
    Advanced = every other real RELION tab, preserved as named groups.
    Falls back to "all fields standard, no advanced" if this job has no
    parsed tab_layout (shouldn't happen for the 32 real RELION jobs; will
    happen for the 3 custom import jobs, which define their own layout).
    """
    layout = raw_job.get("tab_layout")
    if not layout or not layout.get("tab_order"):
        all_keys = [o["key"] for o in raw_job.get("options", [])]
        return all_keys, {}

    tab_order = layout["tab_order"]
    tab_fields = layout["tab_fields"]
    standard = list(tab_fields.get(tab_order[0], []))
    advanced = {name: tab_fields[name] for name in tab_order[1:] if tab_fields.get(name)}
    return standard, advanced


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


def _build_draft_command(raw_job: dict, field_values: dict[str, Any]) -> tuple[str, list[str]]:
    """
    Returns (draft_command_string, unmapped_field_keys).
    See module docstring for the rule this follows.
    """
    program = raw_job.get("program_guess") or "<unknown_program>"
    # A few jobs (DynaMight, ModelAngelo, External) don't hard-code a
    # binary — they run whatever executable path the user configured in a
    # JobOption (e.g. "Location of DynaMight executable:"). extract_job_
    # definitions.py surfaces that as the placeholder "{joboptions.<key>}";
    # resolve it against the actual field values here.
    placeholder_match = re.match(r"^\{joboptions\.([A-Za-z0-9_]+)\}$", program)
    if placeholder_match:
        exe_key = placeholder_match.group(1)
        program = field_values.get(exe_key) or f"<set the '{exe_key}' field to this job's executable path>"
    flags_used = set(raw_job.get("flags_used", []))
    options_by_key = {o["key"]: o for o in raw_job.get("options", [])}

    parts = [program]
    unmapped = []

    for key, value in field_values.items():
        option = options_by_key.get(key)
        if option is None:
            continue
        flag = f"--{key}"
        if flag not in flags_used:
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

    return " ".join(parts), unmapped


def build_job_definition(internal_name: str) -> dict:
    raw = _load_raw()[internal_name]
    meta = JOB_CATALOG[internal_name]  # (label_new, display_name, category, description)
    label_new, display_name, category, description = meta

    standard_keys, advanced_groups = _split_standard_advanced(raw)
    options_by_key = {o["key"]: o for o in raw.get("options", [])}
    default_values = {k: _field_default_value(o) for k, o in options_by_key.items()}
    draft_command, unmapped = _build_draft_command(raw, default_values)

    return {
        "internal_name": internal_name,
        "label_new": label_new,
        "display_name": display_name,
        "category": category,
        "description": description,
        "options": raw.get("options", []),
        "standard_fields": standard_keys,
        "advanced_groups": advanced_groups,
        "default_values": default_values,
        "program_guess": raw.get("program_guess"),
        "flags_used": raw.get("flags_used", []),
        "commands_source": raw.get("commands_source", ""),
        "draft_command": draft_command,
        "unmapped_fields": unmapped,
        "is_custom": False,
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
            }
        )
    return out


def categories() -> list[str]:
    return CATEGORIES
