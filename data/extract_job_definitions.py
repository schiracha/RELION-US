#!/usr/bin/env python3
"""
extract_job_definitions.py — parse RELION's own src/pipeline_jobs.cpp to build
a ground-truth JSON description of every job type: its JobOptions (the fields
RELION's GUI shows), and the real C++ source of its getCommands*Job() function
(the logic that actually assembles the command line).

Why this exists: hand-transcribing ~30 job types x ~250 lines of C++ each,
from memory or by eye, is exactly the kind of task that produces subtly wrong
option flags — which is the whole problem the user is trying to get away
from. Parsing the real source programmatically instead means every field
label, default value, and help string in the generated app traces back to an
exact line in RELION's own code, and is easy to re-run against a newer RELION
version later.

What this script extracts, per job (e.g. "Import", "MotionCorr", ...):
  - options: every `joboptions["key"] = JobOption(...)` call in
    initialise<Job>Job(), classified by which of JobOption's 6 constructor
    overloads was used (arg count + literal-type heuristics — the overload
    set is small and distinctive, see pipeline_jobs.h L696-711).
  - commands_source: the raw, unmodified C++ text of getCommands<Job>Job(),
    kept verbatim as a ground-truth reference (this is NOT transpiled to
    Python — RELION's option-assembly logic has real per-job branching that
    a mechanical transpile would risk getting subtly wrong; the app instead
    shows this alongside an editable draft command, per the user's request).
  - program_guess: the first `command = "..."` literal in that function,
    i.e. the actual relion_* binary invoked.
  - flags_used: every `--flag` literal appearing in that function, as a
    reference list (not a strict mapping — see docstring in job_registry.py
    for how this is used to build the best-effort draft command).

Run: python3 extract_job_definitions.py <path-to-relion-src>/pipeline_jobs.cpp <out.json>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def strip_line_splices(text: str) -> str:
    """Undo C++ backslash-newline line splicing, exactly as the preprocessor
    would, so multi-line string literals (common in help text) parse as one
    token."""
    return re.sub(r"\\\r?\n", "", text)


def strip_comments(text: str) -> str:
    """
    Remove // and /* */ comments while leaving string/char literals intact
    (a literal containing "//" or "/*" must not be treated as a comment).
    Replaces each comment with a single space so line/column-independent
    matching downstream doesn't need to know about them.
    """
    out = []
    i = 0
    n = len(text)
    in_string = False
    in_char = False
    escape = False
    while i < n:
        c = text[i]
        if escape:
            out.append(c)
            escape = False
            i += 1
            continue
        if in_string or in_char:
            out.append(c)
            if c == "\\":
                escape = True
            elif in_string and c == '"':
                in_string = False
            elif in_char and c == "'":
                in_char = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "'":
            in_char = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
            continue
        out.append(c)
        i += 1
    return "".join(out)


def find_matching_brace(text: str, open_pos: int) -> int:
    """Given the index of a '{', return the index of its matching '}'."""
    depth = 0
    i = open_pos
    in_string = False
    in_char = False
    escape = False
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
        elif c == "\\":
            escape = True
        elif in_string:
            if c == '"':
                in_string = False
        elif in_char:
            if c == "'":
                in_char = False
        elif c == '"':
            in_string = True
        elif c == "'":
            in_char = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"No matching brace found starting at {open_pos}")


def find_matching_paren(text: str, open_pos: int) -> int:
    depth = 0
    i = open_pos
    in_string = False
    escape = False
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
        elif c == "\\":
            escape = True
        elif in_string:
            if c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"No matching paren found starting at {open_pos}")


def split_top_level_args(arg_str: str) -> list[str]:
    """Split a C++ call's argument text on top-level commas (not inside
    nested parens or string literals)."""
    args = []
    depth = 0
    in_string = False
    escape = False
    current = []
    for c in arg_str:
        if escape:
            current.append(c)
            escape = False
            continue
        if c == "\\":
            current.append(c)
            escape = True
            continue
        if in_string:
            current.append(c)
            if c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            current.append(c)
            continue
        if c in "([":
            depth += 1
            current.append(c)
            continue
        if c in ")]":
            depth -= 1
            current.append(c)
            continue
        if c == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            continue
        current.append(c)
    if current:
        args.append("".join(current).strip())
    return args


CAST_PREFIX_RE = re.compile(r"^\(\s*std::string\s*\)\s*")
FUNC_CAST_RE = re.compile(r"^std::string\s*\((.*)\)$", re.DOTALL)


def unquote(s: str) -> str:
    s = s.strip()
    s = CAST_PREFIX_RE.sub("", s).strip()  # strip e.g. (std::string)"opticsGroup1" casts
    func_match = FUNC_CAST_RE.match(s)
    if func_match:
        s = func_match.group(1).strip()  # strip e.g. std::string("") -> ""
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        inner = s[1:-1]
        return inner.encode().decode("unicode_escape", errors="replace")
    return s


NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?f?$")


def classify_and_parse(args: list[str]) -> dict:
    """
    Classify a JobOption(...) call by its argument shape against the 6
    constructor overloads declared in pipeline_jobs.h (lines 696-711), and
    extract the fields the app's form renderer needs.
    """
    n = len(args)
    label = unquote(args[0]) if n >= 1 else ""

    # JobOption(label, bool, helptext)  -- BOOLEAN
    if n == 3 and args[1].strip() in ("true", "false"):
        return {
            "field_type": "boolean",
            "label": label,
            "default": args[1].strip() == "true",
            "help": unquote(args[2]),
        }

    # JobOption(label, float, float, float, float, helptext) -- SLIDER
    if n == 6 and all(NUMERIC_RE.match(a.strip().rstrip("f")) for a in args[1:5]):
        return {
            "field_type": "slider",
            "label": label,
            "default": float(args[1].rstrip("f")),
            "min": float(args[2].rstrip("f")),
            "max": float(args[3].rstrip("f")),
            "step": float(args[4].rstrip("f")),
            "help": unquote(args[5]),
        }

    # JobOption(label, vector<string> radio_options, ioption, helptext) -- RADIO
    if n == 4 and NUMERIC_RE.match(args[2].strip()):
        return {
            "field_type": "radio",
            "label": label,
            "options_ref": args[1].strip(),  # name of the static vector, e.g. job_nodetype_options
            "default_index": int(args[2].strip()),
            "help": unquote(args[3]),
        }

    # JobOption(label, default_string, helptext) -- ANY/onlytext
    if n == 3:
        return {
            "field_type": "text",
            "label": label,
            "default": unquote(args[1]),
            "help": unquote(args[2]),
        }

    # JobOption(label, default_value, pattern, directory, helptext) -- FILENAME
    if n == 5:
        return {
            "field_type": "filename",
            "label": label,
            "default": unquote(args[1]),
            "pattern": unquote(args[2]),
            "directory": unquote(args[3]),
            "help": unquote(args[4]),
        }

    # JobOption(label, nodetype, node_type_depth, default_value, pattern, helptext) -- INPUTNODE
    if n == 6:
        return {
            "field_type": "inputnode",
            "label": label,
            "nodetype": unquote(args[1]),
            "node_type_depth": args[2].strip(),
            "default": unquote(args[3]),
            "pattern": unquote(args[4]),
            "help": unquote(args[5]),
        }

    return {"field_type": "unknown", "label": label, "raw_args": args}


def extract_add_tomo_input_options_template(text: str) -> list[tuple[str | None, dict]]:
    """
    `RelionJob::addTomoInputOptions(has_tomograms, has_particles,
    has_trajectories, has_manifolds)` (pipeline_jobs.cpp ~L6315) is a shared
    helper that several tomography jobs call from inside their own
    initialise<X>Job() to add the "input optimisation set / OR: use direct
    entries" fields — a JobOption-side counterpart to the GUI's
    placeTomoInput() (see extract_tab_layout). Because it's a real function
    call, not inlined text, extract_joboptions() (which only scans within
    one function's own body) can't see these options at their call sites.

    Returns [(gating_param_name_or_None, parsed_option_dict), ...] in the
    order they're declared in the real function body, so callers can
    re-evaluate the same booleans used at each call site.
    """
    m = re.search(r"void RelionJob::addTomoInputOptions\([^)]*\)\s*\{", text)
    if not m:
        return []
    brace_open = text.index("{", m.end() - 1)
    brace_close = find_matching_brace(text, brace_open)
    body = text[brace_open + 1 : brace_close]

    results = []
    # Each line is either an unconditional joboptions[...] = JobOption(...)
    # or `if (has_X) joboptions[...] = JobOption(...)`.
    for line_match in re.finditer(
        r'(?:if\s*\(\s*(has_\w+)\s*\)\s*)?joboptions\["([A-Za-z0-9_]+)"\]\s*=\s*JobOption\(',
        body,
    ):
        gate, key = line_match.group(1), line_match.group(2)
        paren_open = line_match.end() - 1
        paren_close = find_matching_paren(body, paren_open)
        args = split_top_level_args(body[paren_open + 1 : paren_close])
        parsed = classify_and_parse(args)
        parsed["key"] = key
        results.append((gate, parsed))
    return results


TOMO_INPUT_PARAM_ORDER = ["has_tomograms", "has_particles", "has_trajectories", "has_manifolds"]


def expand_add_tomo_input_options_call(
    arg_text: str, template: list[tuple[str | None, dict]]
) -> list[dict]:
    args = [a.strip() for a in split_top_level_args(arg_text)]
    values = dict(zip(TOMO_INPUT_PARAM_ORDER, (a == "true" for a in args)))
    out = []
    for gate, option in template:
        if gate is None or values.get(gate):
            out.append(option)
    return out


def extract_joboptions(func_body: str) -> list[dict]:
    results = []
    for m in re.finditer(r'joboptions\["([A-Za-z0-9_]+)"\]\s*=\s*JobOption\(', func_body):
        key = m.group(1)
        paren_open = m.end() - 1
        paren_close = find_matching_paren(func_body, paren_open)
        arg_text = func_body[paren_open + 1 : paren_close]
        args = split_top_level_args(arg_text)
        parsed = classify_and_parse(args)
        parsed["key"] = key
        results.append(parsed)
    return results


def extract_program_guess(func_body: str) -> str | None:
    m = re.search(r'command\s*=\s*"([^"]*)"', func_body)
    if m:
        return m.group(1).strip()
    # Some jobs (DynaMight, ModelAngelo, External) don't hard-code a binary;
    # they run whatever executable path the user set in a JobOption (a
    # "Location of X executable:" field), e.g.
    # `command = joboptions["fn_dynamight_exe"].getString();`. Surface that
    # as a resolvable placeholder rather than silently returning None, so
    # the app can substitute the user's actual configured path.
    m = re.search(r'command\s*=\s*joboptions\["([A-Za-z0-9_]+)"\]\.getString\(\)', func_body)
    if m:
        return "{joboptions." + m.group(1) + "}"
    return None


def extract_flags_used(func_body: str) -> list[str]:
    # Allow hyphens INSIDE a flag body, not just underscores. RELION's newer
    # Python tomo tools (relion_python_tomo_import / _pick / _denoise /
    # _exclude_tilt_images) and DynaMight build hyphenated multi-word flags
    # like `--tilt-image-movie-pattern`, `--nominal-pixel-size`,
    # `--output-directory`. The old pattern `--[A-Za-z0-9_]+` stopped at the
    # first hyphen and captured a truncated, wrong flag (`--tilt`, `--nominal`,
    # `--output`), which then never matched any snake_case option key and
    # produced misleading draft commands. A flag starts with a letter after
    # the `--`, then allows letters/digits/underscores/hyphens.
    flags = re.findall(r'"\s*(--[A-Za-z][A-Za-z0-9_-]*)', func_body)
    # preserve order, dedupe
    seen = set()
    out = []
    for f in flags:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def extract_static_option_vectors(header_text: str) -> dict[str, list[str]]:
    """
    Extract the named `static const std::vector<std::string> job_X_options{...}`
    definitions from pipeline_jobs.h, so radio-field options_ref names (e.g.
    "job_nodetype_options") can be resolved to their real choice lists.
    """
    vectors = {}
    for m in re.finditer(
        r"static const std::vector<std::string>\s+(\w+)\s*\{", header_text
    ):
        name = m.group(1)
        brace_open = header_text.index("{", m.end() - 1)
        # simple brace matching is fine here, no nested braces in these literals
        brace_close = header_text.index("}", brace_open)
        body = header_text[brace_open + 1 : brace_close]
        items = [unquote(a) for a in split_top_level_args(body) if a.strip()]
        vectors[name] = items
    return vectors


def extract_tab_layout(gui_text: str) -> dict[str, dict]:
    """
    Parse src/gui_jobwindow.cpp's `void JobWindow::initialise<Job>Window()`
    functions to recover RELION's own real tab grouping of fields: which
    JobOption keys appear under which tab (RELION's first tab is
    consistently the most essential/I-O-like fields; later tabs hold
    job-specific and computation-resource options). This is the ground
    truth used for this app's Standard (= first tab) / Advanced (= every
    later tab, kept as named groups) split — not an invented heuristic.

    Pattern recognized, in document order within each function body:
      tabN->label("Some Label");   <- starts a new named group
      place("some_key", ...);      <- assigns the current field to that group
    """
    # placeTomoInput(has_tomograms, has_particles, has_trajectories, has_manifolds)
    # is a shared helper (defined once, gui_jobwindow.cpp ~L2523) that several
    # tomography jobs call instead of individual place("key", ...) calls; a
    # plain place("key") regex misses everything it adds. Expand it inline
    # using its own real definition (which fields it places, and in what
    # order, for each boolean flag) rather than hard-coding the field list,
    # so this stays correct if a future RELION version changes which flags
    # are passed at each call site.
    placetomoinput_def_fields = ["in_optimisation", "use_direct_entries"]
    # conditional fields in the order placeTomoInput's body adds them,
    # keyed by (arg_index, joboption_key) — see the has_particles/
    # has_tomograms/has_trajectories/has_manifolds parameter order and the
    # body's if-blocks (gui_jobwindow.cpp placeTomoInput()).
    placetomoinput_conditional = [
        ("has_particles", "in_particles"),
        ("has_tomograms", "in_tomograms"),
        ("has_trajectories", "in_trajectories"),
        ("has_manifolds", "in_manifolds"),
    ]

    def expand_place_tomo_input(arg_text: str) -> list[str]:
        args = [a.strip() for a in split_top_level_args(arg_text)]
        fields = ["in_optimisation", "use_direct_entries"]
        # Call signature is placeTomoInput(has_tomograms, has_particles,
        # has_trajectories, has_manifolds) but the BODY checks them in the
        # order has_particles, has_tomograms, has_trajectories, has_manifolds
        # (see gui_jobwindow.cpp) — map positionally by the declared
        # parameter order, then emit in body order.
        param_order = ["has_tomograms", "has_particles", "has_trajectories", "has_manifolds"]
        values = dict(zip(param_order, args))
        for param_name, field_key in placetomoinput_conditional:
            if values.get(param_name, "false").strip() == "true":
                fields.append(field_key)
        return fields

    layouts = {}
    combined_re = re.compile(
        r'tab\d+->label\("([^"]*)"\)'
        r'|place2\("([A-Za-z0-9_]+)"\s*,\s*"([A-Za-z0-9_]+)"'  # must precede place( below
        r'|place\("([A-Za-z0-9_]+)"'
        r'|placeTomoInput\(([^;]*?)\)\s*;'
    )
    for m in re.finditer(r"void JobWindow::initialise(\w+)Window\(\)\s*\{", gui_text):
        job_name = m.group(1)
        brace_open = gui_text.index("{", m.end() - 1)
        brace_close = find_matching_brace(gui_text, brace_open)
        body = gui_text[brace_open + 1 : brace_close]

        tabs_ordered = []
        tab_fields: dict[str, list[str]] = {}
        current_tab = None

        def ensure_tab(name: str) -> str:
            if name not in tab_fields:
                tab_fields[name] = []
                tabs_ordered.append(name)
            return name

        for sub in combined_re.finditer(body):
            tab_label, place2_a, place2_b, field_key, tomo_input_args = (
                sub.group(1), sub.group(2), sub.group(3), sub.group(4), sub.group(5)
            )
            if tab_label is not None:
                current_tab = tab_label
                ensure_tab(current_tab)
            elif place2_a is not None:
                target = ensure_tab(current_tab if current_tab is not None else "(ungrouped)")
                for k in (place2_a, place2_b):
                    if k not in tab_fields[target]:
                        tab_fields[target].append(k)
            elif field_key is not None:
                target = ensure_tab(current_tab if current_tab is not None else "(ungrouped)")
                if field_key not in tab_fields[target]:
                    tab_fields[target].append(field_key)
            elif tomo_input_args is not None:
                target = ensure_tab(current_tab if current_tab is not None else "(ungrouped)")
                for field_key in expand_place_tomo_input(tomo_input_args):
                    if field_key not in tab_fields[target]:
                        tab_fields[target].append(field_key)

        layouts[job_name] = {"tab_order": tabs_ordered, "tab_fields": tab_fields}
    return layouts


def main() -> int:
    if len(sys.argv) != 5:
        print(
            f"Usage: {sys.argv[0]} <pipeline_jobs.cpp> <pipeline_jobs.h> "
            f"<gui_jobwindow.cpp> <out.json>",
            file=sys.stderr,
        )
        return 1

    src_path = Path(sys.argv[1])
    header_path = Path(sys.argv[2])
    gui_path = Path(sys.argv[3])
    out_path = Path(sys.argv[4])
    text = strip_comments(strip_line_splices(src_path.read_text()))
    header_text = strip_comments(strip_line_splices(header_path.read_text()))
    gui_text = strip_comments(strip_line_splices(gui_path.read_text()))
    option_vectors = extract_static_option_vectors(header_text)
    tab_layouts = extract_tab_layout(gui_text)
    tomo_input_template = extract_add_tomo_input_options_template(text)

    jobs = {}

    for m in re.finditer(r"void RelionJob::initialise(\w+)Job\(\)\s*\{", text):
        job_name = m.group(1)
        brace_open = text.index("{", m.end() - 1)
        brace_close = find_matching_brace(text, brace_open)
        body = text[brace_open + 1 : brace_close]
        options = extract_joboptions(body)
        for opt in options:
            if opt["field_type"] == "radio" and opt.get("options_ref") in option_vectors:
                opt["options"] = option_vectors[opt["options_ref"]]

        # Expand any addTomoInputOptions(...) call in this job's own
        # initialise body (see extract_add_tomo_input_options_template) —
        # these add real JobOptions (in_optimisation, in_particles, ...)
        # that a plain scan of this function's own text would miss.
        existing_keys = {o["key"] for o in options}
        for call_m in re.finditer(r"addTomoInputOptions\(([^;]*?)\)\s*;", body):
            for opt in expand_add_tomo_input_options_call(call_m.group(1), tomo_input_template):
                if opt["key"] not in existing_keys:
                    options.append(opt)
                    existing_keys.add(opt["key"])

        jobs.setdefault(job_name, {})["options"] = options

    for m in re.finditer(r"bool RelionJob::getCommands(\w+)Job\(", text):
        job_name = m.group(1)
        paren_open = text.index("(", m.end() - 1)
        paren_close = find_matching_paren(text, paren_open)
        brace_open = text.index("{", paren_close)
        brace_close = find_matching_brace(text, brace_open)
        full_func = text[m.start() : brace_close + 1]
        body = text[brace_open + 1 : brace_close]
        entry = jobs.setdefault(job_name, {})
        entry["commands_source"] = full_func
        entry["program_guess"] = extract_program_guess(body)
        entry["flags_used"] = extract_flags_used(body)

    # gui_jobwindow.cpp's JobWindow::initialise<X>Window() names don't all
    # match pipeline_jobs.cpp's RelionJob::initialise<X>Job() names exactly
    # (verified by diffing the two name lists directly against this RELION
    # checkout): reconcile the three known mismatches rather than silently
    # creating orphan entries with 0 options.
    GUI_TO_JOB_NAME_ALIASES = {
        "Locres": "Localres",
        "TomoAlignTiltseries": "TomoAlignTiltSeries",
        "TomoReconPar": "TomoReconPart",
    }
    for job_name, layout in tab_layouts.items():
        canonical = GUI_TO_JOB_NAME_ALIASES.get(job_name, job_name)
        jobs.setdefault(canonical, {})["tab_layout"] = layout

    out_path.write_text(json.dumps(jobs, indent=2))
    n_options = sum(len(j.get("options", [])) for j in jobs.values())
    print(f"Extracted {len(jobs)} job types, {n_options} total JobOptions -> {out_path}")
    for name, j in jobs.items():
        print(f"  {name}: {len(j.get('options', []))} options, program={j.get('program_guess')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
