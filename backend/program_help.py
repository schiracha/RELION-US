"""
program_help.py — enumerate a program's real command-line options by running it
with `--help`, so the job popup's Advanced section can offer the options RELION's
own GUI never exposes.

Why run the binary instead of reading the source: the GUI and the program are
different surfaces. `initialise<Job>Job()` defines what the GUI shows; the
program's own `IOParser` defines what it accepts, and the second is a superset —
expert and developmental flags you would otherwise find by running the binary
with no arguments, or by reading `src/*_runner.cpp`. Those are exactly what
RELION's "Additional arguments" box is for, and what this module lists.

Asking the installed binary also means the list matches *your* RELION build,
including local patches, rather than whichever checkout the job definitions
were extracted from.

Output format (src/args.cpp, IOParser::writeUsage / writeUsageOneLine):

    +++ RELION: command line arguments (with defaults for optional ones between parantheses) +++
    ====== General options =====
                                --i : Input STAR file
                     --angpix (1.0) : Pixel size in Angstroms
                          --version : Print RELION version and exit

Each option line is the flag right-aligned in a 35-character field, then " : ",
then the usage text. A flag with a default in parentheses is optional; one
without is compulsory. Booleans go through IOParser::checkOption and so show a
`(false)` / `(true)` default.

Programs that don't use IOParser (RELION-5's Python tomo tools, which are
Typer-based) print something this parser doesn't recognise. That is reported
honestly — `parsed: false` plus the raw help text — rather than guessed at.
"""
from __future__ import annotations

import functools
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

HELP_TIMEOUT_SECONDS = 20

# `+++ RELION: command line arguments ...`, the banner writeUsage() always
# emits. Its presence is what tells us the IOParser grammar below applies.
RELION_USAGE_BANNER = "+++ RELION: command line arguments"
SECTION_RE = re.compile(r"^=+\s*(.*?)\s*=+\s*$")
# "  --flag (default) : usage"  /  "  --flag : usage"
OPTION_RE = re.compile(
    r"^\s*(--[A-Za-z0-9][A-Za-z0-9_.-]*)"      # flag
    r"(?:\s*\(([^)]*)\))?"                      # optional (default)
    r"\s*:\s?(.*)$"                             # " : " then usage
)


class ProgramHelpError(Exception):
    """The program could not be run at all (not found, not executable,
    timed out). Distinct from "ran, but its help was not parseable"."""


def program_argv(program: str) -> list[str]:
    """Turn a stored program string into an argv list.

    The extracted `program_guess` values are shell fragments as RELION writes
    them, e.g. ```which relion_refine``` or
    ``relion_python_tomo_import SerialEM``. Strip the backtick-which wrapper
    (running it would just re-resolve what shutil.which already does) and split
    the rest, so a program with a subcommand keeps it.
    """
    text = (program or "").strip()
    m = re.fullmatch(r"`\s*which\s+(.+?)\s*`", text)
    if m:
        text = m.group(1)
    try:
        return shlex.split(text)
    except ValueError:
        return [text]


def resolve_program(program: str) -> tuple[str | None, list[str]]:
    """(absolute path or None, extra argv after the binary)."""
    argv = program_argv(program)
    if not argv:
        return None, []
    return shutil.which(argv[0]), argv[1:]


def _run_help(path: str, extra_argv: list[str]) -> str:
    """Raw help text. RELION prints usage to stdout and exits 0; other programs
    may use stderr or a non-zero exit, so both streams are kept and the exit
    code is not treated as failure — a program that prints its options and
    exits 1 is still telling us what we asked."""
    try:
        proc = subprocess.run(
            [path, *extra_argv, "--help"],
            capture_output=True,
            text=True,
            timeout=HELP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise ProgramHelpError(
            f"{Path(path).name} did not respond to --help within "
            f"{HELP_TIMEOUT_SECONDS}s"
        )
    except OSError as exc:
        raise ProgramHelpError(f"could not run {path}: {exc}")
    return (proc.stdout or "") + (proc.stderr or "")


def parse_relion_help(text: str) -> list[dict[str, Any]]:
    """Parse IOParser usage output into a flat, ordered option list.

    Returns [] for output that isn't in RELION's format — the caller surfaces
    the raw text in that case instead of pretending to have parsed it.
    """
    if RELION_USAGE_BANNER not in text:
        return []
    options: list[dict[str, Any]] = []
    section = ""
    seen: set[str] = set()
    for line in text.splitlines():
        sec = SECTION_RE.match(line.strip())
        if sec and "--" not in line:
            section = sec.group(1).strip()
            continue
        m = OPTION_RE.match(line)
        if not m:
            continue
        flag, default, help_text = m.group(1), m.group(2), m.group(3).strip()
        if flag in seen:
            continue
        seen.add(flag)
        default = (default or "").strip()
        options.append({
            "flag": flag,
            "section": section,
            # A compulsory option prints no parentheses at all; RELION also
            # writes a single space as the "default" for those, so both forms
            # mean the same thing here.
            "required": default == "",
            "default": default,
            # checkOption() is how RELION declares a boolean, and it defaults
            # them to false/true. This drives whether the UI offers a value box
            # or just adds the bare flag; it is a hint, not a guarantee, so the
            # value box stays available either way.
            "takes_value": default.lower() not in ("false", "true"),
            "help": help_text,
        })
    return options


@functools.lru_cache(maxsize=64)
def _cached_help(path: str, extra: tuple[str, ...], mtime: float, size: int) -> tuple[str, tuple]:
    """Cache key includes the binary's mtime and size, so rebuilding or
    switching RELION versions invalidates it without a restart."""
    raw = _run_help(path, list(extra))
    return raw, tuple(
        tuple(sorted(o.items())) for o in parse_relion_help(raw)
    )


def program_options(program: str) -> dict[str, Any]:
    """Everything the installed binary says it accepts.

    Raises ProgramHelpError if the program isn't installed or wouldn't run —
    the caller turns that into a message rather than an empty list, because
    "no extra options" and "RELION isn't on this machine's PATH" are very
    different answers.
    """
    path, extra = resolve_program(program)
    if path is None:
        argv = program_argv(program)
        raise ProgramHelpError(
            f"{argv[0] if argv else program!r} is not on this machine's PATH. "
            "The Advanced section lists options by asking the installed program, so "
            "it needs the RELION binaries the backend would run."
        )
    try:
        st = Path(path).stat()
    except OSError as exc:
        raise ProgramHelpError(f"could not stat {path}: {exc}")

    raw, packed = _cached_help(path, tuple(extra), st.st_mtime, st.st_size)
    options = [dict(o) for o in (dict(p) for p in packed)]
    return {
        "program": program,
        "path": path,
        "raw": raw,
        "parsed": bool(options),
        "options": options,
    }


def gui_exposed_flags(job_def: dict) -> set[str]:
    """Flags this job's form already covers, so the Advanced section can show only
    what the GUI does not.

    Three sources, all extracted rather than assumed: every flag literal in the
    job's own builder (`flags_used`), the verified per-option pairings
    (`option_flags`), and the output flag the draft inserts.
    """
    flags = set(job_def.get("flags_used") or [])
    for pair in (job_def.get("option_flags") or {}).values():
        flag = pair.get("flag") if isinstance(pair, dict) else None
        if flag:
            flags.add(flag)
    # Always in the draft, never a "hidden" option worth offering again.
    flags.update({"--o", "--output-directory", "--pipeline_control",
                  "--pipeline-control", "--version", "--help", "--j"})
    return flags


def extra_options_for_job(job_def: dict, program: str) -> dict[str, Any]:
    """The Advanced section's payload: options the program accepts that the job's
    form does not already offer."""
    info = program_options(program)
    exposed = gui_exposed_flags(job_def)
    extras = [o for o in info["options"] if o["flag"] not in exposed]
    return {
        **info,
        "options": extras,
        "total_program_options": len(info["options"]),
        "hidden_by_gui": len(info["options"]) - len(extras),
    }
