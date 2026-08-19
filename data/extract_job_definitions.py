#!/usr/bin/env python3
#
# Part of RELION-US. Copyright (C) 2026 the RELION-US authors.
# Licensed under the GNU General Public License v2 or later; see LICENSE.
#
# The JSON this script produces embeds material copied verbatim from RELION
# (C) MRC Laboratory of Molecular Biology, GPL-2.0-or-later. See NOTICE.md.
#
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
        # `unicode_escape` decodes LATIN-1, so encoding as UTF-8 first mangles
        # every non-ASCII character in RELION's help text (U+2212 MINUS became
        # "\xe2\x88\x92", U+2013 EN DASH became "\xe2\x80\x93" -- both were
        # present in the shipped JSON). Encoding latin-1 with backslashreplace
        # keeps the C escapes (\n, \t, \") interpreted while letting real
        # non-ASCII characters survive the round trip intact.
        return inner.encode("latin-1", "backslashreplace").decode(
            "unicode_escape", errors="replace"
        )
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


def _match_paren_backwards(text: str, close_pos: int) -> int:
    """Index of the '(' matching the ')' at close_pos, or -1."""
    depth = 0
    i = close_pos
    while i >= 0:
        if text[i] == ")":
            depth += 1
        elif text[i] == "(":
            depth -= 1
            if depth == 0:
                return i
        i -= 1
    return -1


def _condition_before(text: str, pos: int) -> str | None:
    """The `if (...)` / `else if (...)` / `else` guarding whatever starts at
    `pos`, or None if nothing does.

    Returns the condition text, or "else" for a bare else (which is a guard
    even though it has no condition of its own). Matching the parenthesis
    backwards rather than regex-scanning a fixed window matters: RELION's
    conditions nest parens and span lines, and a greedy `[^{]*` pattern
    swallows whole statements and reports them as the condition.
    """
    j = pos - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return None
    if text[j] == ")":
        open_p = _match_paren_backwards(text, j)
        if open_p == -1:
            return None
        if re.search(r"\bif\s*$", text[:open_p]):
            return text[open_p + 1 : j].strip()
        return None
    if re.search(r"\belse$", text[: j + 1]):
        return "else"
    return None


def enclosing_conditions(text: str, pos: int) -> list[str]:
    """Conditions of every construct that guards `pos`.

    Covers both forms RELION uses: braced blocks, and the brace-less
    `else if (x) command += ...;` one-liners that are common in these
    builders. Missing the brace-less form is not cosmetic -- it reports a
    branch-only flag as unconditional, and the draft then emits mutually
    contradictory options.
    """
    stack: list[str] = []
    i = 0
    while i < pos and i < len(text):
        c = text[i]
        if c == "{":
            stack.append(_condition_before(text, i) or "")
        elif c == "}":
            if stack:
                stack.pop()
        i += 1

    # ...plus a brace-less guard on the statement itself: RELION writes
    # `else if (scratch_dir != "") command += " --scratch_dir " + ...;`
    # with no braces, and treating that as unconditional is how a
    # branch-only flag ends up in every draft.
    immediate = _condition_before(text, pos)
    if immediate is not None:
        stack.append(immediate)
    return stack


# A literal guarded by one of these is not what the job runs by default:
#   is_continue -> only when continuing/re-running an existing job
#   nr_mpi      -> the parallel binary, chosen only when MPI procs > 1
# Taking the first literal in the function regardless is how the extractor
# used to report `relion_manualpick` as Autopick's program (Autopick's first
# literal sits inside its "continue manually" branch) and the _mpi binary as
# the default for every MPI-capable job.
NON_DEFAULT_BRANCH_MARKERS = ("is_continue", "nr_mpi")


def _is_continue_only(condition: str) -> bool:
    """True if this condition can only hold when continuing an existing job.

    `is_continue && ...` qualifies; `!is_continue || ...` does not -- that is
    the ordinary first-run path, and treating it as continue-only skipped the
    real command literal.
    """
    if "is_continue" not in condition:
        return False
    return not re.search(r"!\s*is_continue", condition)


def extract_program_guess(func_body: str) -> str | None:
    # Skip the parallel half of each `if (nr_mpi > 1) ... else ...` pair: the
    # serial binary is what the job runs by default (nr_mpi defaults to 1), and
    # taking the first literal in the function reported the _mpi binary as the
    # default for every MPI-capable job.
    mpi_spans = [m.span(1) for m in MPI_BINARY_PAIR_RE.finditer(func_body)]
    for m in re.finditer(r'command\s*=\s*"([^"]*)"', func_body):
        pos = m.start(1)
        if any(lo <= pos < hi for lo, hi in mpi_spans):
            continue
        # ...and skip literals that only run when continuing an existing job
        # (Autopick's first literal is `relion_manualpick`, inside its
        # "continue manually" branch). A negated test is the default path, not
        # a continue-only one: MultiBody wraps its real command in
        # `if (!is_continue || (is_continue && fn_cont != ""))`.
        if any(_is_continue_only(c) for c in enclosing_conditions(func_body, pos)):
            continue
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


# RELION's MPI-capable jobs pick their binary with a brace-less if/else:
#
#     if (joboptions["nr_mpi"].getNumber(error_message) > 1)
#         command="`which relion_run_motioncorr_mpi`";
#     else
#         command="`which relion_run_motioncorr`";
#
# Both names are read out of that branch rather than derived by appending
# "_mpi" to the serial name: the two differ by more than a suffix for some
# jobs, and a guessed binary name is a job that fails at launch.
MPI_BINARY_PAIR_RE = re.compile(
    r'if\s*\(\s*joboptions\["nr_mpi"\]\.getNumber\([^)]*\)\s*>\s*1\s*\)\s*'
    r'command\s*=\s*"([^"]*)"\s*;\s*else\s*command\s*=\s*"([^"]*)"\s*;',
    re.DOTALL,
)


def extract_mpi_program(func_body: str) -> str | None:
    """The binary this job runs when "Number of MPI procs" > 1, or None if it
    has no MPI variant."""
    m = MPI_BINARY_PAIR_RE.search(func_body)
    return m.group(1).strip() if m else None


def extract_mpi_thread_capability(text: str) -> dict[str, dict[str, bool]]:
    """Which jobs RELION gives "Number of MPI procs" / "Number of threads".

    Read from the dispatcher in `RelionJob::initialise(int _job_type)`, where
    each branch sets has_mpi/has_thread next to the initialise<Name>Job() call:

        else if (type == PROC_CTFFIND)
        {
            has_mpi = true;
            has_thread = false;
            initialiseCtffindJob();
        }

    These two options are added by the shared tail of initialise(), not by any
    job's own initialise<Name>Job(), so a per-job scan misses them entirely --
    which is why the Running tab was absent from every job.
    """
    m = re.search(r"void RelionJob::initialise\(int\s+\w+\)\s*\{", text)
    if not m:
        return {}
    brace_open = text.index("{", m.end() - 1)
    body = text[brace_open + 1 : find_matching_brace(text, brace_open)]

    out: dict[str, dict[str, bool]] = {}
    for call in re.finditer(r"initialise(\w+)Job\(\)\s*;", body):
        # The enclosing branch block: scan back to the `{` that opens it.
        start = body.rfind("{", 0, call.start())
        block = body[start + 1 : call.start()] if start != -1 else ""
        flags = {"has_mpi": False, "has_thread": False}
        # `has_mpi = has_thread = false;` and `has_mpi = true;` both occur.
        for chain in re.finditer(
            r"((?:has_mpi|has_thread)\s*=\s*(?:has_mpi|has_thread)\s*=\s*|"
            r"(?:has_mpi|has_thread)\s*=\s*)(true|false)\s*;",
            block,
        ):
            value = chain.group(2) == "true"
            for name in re.findall(r"has_mpi|has_thread", chain.group(1)):
                flags[name] = value
        out[call.group(1)] = flags
    return out


# Ranges for nr_mpi / nr_threads. RELION reads these from the environment at
# GUI start-up (RELION_MPI_MAX, RELION_THREAD_MAX), so there is no literal in
# the source to extract; these are RELION's own compiled-in defaults, from
# pipeline_jobs.h DEFAULTMPIMAX / DEFAULTTHREADMAX / DEFAULTNRMPI /
# DEFAULTNRTHREADS. Everything else about these two fields -- label, help text,
# step -- is extracted verbatim like any other JobOption.
RUN_TAB_NUMERIC_RANGES = {
    "nr_mpi": {"default": 1, "min": 1, "max": 64, "step": 1},
    "nr_threads": {"default": 1, "min": 1, "max": 16, "step": 1},
}

# Of the shared Running tab, these are the options that actually change the
# command RELION-US runs. The queue-submission ones (do_queue, queuename, qsub,
# qsub_extra*, qsubscript, min_dedicated) drive RELION's own qsub path, which
# this app does not reproduce -- it runs the command as a subprocess -- so
# showing them would be offering controls that do nothing. See slurm/ for the
# cluster-submission path.
RUN_TAB_KEYS = ("nr_mpi", "nr_threads", "other_args")
RUN_TAB_NAME = "Running"


def extract_run_tab_options(text: str) -> dict[str, dict]:
    """The Running-tab JobOptions from the shared tail of
    `RelionJob::initialise()`, keyed by option key."""
    m = re.search(r"void RelionJob::initialise\(int\s+\w+\)\s*\{", text)
    if not m:
        return {}
    brace_open = text.index("{", m.end() - 1)
    body = text[brace_open + 1 : find_matching_brace(text, brace_open)]
    found = {}
    for opt in extract_joboptions(body):
        if opt["key"] in RUN_TAB_KEYS:
            fixed = RUN_TAB_NUMERIC_RANGES.get(opt["key"])
            if fixed:
                opt.update(fixed)
                opt["field_type"] = "slider"
            found[opt["key"]] = opt
    return found


# RELION's builders append most options as `command += " --flag " + joboptions
# ["key"]...`, and for ~200 of them the flag is NOT just "--" + key (--i for
# input_star_mics, --Box for box, --j for nr_threads, --gainref for
# fn_gain_ref). Reading the pairing straight out of the source is the whole
# point of this extractor: the alternative is guessing, and a guessed flag is
# a job that either fails or, worse, runs with a default the user thought they
# had changed.
#
# The optional `(?:\\")?` before the closing quote handles a second shape
# RELION also uses, most consistently for GPU: `command += " --gpu \"" +
# joboptions["gpu_ids"].getString() + "\"";` -- the value is wrapped in
# (escaped, since it's inside a C++ string literal) double quotes, so the
# flag's own string literal doesn't close until AFTER that escaped quote.
# Without this, the plain `\s*"` right after the flag name never matches
# (the next character is a literal backslash, not the closing quote), and
# the whole pairing is silently missed -- confirmed for real: every job
# that gates `--gpu` on "Use GPU acceleration?" (Autopick, Class2D,
# Inimodel, Class3D, Autorefine, MultiBody, Motioncorr) uses exactly this
# quoted form, so `gpu_ids` had NO entry in `option_flags` at all for any
# of them before this, and the app's GPU fields never drafted anything.
OPTION_FLAG_RE = re.compile(
    r'command\s*\+=\s*"\s*(--[A-Za-z][A-Za-z0-9_-]*)\s*(?:\\")?\s*"\s*\+\s*'
    r'joboptions\[\s*"([A-Za-z0-9_]+)"\s*\]'
)


def extract_option_flags(func_body: str) -> dict[str, dict]:
    """Per-job {option key: {flag, condition}} read from the real builder.

    `condition` is the `if (...)` test guarding the statement, or "" when the
    flag is emitted unconditionally. Callers must respect it: RELION emits
    Autopick's --particle_diameter only in Topaz mode and its --LoG_diam_min
    only in LoG mode, so emitting every extracted flag would produce a command
    with mutually contradictory options.
    """
    out: dict[str, dict] = {}
    for m in OPTION_FLAG_RE.finditer(func_body):
        flag, key = m.group(1), m.group(2)
        conds = [c.strip() for c in enclosing_conditions(func_body, m.start()) if c.strip()]
        # First occurrence wins, but an unconditional one always beats a
        # conditional one for the same key.
        condition = " && ".join(conds)
        prev = out.get(key)
        if prev is None or (prev["condition"] and not condition):
            out[key] = {"flag": flag, "condition": condition}
    return out


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
        param_order = TOMO_INPUT_PARAM_ORDER  # single source of truth (see module scope)
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
    text = strip_comments(strip_line_splices(src_path.read_text(encoding="utf-8")))
    header_text = strip_comments(strip_line_splices(header_path.read_text(encoding="utf-8")))
    gui_text = strip_comments(strip_line_splices(gui_path.read_text(encoding="utf-8")))
    option_vectors = extract_static_option_vectors(header_text)
    tab_layouts = extract_tab_layout(gui_text)
    mpi_thread = extract_mpi_thread_capability(text)
    run_tab_options = extract_run_tab_options(text)
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
        entry["program_mpi"] = extract_mpi_program(body)
        entry["option_flags"] = extract_option_flags(body)
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

    # Append RELION's shared Running tab (JobWindow::setupRunTab) to every job.
    # It is not part of any job's own initialise<Name>Job() or of the per-job
    # window layout in gui_jobwindow.cpp, so without this step these fields --
    # including "Additional arguments", which RELION appends verbatim to every
    # command -- simply do not exist in the extracted data.
    for job_name, entry in jobs.items():
        caps = mpi_thread.get(job_name, {})
        keys = []
        for key in RUN_TAB_KEYS:
            if key == "nr_mpi" and not caps.get("has_mpi"):
                continue
            if key == "nr_threads" and not caps.get("has_thread"):
                continue
            if key not in run_tab_options:
                continue
            keys.append(key)
        if not keys:
            continue
        existing = {o["key"] for o in entry.get("options", [])}
        entry.setdefault("options", []).extend(
            dict(run_tab_options[k]) for k in keys if k not in existing
        )
        layout = entry.setdefault("tab_layout", {"tab_order": [], "tab_fields": {}})
        if RUN_TAB_NAME not in layout["tab_order"]:
            layout["tab_order"].append(RUN_TAB_NAME)
        layout["tab_fields"].setdefault(RUN_TAB_NAME, []).extend(
            k for k in keys if k not in layout["tab_fields"].get(RUN_TAB_NAME, [])
        )

    out_path.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    n_options = sum(len(j.get("options", [])) for j in jobs.values())
    print(f"Extracted {len(jobs)} job types, {n_options} total JobOptions -> {out_path}")
    for name, j in jobs.items():
        print(f"  {name}: {len(j.get('options', []))} options, program={j.get('program_guess')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
