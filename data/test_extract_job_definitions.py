"""Tests for extract_job_definitions.py's resolve_default_location_options
(found via a real RELION-US Ctffind job failing against RELION 5.0.1: the
job's fn_ctffind_exe field showed the literal text "default_location" as its
default, since that's a C++ local variable name, not a literal default
string -- relion_run_ctffind_mpi then tried to exec a file literally named
"default_location" and failed immediately)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_job_definitions import parse_default_location_macros, resolve_default_location_options


HEADER_TEXT = '''
#define DEFAULTCTFFINDLOCATION "/public/EM/ctffind/ctffind.exe"
#define DEFAULTMOTIONCOR2LOCATION "/public/EM/MOTIONCOR2/MotionCor2"
#define DEFAULTARETOMOLOCATION "/public/EM/AreTomo/AreTomo2/AreTomo2"
#define DEFAULTBATCHTOMOLOCATION "/public/EM/imod/IMOD/bin/batchruntomo"
'''


def test_parse_default_location_macros_reads_the_define_table():
    macros = parse_default_location_macros(HEADER_TEXT)
    assert macros["DEFAULTCTFFINDLOCATION"] == "/public/EM/ctffind/ctffind.exe"
    assert macros["DEFAULTMOTIONCOR2LOCATION"] == "/public/EM/MOTIONCOR2/MotionCor2"


def test_resolves_a_single_default_location_field():
    macros = parse_default_location_macros(HEADER_TEXT)
    func_body = '''
    char *default_location = getenv ("RELION_CTFFIND_EXECUTABLE");
    if (default_location == NULL) {
        char default_ctffind[] = DEFAULTCTFFINDLOCATION;
        default_location = default_ctffind;
    }
    joboptions["fn_ctffind_exe"] = JobOption("CTFFIND-4.1 executable:", std::string(default_location), "*", ".", "help text");
    '''
    options = [{"key": "fn_ctffind_exe", "default": "default_location", "field_type": "filename"}]
    resolve_default_location_options(options, func_body, macros)
    assert options[0]["default"] == "/public/EM/ctffind/ctffind.exe"


def test_two_default_location_fields_in_one_function_each_get_their_own_getenv():
    # TomoAlignTiltSeries declares fn_batchtomo_exe AND fn_aretomo_exe in the
    # same initialise...Job() function, each preceded by its own getenv()
    # reassigning the SAME "default_location" variable name. Pairing every
    # such field with just "the first getenv in the function" (what an
    # earlier version of this fix did) gave fn_aretomo_exe batchruntomo's
    # own path -- confirmed wrong by running the real extractor and
    # comparing against pipeline_jobs.h's #define table by hand.
    macros = parse_default_location_macros(HEADER_TEXT)
    func_body = '''
    char *default_location = getenv ("RELION_BATCHTOMO_EXECUTABLE");
    if (default_location == NULL) default_location = DEFAULTBATCHTOMOLOCATION;
    joboptions["fn_batchtomo_exe"] = JobOption("Batchruntomo executable:", std::string(default_location), "*", ".", "help");

    default_location = getenv ("RELION_ARETOMO_EXECUTABLE");
    if (default_location == NULL) default_location = DEFAULTARETOMOLOCATION;
    joboptions["fn_aretomo_exe"] = JobOption("AreTomo2 executable:", std::string(default_location), "*", ".", "help");
    '''
    options = [
        {"key": "fn_batchtomo_exe", "default": "default_location", "field_type": "filename"},
        {"key": "fn_aretomo_exe", "default": "default_location", "field_type": "filename"},
    ]
    resolve_default_location_options(options, func_body, macros)
    assert options[0]["default"] == "/public/EM/imod/IMOD/bin/batchruntomo"
    assert options[1]["default"] == "/public/EM/AreTomo/AreTomo2/AreTomo2"


def test_resolves_a_directly_assigned_variable_not_just_the_exe_indirection_shape():
    # Confirmed for real: a Class2D job's scratch_dir field showed the
    # literal text "default_scratch" -- would have made relion_refine try
    # to copy every particle into a "default_scratch/relion_volatile/"
    # directory relative to cwd, instead of correctly defaulting to ""
    # (DEFAULTSCRATCHDIR's real value -- no scratch copy at all). Unlike
    # the exe-path cases, this variable is assigned DIRECTLY from the
    # macro (`default_scratch = DEFAULTSCRATCHDIR;`), no intermediate
    # array variable -- proving the fix isn't special-cased to the
    # exe-indirection shape.
    header = HEADER_TEXT + '\n#define DEFAULTSCRATCHDIR ""\n'
    macros = parse_default_location_macros(header)
    func_body = '''
    const char *default_scratch = getenv("RELION_SCRATCH_DIR");
    if (default_scratch == NULL)
    {
        default_scratch = DEFAULTSCRATCHDIR;
    }
    joboptions["scratch_dir"] = JobOption("Copy particles to scratch directory:", std::string(default_scratch), "help");
    '''
    options = [{"key": "scratch_dir", "default": "default_scratch", "field_type": "text"}]
    resolve_default_location_options(options, func_body, macros)
    assert options[0]["default"] == ""


def test_leaves_non_default_location_fields_alone():
    macros = parse_default_location_macros(HEADER_TEXT)
    options = [{"key": "angpix", "default": 1.4, "field_type": "text"}]
    resolve_default_location_options(list(options), "no getenv here", macros)
    assert options[0]["default"] == 1.4


def test_missing_getenv_leaves_the_literal_identifier_untouched():
    """If a function's getenv() call doesn't match the expected
    RELION_XXX_EXECUTABLE shape, this must not guess -- leaving the
    obviously-wrong literal in place is safer than silently attaching an
    unrelated macro's value."""
    macros = parse_default_location_macros(HEADER_TEXT)
    options = [{"key": "fn_something_exe", "default": "default_location", "field_type": "filename"}]
    resolve_default_location_options(options, "no getenv call in this body at all", macros)
    assert options[0]["default"] == "default_location"
