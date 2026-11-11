"""
Tests for aretomo_jobs.py and its wiring into job_catalog.py/main.py.

The AreTomo2 job runs the `AreTomo` command (a link to AreTomo2) as a
subprocess, like the IsoNet2/DeepETPicker jobs. EXPECTED_FLAGS is the flag
list from AreTomo2's own CInput.cpp (`AreTomo --help`); keys below must map
onto it. test_real_binary_* runs the real thing on a tiny synthetic tilt series
when the binary and a GPU are present, and is skipped otherwise.
"""
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_catalog
import main
import project_manager
from aretomo_jobs import (
    ARETOMO_JOB_DEFINITIONS,
    build_aretomo_command,
    validate_aretomo_fields,
)

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

NAME = "AreTomo2"
DEFN = ARETOMO_JOB_DEFINITIONS[NAME]

# every tag in AreTomo2's CInput.cpp
ARETOMO_TAGS = {
    "-InMrc", "-OutMrc", "-AlnFile", "-AngFile", "-RoiFile", "-TmpFile", "-LogFile", "-AlignZ", "-VolZ",
    "-OutBin", "-TiltRange", "-TiltAxis", "-Gpu", "-TiltCor", "-ReconRange", "-PixSize", "-ImgDose", "-Kv",
    "-Cs", "-AmpContrast", "-ExtPhase", "-FlipVol", "-FlipInt", "-Sart", "-Wbp", "-Patch", "-TiltScheme",
    "-OutXF", "-Align", "-CropVol", "-OutImod", "-DarkTol", "-Bft", "-IntpCor",
}
# deliberately not exposed: debugging/obsolete/disabled in AreTomo2 itself
NOT_EXPOSED = {"-TmpFile", "-LogFile", "-OutXF", "-TiltScheme"}


def tokens(cmd: str) -> list:
    return shlex.split(cmd)


def flag_values(cmd: str) -> dict:
    """{"-VolZ": ["1200"], ...} for a single-stack command (up to the `&&` output check)."""
    t = tokens(cmd)
    t = t[:t.index("&&")]
    out, cur = {}, None
    for tok in t[1:]:
        if tok in ARETOMO_TAGS:
            cur = out.setdefault(tok, [])
        elif cur is not None:
            cur.append(tok)
    return out


def single(**fields):
    return build_aretomo_command(NAME, {"tilt_series": "ts/TS_01.mrc", **fields}, "AreTomo2/job001")


# ---- catalog -----------------------------------------------------------
def test_job_is_in_the_catalog_under_tomogram_reconstruction():
    meta = job_catalog.CUSTOM_JOBS[NAME]
    assert meta["label_new"] == "custom.aretomo2" and NAME in job_catalog.JOB_DIRNAME
    assert meta["category"] == "Tilt Series / Tomogram Reconstruction" and meta["category"] in job_catalog.CATEGORIES
    assert job_catalog.pipeline_type(NAME) == "tomo"
    assert job_catalog.job_dirname(NAME) == "AreTomo2"
    assert job_catalog.internal_name_for_label("custom.aretomo2") == NAME
    assert job_catalog.internal_name_for_label("custom.aretomo_import") == "AreTomoImport"   # untouched


def test_listed_with_the_other_jobs():
    import job_registry
    row = next(j for j in job_registry.list_catalog() if j["internal_name"] == NAME)
    assert row["display_name"] == DEFN["display_name"] and row["pipeline_type"] == "tomo"


def test_not_an_in_process_custom_job():
    import custom_jobs
    assert NAME not in custom_jobs.CUSTOM_JOB_DEFINITIONS
    assert "AreTomoImport" in custom_jobs.CUSTOM_JOB_DEFINITIONS
    assert NAME in main.EXTERNAL_COMMAND_JOBS


def test_every_option_is_placed_exactly_once():
    keys = [o["key"] for o in DEFN["options"]]
    placed = [k for g in DEFN["standard_groups"] for k in g["fields"]]
    assert sorted(placed) == sorted(keys) and len(set(keys)) == len(keys)


def test_radio_defaults_are_among_their_options():
    for o in DEFN["options"]:
        if o["field_type"] == "radio":
            assert o["default"] in o["options"], o["key"]


def test_every_aretomo_flag_is_reachable_or_deliberately_not():
    """Set every field to something non-default and require each real flag to appear."""
    every = {
        "tilt_series": "a.mrc", "angle_source": "Tilt range", "tilt_min": "-50", "tilt_max": "50",
        "aln_file": "a.aln", "align": True, "align_z": 400, "tilt_axis": "85", "tilt_axis_refine": "-1: x",
        "tilt_cor": "1: x", "tilt_offset": "5", "patch_x": 5, "patch_y": 4, "roi_file": "r.txt", "crop_x": 100,
        "crop_y": 90, "dark_tol": 0.8, "bft_global": "400", "bft_local": "300", "intp_cor": False,
        "vol_z": 800, "out_bin": 2, "recon_method": "SART", "sart_iter": 15, "sart_proj": 4, "recon_min": "-60",
        "recon_max": "60", "flip_vol": True, "flip_int": True, "pix_size": "2.5", "kv": "300", "img_dose": "3",
        "cs": "2.7", "amp_contrast": "0.1", "ext_phase_guess": "90", "ext_phase_range": "20", "out_imod": "2: w",
        "gpu": "0 1",
    }
    # some flags exclude each other (an .aln file implies no -Align; WBP replaces -Sart; one angle
    # source), so the union over a few variants has to cover the whole list
    variants = [every,
                {**every, "aln_file": "", "align": False},
                {**every, "recon_method": "WBP", "angle_source": "Angle file", "ang_file": "t.tlt"}]
    seen = {"-InMrc", "-OutMrc"}
    for fields in variants:
        seen |= set(flag_values(build_aretomo_command(NAME, fields, "AreTomo2/job001")))
    assert ARETOMO_TAGS - NOT_EXPOSED - seen == set(), ARETOMO_TAGS - NOT_EXPOSED - seen


# ---- single-stack command ---------------------------------------------
def test_defaults_give_the_core_command_only():
    cmd = single()
    assert cmd == ("AreTomo -InMrc ts/TS_01.mrc -OutMrc AreTomo2/job001/TS_01.mrc -VolZ 1200 -OutBin 4 "
                   "-AlignZ 600 -Gpu 0 -OutImod 1 && [ -s AreTomo2/job001/TS_01.mrc ] || "
                   "{ echo \"AreTomo2 failed or wrote no output: AreTomo2/job001/TS_01.mrc\" >&2; exit 1; }")


def test_no_input_gives_a_placeholder_not_an_empty_argument():
    cmd = build_aretomo_command(NAME, {}, "AreTomo2/job001")
    assert "-InMrc TILT_SERIES.mrc" in cmd and "-InMrc -" not in cmd


def test_output_is_named_after_the_stack_and_lands_in_the_job_directory():
    for stack, stem in (("a/b/TS_01.mrc", "TS_01"), ("x/TS_01.mrc.st", "TS_01.mrc"), ("TS 01.st", "TS 01"),
                        ("noext", "noext")):
        cmd = build_aretomo_command(NAME, {"tilt_series": stack}, "AreTomo2/job007/")
        t = tokens(cmd)
        assert t[t.index("-InMrc") + 1] == stack
        assert t[t.index("-OutMrc") + 1] == f"AreTomo2/job007/{stem}.mrc"


def test_angle_sources():
    assert "-TiltRange" not in single() and "-AngFile" not in single()
    assert flag_values(single(angle_source="Tilt range"))["-TiltRange"] == ["-60", "60"]
    assert flag_values(single(angle_source="Tilt range", tilt_min="-45", tilt_max="63"))["-TiltRange"] == ["-45", "63"]
    f = flag_values(single(angle_source="Angle file", ang_file="ts/TS 01.rawtlt"))
    assert f["-AngFile"] == ["ts/TS 01.rawtlt"] and "-TiltRange" not in f
    assert "-AngFile" not in single(angle_source="MRC extended header", ang_file="ignored.tlt")


def test_values_that_differ_from_aretomos_defaults_are_passed_and_the_rest_left_off():
    f = flag_values(single(
        vol_z=900, out_bin=2, align_z=350, gpu="0,1", out_imod="3: whatever", tilt_axis="85.5",
        tilt_axis_refine="0: refine", tilt_cor="1: both", tilt_offset="-4.5", patch_x=5, patch_y=4,
        dark_tol=0.5, bft_global="400", bft_local="250", intp_cor=False, recon_min="-60", recon_max="55",
        flip_vol=True, flip_int=True, pix_size="2.7", kv="300", cs="2.7", img_dose="3.5", amp_contrast="0.1",
        ext_phase_guess="30", ext_phase_range="10"))
    assert f == {
        "-VolZ": ["900"], "-OutBin": ["2"], "-AlignZ": ["350"], "-Gpu": ["0", "1"], "-OutImod": ["3"],
        "-TiltAxis": ["85.5", "0"], "-TiltCor": ["1", "-4.5"], "-Patch": ["5", "4"], "-DarkTol": ["0.5"],
        "-Bft": ["400", "250"], "-IntpCor": ["0"], "-ReconRange": ["-60", "55"], "-FlipVol": ["1"],
        "-FlipInt": ["1"], "-PixSize": ["2.7"], "-Kv": ["300"], "-Cs": ["2.7"], "-ImgDose": ["3.5"],
        "-AmpContrast": ["0.1"], "-ExtPhase": ["30", "10"], "-InMrc": ["ts/TS_01.mrc"],
        "-OutMrc": ["AreTomo2/job001/TS_01.mrc"],
    }
    d = flag_values(single(dark_tol="0.70", bft_global="500.0", amp_contrast="0.070", sart_iter=20.0,
                           recon_min="-90.0", patch_x="0", patch_y="0", tilt_axis="0.0"))
    assert not {"-DarkTol", "-Bft", "-AmpContrast", "-Sart", "-ReconRange", "-Patch", "-TiltAxis"} & set(d)


def test_tilt_axis_and_tilt_correction_forms():
    assert "-TiltAxis" not in single()                                  # 0, refine = AreTomo2's default 0 1
    assert flag_values(single(tilt_axis="0", tilt_axis_refine="-1: x"))["-TiltAxis"] == ["0", "-1"]
    assert flag_values(single(tilt_axis="12"))["-TiltAxis"] == ["12", "1"]
    assert "-TiltCor" not in single(tilt_cor="0: measure; correct for alignment only")
    assert flag_values(single(tilt_cor="-1: none"))["-TiltCor"] == ["-1"]
    assert flag_values(single(tilt_offset="10"))["-TiltCor"] == ["0", "10"]       # offset given: measurement off


def test_reconstruction_method():
    assert "-Wbp" not in single() and "-Sart" not in single()
    assert flag_values(single(recon_method="WBP"))["-Wbp"] == ["1"]
    f = flag_values(single(sart_iter=15, sart_proj=3))
    assert f["-Sart"] == ["15", "3"] and "-Wbp" not in f
    assert "-Sart" not in flag_values(single(recon_method="WBP", sart_iter=15))


def test_alignment_switches():
    assert flag_values(single(align=False))["-Align"] == ["0"]
    f = flag_values(single(aln_file="prev/TS_01.aln"))
    assert f["-AlnFile"] == ["prev/TS_01.aln"] and "-Align" not in f       # -AlnFile implies it
    f = flag_values(single(roi_file="roi.txt", crop_x=500, crop_y=400))
    assert f["-RoiFile"] == ["roi.txt"] and f["-CropVol"] == ["500", "400"]
    assert "-CropVol" not in flag_values(single(crop_x=500))                # no ROI file: cropping does nothing


def test_aretomo_executable_and_odd_paths_are_quoted():
    assert tokens(single(aretomo_exe="/opt/AreTomo 2/AreTomo2"))[0] == "/opt/AreTomo 2/AreTomo2"
    t = tokens(build_aretomo_command(NAME, {"tilt_series": "my data/TS 01; rm -rf x.mrc"}, "AreTomo2/job001"))
    assert t[t.index("-InMrc") + 1] == "my data/TS 01; rm -rf x.mrc" and "rm" not in t


# ---- folder of stacks --------------------------------------------------
def test_folder_command_is_a_visible_loop_that_survives_one_bad_stack():
    cmd = build_aretomo_command(NAME, {"tilt_series_dir": "tilt series", "input_ext": "st",
                                       "angle_source": "Angle file", "ang_ext": "tlt"}, "AreTomo2/job002")
    assert cmd.startswith("rc=0; for f in 'tilt series'/*.st; do ") and cmd.endswith("done; exit $rc")
    assert '-InMrc "$f" -OutMrc "$o" -AngFile "${f%.*}.tlt"' in cmd
    assert 'o=AreTomo2/job002/"$b".mrc' in cmd and '[ -s "$o" ] ||' in cmd and "rc=1" in cmd
    assert "-VolZ 1200 -OutBin 4 -AlignZ 600 -Gpu 0 -OutImod 1" in cmd
    assert "-AlnFile" not in cmd and "-RoiFile" not in cmd


def test_folder_command_has_no_bashisms_and_parses_in_sh():
    cmd = build_aretomo_command(NAME, {"tilt_series_dir": "d", "angle_source": "Tilt range"}, "AreTomo2/job002")
    assert "[[" not in cmd and "<(" not in cmd and " == " not in cmd
    assert subprocess.run(["/bin/sh", "-n", "-c", cmd]).returncode == 0
    assert subprocess.run(["/bin/sh", "-n", "-c", single()]).returncode == 0


def test_folder_loop_reports_failure_even_when_aretomo_exits_zero(tmp_path):
    """AreTomo2 prints "Error: tilt angles cannot be found." and exits 0; the loop must still fail."""
    fake = tmp_path / "bin"
    fake.mkdir()
    exe = fake / "AreTomo"
    exe.write_text('#!/bin/sh\n'
                   'while [ $# -gt 0 ]; do [ "$1" = -OutMrc ] && out=$2; [ "$1" = -InMrc ] && in=$2; shift; done\n'
                   'case "$in" in *bad*) echo "Error: tilt angles cannot be found."; exit 0;; esac\n'
                   'echo data > "$out"\n')
    exe.chmod(0o755)
    (tmp_path / "ts").mkdir()
    for n in ("good1", "bad", "good2"):
        (tmp_path / "ts" / f"{n}.mrc").write_text("x")
    (tmp_path / "AreTomo2" / "job001").mkdir(parents=True)
    cmd = build_aretomo_command(NAME, {"tilt_series_dir": "ts", "aretomo_exe": str(exe)}, "AreTomo2/job001")
    r = subprocess.run(["/bin/sh", "-c", cmd], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 1 and "wrote no output for ts/bad.mrc" in r.stderr
    done = sorted(p.name for p in (tmp_path / "AreTomo2" / "job001").iterdir())
    assert done == ["good1.mrc", "good2.mrc"]                       # the loop carried on past the bad one


def test_single_command_reports_failure_even_when_aretomo_exits_zero(tmp_path):
    exe = tmp_path / "AreTomo"
    exe.write_text('#!/bin/sh\necho "Error: unable to open MRC file"\nexit 0\n')
    exe.chmod(0o755)
    (tmp_path / "AreTomo2" / "job001").mkdir(parents=True)
    cmd = build_aretomo_command(NAME, {"tilt_series": "x.mrc", "aretomo_exe": str(exe)}, "AreTomo2/job001")
    r = subprocess.run(["/bin/sh", "-c", cmd], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 1 and "wrote no output" in r.stderr
    ok = tmp_path / "ok"
    ok.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -OutMrc ] && echo y > "$2"; shift; done\n')
    ok.chmod(0o755)
    cmd = build_aretomo_command(NAME, {"tilt_series": "x.mrc", "aretomo_exe": str(ok)}, "AreTomo2/job001")
    assert subprocess.run(["/bin/sh", "-c", cmd], cwd=tmp_path).returncode == 0


# ---- validation --------------------------------------------------------
@pytest.mark.parametrize("values,fragment", [
    ({}, "exactly one"),
    ({"tilt_series": "a.mrc", "tilt_series_dir": "d"}, "exactly one"),
    ({"tilt_series": "a.mrc", "angle_source": "Angle file"}, "no angle file"),
    ({"tilt_series_dir": "d", "angle_source": "Angle file", "ang_ext": ""}, "extension"),
    ({"tilt_series_dir": "d", "angle_source": "Angle file", "ang_ext": "r/../x"}, "extension"),
    ({"tilt_series": "a.mrc", "angle_source": "Tilt range", "tilt_min": "60", "tilt_max": "-60"}, "minimum"),
    ({"tilt_series": "a.mrc", "angle_source": "Tilt range", "tilt_min": "", "tilt_max": "60"}, "tilt range"),
    ({"tilt_series_dir": "d", "aln_file": "a.aln"}, "single stack"),
    ({"tilt_series_dir": "d", "roi_file": "r.txt"}, "single stack"),
    ({"tilt_series": "a.mrc", "crop_x": 100}, "region-of-interest"),
    ({"tilt_series": "a.mrc", "align": False, "aln_file": "a.aln"}, "pick one"),
    ({"tilt_series": "a.mrc", "vol_z": "abc"}, "must be a number"),
    ({"tilt_series": "a.mrc", "pix_size": "-2"}, "negative"),
    ({"tilt_series": "a.mrc", "out_bin": "0"}, "at least 1"),
    ({"tilt_series": "a.mrc", "vol_z": 300, "align_z": 600}, "must not exceed"),
    ({"tilt_series": "a.mrc", "dark_tol": 1.5}, "between 0 and 1"),
    ({"tilt_series": "a.mrc", "recon_min": "10", "recon_max": "-10"}, "minimum"),
    ({"tilt_series": "a.mrc", "img_dose": "3", "angle_source": "Angle file", "ang_file": "t.txt"}, "pixel size and voltage"),
    ({"tilt_series": "a.mrc", "img_dose": "3", "pix_size": "2", "kv": "300"}, "angle file"),
    ({"tilt_series": "a.mrc", "cs": "2.7"}, "CTF estimation"),
    ({"tilt_series": "a.mrc", "gpu": "zero"}, "GPU"),
])
def test_validation_rejects(values, fragment):
    assert any(fragment in p for p in validate_aretomo_fields(NAME, values)), \
        (values, validate_aretomo_fields(NAME, values))


def test_validation_accepts_good_input():
    assert validate_aretomo_fields(NAME, {"tilt_series": "a.mrc"}) == []
    assert validate_aretomo_fields(NAME, {"tilt_series_dir": "d", "angle_source": "Angle file"}) == []
    assert validate_aretomo_fields(NAME, {"tilt_series": "a.mrc", "angle_source": "Tilt range", "vol_z": 0,
                                          "align_z": 600, "gpu": "0, 1"}) == []
    assert validate_aretomo_fields(NAME, {
        "tilt_series": "a.mrc", "angle_source": "Angle file", "ang_file": "t.txt", "img_dose": "3",
        "pix_size": "2.7", "kv": "300", "cs": "2.7", "roi_file": "r.txt", "crop_x": 10}) == []


# ---- endpoints ---------------------------------------------------------
def test_job_definition_endpoint():
    d = main.get_job_definition(NAME)
    assert d["internal_name"] == NAME and d["output_subdir"].startswith("AreTomo2/job")
    assert d["draft_command"].startswith("AreTomo -InMrc TILT_SERIES.mrc")
    assert d["default_values"]["out_bin"] == 4 and d["standard_groups"]


def test_cli_options_and_draft_endpoints():
    assert main.job_cli_options(NAME)["reason"] == "aretomo_job"
    r = main.recompute_draft(NAME, main.DraftRequest(
        field_values={"tilt_series": "ts/a.mrc"}, output_subdir="AreTomo2/job009"))
    assert r["output_subdir"] == "AreTomo2/job009" and "-OutMrc AreTomo2/job009/a.mrc" in r["draft_command"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    project_dir = tmp_path / "project"
    project_manager.init_new_project(project_dir)
    main.run_manager.set_project_dir(project_dir)
    return TestClient(main.app)


def _wait(client, run_id, statuses, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = client.get(f"/api/runs/{run_id}").json()
        if last.get("status") in statuses:
            return last
        time.sleep(0.02)
    raise AssertionError(f"never reached {statuses}: {last}")


def test_run_validates_the_drafted_command_then_runs_it(client):
    sub = "AreTomo2/job001"
    for fields in ({}, {"tilt_series": "a.mrc", "angle_source": "Angle file"}):
        draft = main.build_external_command(NAME, fields, sub)
        resp = client.post("/api/runs", json={"internal_name": NAME, "command": draft,
                                              "field_values": fields, "subdir": sub})
        assert resp.status_code == 400, fields
    assert client.get("/api/runs").json() == []                       # nothing was started
    good = client.post("/api/runs", json={"internal_name": NAME,
                                          "field_values": {"tilt_series": "a.mrc", "aretomo_exe": "false"}})
    assert good.status_code == 200 and good.json()["command"].startswith("false -InMrc a.mrc")
    assert _wait(client, good.json()["run_id"], {"completed", "failed"})["status"] == "failed"


def test_hand_edited_command_skips_field_validation(client):
    resp = client.post("/api/runs", json={"internal_name": NAME, "command": "true", "field_values": {}})
    assert resp.status_code == 200
    assert _wait(client, resp.json()["run_id"], {"completed", "failed"})["status"] == "completed"


def test_a_failed_stack_fails_the_job_through_the_real_runner(client, tmp_path):
    """The whole path: draft command -> runner -> /bin/sh -> status, with a stand-in AreTomo that exits 0."""
    exe = tmp_path / "fake_aretomo"
    exe.write_text('#!/bin/sh\necho "Error: tilt angles cannot be found."\nexit 0\n')
    exe.chmod(0o755)
    stacks = Path(main.run_manager.project_dir) / "stacks"
    stacks.mkdir()
    (stacks / "TS_01.mrc").write_text("x")
    resp = client.post("/api/runs", json={"internal_name": NAME, "field_values": {
        "tilt_series_dir": "stacks", "aretomo_exe": str(exe), "angle_source": "Tilt range"}})
    assert resp.status_code == 200
    assert _wait(client, resp.json()["run_id"], {"completed", "failed"})["status"] == "failed"


# ---- the real binary ---------------------------------------------------
def _real_aretomo():
    exe = shutil.which("AreTomo") or shutil.which("AreTomo2")
    if not exe:
        return None
    try:
        gpus = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return exe if "GPU" in gpus else None


@pytest.mark.skipif(_real_aretomo() is None, reason="AreTomo2 and a GPU are required")
def test_real_binary_accepts_the_generated_flags_on_a_synthetic_series(tmp_path):
    np = pytest.importorskip("numpy")
    mrcfile = pytest.importorskip("mrcfile")
    rng = np.random.default_rng(0)
    n = 256
    yy, xx = np.mgrid[0:n, 0:n]
    pts = [(rng.uniform(50, 200), rng.uniform(50, 200), rng.uniform(-50, 50)) for _ in range(40)]
    tilts = np.arange(-60, 61, 6.0)
    frames = []
    for t in tilts:
        img = rng.normal(0, 0.3, (n, n)).astype(np.float32)
        for x, y, z in pts:
            xp = (x - n / 2) * np.cos(np.radians(t)) + z * np.sin(np.radians(t)) + n / 2
            img -= 3 * np.exp(-((xx - xp) ** 2 + (yy - y) ** 2) / (2 * 4 ** 2))
        frames.append(img)
    (tmp_path / "ts").mkdir()
    with mrcfile.new(tmp_path / "ts" / "TS_01.mrc", overwrite=True) as m:
        m.set_data(np.array(frames, dtype=np.float32))
        m.voxel_size = 4.0
    (tmp_path / "AreTomo2" / "job001").mkdir(parents=True)
    cmd = build_aretomo_command(NAME, {"tilt_series_dir": "ts", "angle_source": "Tilt range", "vol_z": 100,
                                       "align_z": 80, "out_bin": 2}, "AreTomo2/job001")
    r = subprocess.run(["/bin/sh", "-c", cmd], cwd=tmp_path, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    out = tmp_path / "AreTomo2" / "job001"
    assert (out / "TS_01.mrc").stat().st_size > 0 and (out / "TS_01.aln").exists()
    assert (out / "TS_01_Imod" / "TS_01_st.xf").exists()
