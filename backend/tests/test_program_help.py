"""
Tests for program_help.py — enumerating a program's real command-line options
by running it with --help, which is what fills the job popup's Advanced
section (in the Inputs tab).

The fixtures are stub executables printing RELION's own IOParser usage format
(src/args.cpp, IOParser::writeUsage): flag right-aligned in a 35-character
field, " : ", then the usage text; a default in parentheses marks the option
optional, and booleans (declared with checkOption) show false/true.
"""
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import program_help


RELION_HELP = """\
+++ RELION: command line arguments (with defaults for optional ones between parantheses) +++
====== General options =====
                                --i : Input images (in a star-file)
                                --o : Output rootname
                     --angpix (1.0) : Pixel size in Angstroms
                            --j (1) : Number of threads
====== Expert options =====
          --dont_check_norm (false) : Skip the check whether images are normalised
                         --verb (1) : Verbosity (1=normal, 0=silent)
           --onlyflipphases (false) : Only flip phases, do not correct amplitudes
                          --version : Print RELION version and exit
"""


def _make_stub(tmp_path, name, output, exit_code=0):
    """A stub executable on a directory we prepend to PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({output!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


@pytest.fixture
def relion_stub(tmp_path, monkeypatch):
    bindir = _make_stub(tmp_path, "relion_refine", RELION_HELP)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    program_help._cached_help.cache_clear()
    return bindir


# --------------------------------------------------------------------------
# argv handling
# --------------------------------------------------------------------------


def test_backtick_which_wrapper_is_stripped():
    # program_guess values are shell fragments as RELION writes them
    assert program_help.program_argv("`which relion_refine`") == ["relion_refine"]


def test_subcommand_is_preserved():
    # RELION-5's Python tomo tools take a subcommand before their flags
    assert program_help.program_argv("relion_python_tomo_import SerialEM") == [
        "relion_python_tomo_import", "SerialEM"
    ]


def test_plain_program_name():
    assert program_help.program_argv("relion_import") == ["relion_import"]


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_parses_flags_defaults_and_help():
    opts = {o["flag"]: o for o in program_help.parse_relion_help(RELION_HELP)}
    assert set(opts) == {"--i", "--o", "--angpix", "--j", "--dont_check_norm",
                         "--verb", "--onlyflipphases", "--version"}
    assert opts["--angpix"]["default"] == "1.0"
    assert opts["--angpix"]["help"] == "Pixel size in Angstroms"


def test_option_without_parentheses_is_required():
    opts = {o["flag"]: o for o in program_help.parse_relion_help(RELION_HELP)}
    assert opts["--i"]["required"] is True
    assert opts["--angpix"]["required"] is False


def test_boolean_options_are_flagged_as_taking_no_value():
    """RELION declares booleans through checkOption, which defaults them to
    false/true — that is the only signal in the usage output, and it decides
    whether the UI offers a value box."""
    opts = {o["flag"]: o for o in program_help.parse_relion_help(RELION_HELP)}
    assert opts["--dont_check_norm"]["takes_value"] is False
    assert opts["--onlyflipphases"]["takes_value"] is False
    assert opts["--verb"]["takes_value"] is True


def test_section_headers_are_attached_to_their_options():
    opts = {o["flag"]: o for o in program_help.parse_relion_help(RELION_HELP)}
    assert opts["--angpix"]["section"] == "General options"
    assert opts["--verb"]["section"] == "Expert options"


def test_non_relion_help_returns_no_options_rather_than_garbage():
    """A Typer/argparse program (RELION-5's Python tomo tools) prints something
    else entirely. Reporting nothing parsed is honest; inventing options from
    a format we don't understand is not."""
    typer_help = textwrap.dedent("""\
        Usage: relion_python_tomo_import [OPTIONS] COMMAND [ARGS]...

        Options:
          --help  Show this message and exit.
    """)
    assert program_help.parse_relion_help(typer_help) == []


# --------------------------------------------------------------------------
# running the program
# --------------------------------------------------------------------------


def test_program_options_runs_the_binary(relion_stub):
    info = program_help.program_options("`which relion_refine`")
    assert info["parsed"] is True
    assert info["path"].endswith("relion_refine")
    assert any(o["flag"] == "--onlyflipphases" for o in info["options"])


def test_missing_program_raises_with_an_actionable_message(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(program_help.ProgramHelpError) as exc:
        program_help.program_options("`which relion_definitely_not_installed`")
    assert "relion_definitely_not_installed" in str(exc.value)
    assert "PATH" in str(exc.value)


def test_nonzero_exit_still_yields_options(tmp_path, monkeypatch):
    """Some programs print their usage and exit non-zero. That is still an
    answer to the question we asked."""
    bindir = _make_stub(tmp_path, "grumpy_prog", RELION_HELP, exit_code=1)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    program_help._cached_help.cache_clear()
    info = program_help.program_options("grumpy_prog")
    assert info["parsed"] is True


def test_help_is_cached_per_binary(relion_stub, monkeypatch):
    """One subprocess per program per backend lifetime, not one per popup."""
    calls = []
    real = program_help._run_help
    monkeypatch.setattr(program_help, "_run_help",
                        lambda p, e: (calls.append(p), real(p, e))[1])
    program_help._cached_help.cache_clear()
    program_help.program_options("`which relion_refine`")
    program_help.program_options("`which relion_refine`")
    assert len(calls) == 1


def test_rebuilt_binary_invalidates_the_cache(relion_stub):
    """The cache key is (path, mtime, size), so swapping RELION versions or
    rebuilding is picked up without restarting the backend."""
    program_help.program_options("`which relion_refine`")
    script = relion_stub / "relion_refine"
    patched = RELION_HELP.replace("--onlyflipphases", "--brand_new_flag")
    script.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        f"sys.stdout.write({patched!r})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    os.utime(script, (0, 0))     # different mtime
    info = program_help.program_options("`which relion_refine`")
    assert any(o["flag"] == "--brand_new_flag" for o in info["options"])


# --------------------------------------------------------------------------
# the GUI diff — the point of the Advanced section
# --------------------------------------------------------------------------


def test_extra_options_exclude_what_the_form_already_offers(relion_stub):
    job_def = {
        "flags_used": ["--i", "--angpix"],
        "option_flags": {"nr_threads": {"flag": "--j", "condition": ""}},
    }
    payload = program_help.extra_options_for_job(job_def, "`which relion_refine`")
    flags = [o["flag"] for o in payload["options"]]
    assert "--dont_check_norm" in flags and "--verb" in flags
    for already_shown in ("--i", "--angpix", "--j", "--o", "--version"):
        assert already_shown not in flags
    assert payload["hidden_by_gui"] == payload["total_program_options"] - len(flags)


def test_gui_exposed_flags_reads_both_extraction_sources():
    flags = program_help.gui_exposed_flags({
        "flags_used": ["--ctf"],
        "option_flags": {"box": {"flag": "--Box", "condition": ""}},
    })
    assert "--ctf" in flags and "--Box" in flags


def test_real_job_definitions_expose_flags(relion_stub):
    """Against the real extracted data, not a fixture: a job whose exposed-flag
    set came out empty would dump its whole option list into Advanced."""
    import job_registry

    raw = job_registry.raw_job("Class2D")
    exposed = program_help.gui_exposed_flags(raw)
    assert "--pad" in exposed        # emitted by Class2D's own builder
    assert "--j" in exposed          # nr_threads, from option_flags
    assert len(exposed) > 20
