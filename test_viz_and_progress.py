"""
Playwright suite for the recent-projects list, the orthogonal (three-panel)
tomogram viewer, the dark/light theme switch, the job Progress tab (live
charts + class thumbnails), the CTF QC tab (end-of-job charts + power-
spectrum thumbnails), and the visualizer's Browse file pickers.

One backend + one browser session for the whole file. Order matters: the
orthogonal-viewer section writes and checks against its own MRC/picks
fixture at Tomograms/job099, then explicitly closes the visualizer popup
before the Browse-pickers section later overwrites that same path with a
different fixture (different dims/pick count) and reopens a fresh
visualizer -- openVisualizer() always creates a new WinBox rather than
reusing one, so a stale popup left open would leave two `.viz-winbox`
elements and make `.first` resolve to the wrong (stale) one.

Needs a live backend on an EMPTY project.

Usage: python3 test_viz_and_progress.py [base_url] [project_dir]
"""
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def launch_browser(p):
    """Launch Chromium for the smoke tests.

    By default let Playwright find its own bundled browser -- that is what
    `playwright install chromium` sets up and it is right on almost every
    machine. Set RELION_US_CHROMIUM to point at a specific binary when the
    browser lives somewhere Playwright does not look (a shared read-only
    install on a cluster, for instance)."""
    exe = os.environ.get("RELION_US_CHROMIUM")
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8429"
PROJECT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("relion_project")

errors = []
ok = True


def check(label, cond):
    global ok
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        ok = False


def make_viewer_fixtures():
    import numpy as np
    import mrcfile
    import starfile
    import pandas as pd

    d = PROJECT_DIR / "Tomograms" / "job099"
    d.mkdir(parents=True, exist_ok=True)
    # Deliberately anisotropic (nx != ny != nz) so a panel that used the wrong
    # dimension shows up as a wrong aspect ratio rather than passing by luck.
    nx, ny, nz = 64, 48, 24
    vol = (np.random.rand(nz, ny, nx) * 50).astype(np.float32)
    vol[10:14, 20:26, 30:38] += 400          # a blob to see
    with mrcfile.new(d / "TS_77.mrc", overwrite=True) as m:
        m.set_data(vol)
        m.voxel_size = 10.0
    starfile.write(
        {"particles": pd.DataFrame({
            "rlnTomoName": ["TS_77"] * 3,
            "rlnCoordinateX": [32.0, 12.0, 50.0],
            "rlnCoordinateY": [24.0, 8.0, 40.0],
            "rlnCoordinateZ": [12.0, 4.0, 20.0]})},
        d / "picks99.star", overwrite=True)
    return nx, ny, nz


FAKE_JOB = '''
import sys, time, numpy as np, mrcfile, starfile, pandas as pd
from pathlib import Path
# argv[1] is an OUTPUT PREFIX in RELION's own --o convention (e.g.
# "Class2D/job001/run"), not a directory to create -- "run" is a filename
# prefix, and the iteration files (run_it001_model.star, ...) live directly
# in the job's own directory (prefix.parent), never in a nested "run/"
# subdirectory. progress.py's _iteration_files() does a non-recursive
# job_dir.iterdir(), so getting this wrong means it silently finds nothing,
# forever -- not a timing issue, a permanently empty Progress tab.
prefix = Path(sys.argv[1])
prefix.parent.mkdir(parents=True, exist_ok=True)
NC = 4
for it in range(1, 13):
    stack = np.random.rand(NC, 48, 48).astype(np.float32) * 0.3
    yy, xx = np.mgrid[0:48, 0:48]
    for k in range(NC):
        stack[k] += np.exp(-(((xx-24)**2 + (yy-24)**2) / (40.0 + 30*k)))
    with mrcfile.new(f"{prefix}_it{it:03d}_classes.mrcs", overwrite=True) as m:
        m.set_data(stack)
    dist = np.array([0.4, 0.3, 0.2, 0.1])
    starfile.write({
      "model_general": pd.DataFrame({"rlnCurrentResolution":[1/(20.0-it*1.5)],
          "rlnNrClasses":[NC], "rlnReferenceDimensionality":[2], "rlnPixelSize":[1.4]}),
      "model_classes": pd.DataFrame({
          "rlnReferenceImage":[f"{k+1:06d}@{prefix.name}_it{it:03d}_classes.mrcs" for k in range(NC)],
          "rlnClassDistribution": dist,
          "rlnEstimatedResolution":[22.0-it + k for k in range(NC)]})},
      f"{prefix}_it{it:03d}_model.star", overwrite=True)
    print(f"Iteration {it}/12", flush=True)
    time.sleep(1.5)
print("done")
'''

# CTF Estimation writes its joint results ONCE, at the very end (see
# backend/ctf_qc.py's module docstring) -- unlike FAKE_JOB above, no
# per-iteration files, no sleep loop. RELION's own --o convention for this
# job is a bare directory (no filename prefix, confirmed against the real
# getCommandsCtffindJob source: `command += " --o " + outputname` where
# outputname already ends in "/") -- so argv[1] here IS the job directory
# to mkdir into directly, unlike FAKE_JOB's prefix handling.
FAKE_CTFFIND_JOB = '''
import sys
from pathlib import Path
import numpy as np, mrcfile, starfile, pandas as pd
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
n = 5
rng = np.random.default_rng(1)
names = [f"mic_{i}.mrc" for i in range(n)]
for name in names:
    with mrcfile.new(out / f"{name}.ctf", overwrite=True) as m:
        m.set_data((rng.random((32, 32)) * 0.3).astype(np.float32))
starfile.write({
    "micrographs": pd.DataFrame({
        "rlnMicrographName": names,
        "rlnCtfImage": [f"{name}.ctf:mrc" for name in names],
        "rlnDefocusU": rng.normal(15000, 2000, n),
        "rlnDefocusV": rng.normal(14000, 2000, n),
        "rlnCtfAstigmatism": rng.uniform(0, 500, n),
        "rlnDefocusAngle": rng.uniform(0, 180, n),
        "rlnCtfFigureOfMerit": rng.uniform(0.02, 0.15, n),
        "rlnCtfMaxResolution": rng.uniform(3.0, 8.0, n),
    })
}, out / "micrographs_ctf.star", overwrite=True)
print("done")
'''


def main():
    nx, ny, nz = make_viewer_fixtures()
    (PROJECT_DIR / "fake_iterative_job.py").write_text(FAKE_JOB)
    (PROJECT_DIR / "fake_ctffind_job.py").write_text(FAKE_CTFFIND_JOB)

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)

        page.goto(BASE_URL + "/", wait_until="networkidle")
        page.wait_for_selector(".job-item", timeout=5000)

        # ==== recent projects ====================================================
        page.locator("#changeProjectBtn").click()
        page.wait_for_selector("#projectModalOverlay:not(.hidden)", timeout=5000)
        page.wait_for_timeout(500)
        check("Recent projects section is shown",
              page.locator("#recentProjectsWrap").is_visible())
        entries = page.locator(".recent-entry")
        check("Current project appears in the recent list", entries.count() >= 1)
        first_path = entries.first.locator(".recent-entry-path").inner_text()
        check(f"Recent entry shows a full path ({first_path})", first_path.startswith("/"))
        check("Each entry has a forget button",
              entries.first.locator(".recent-forget").count() == 1)

        # clicking browses to it (fills the path box) rather than switching outright
        page.locator("#projectPathInput").fill("")
        entries.first.click()
        page.wait_for_timeout(600)
        check("Clicking a recent project browses to it",
              page.locator("#projectPathInput").input_value() == first_path)

        # forgetting removes it from the list but not from disk
        before = entries.count()
        entries.first.locator(".recent-forget").click()
        page.wait_for_timeout(600)
        after = page.locator(".recent-entry").count()
        check(f"Forget removes the entry ({before} -> {after})", after == before - 1)
        check("Forget did not delete the folder", PROJECT_DIR.is_dir())

        page.locator("#projectModalCancelBtn").click()
        page.wait_for_timeout(300)

        # ==== orthogonal viewer ==================================================
        page.locator("#visualizeBtn").click()
        page.wait_for_selector(".viz-popup", timeout=5000)
        viz = page.locator(".winbox.viz-winbox").first

        check("Three orthogonal panels exist", viz.locator(".viz-panel").count() == 3)
        check("Controls live in a right-hand rail", viz.locator(".viz-side").count() == 1)
        check("Path inputs use the compact class",
              viz.locator('input.viz-input-sm[data-role="viz-path"]').count() == 1)

        viz.locator('[data-role="viz-path"]').fill("Tomograms/job099/TS_77.mrc")
        viz.locator('[data-role="viz-particles"]').fill("Tomograms/job099/picks99.star")
        viz.locator('[data-role="viz-load"]').click()
        page.wait_for_selector('[data-role="viz-img"]', state="visible", timeout=8000)
        page.wait_for_timeout(1800)

        # every panel actually loaded an image
        loaded = viz.locator(".viz-panel img").evaluate_all(
            "els => els.map(e => e.naturalWidth)")
        check(f"All three panels loaded a slice ({loaded})",
              len(loaded) == 3 and all(w > 0 for w in loaded))

        # ...and each has the dimensions its plane implies
        sizes = viz.locator(".viz-panel img").evaluate_all(
            "els => els.map(e => [e.getAttribute('data-role'), e.naturalWidth, e.naturalHeight])")
        by_role = {r: (w, h) for r, w, h in sizes}
        check(f"XY panel is nx x ny ({by_role.get('viz-img')})", by_role["viz-img"] == (nx, ny))
        check(f"ZY panel is nz x ny — transposed ({by_role.get('img-zy')})", by_role["img-zy"] == (nz, ny))
        check(f"XZ panel is nx x nz ({by_role.get('img-xz')})", by_role["img-xz"] == (nx, nz))

        # panels are laid out with one isotropic voxel scale, so the shared
        # edges match: XY and XZ share width, XY and ZY share height
        box = viz.locator(".viz-panel[data-panel='xy']").bounding_box()
        box_xz = viz.locator(".viz-panel[data-panel='xz']").bounding_box()
        box_zy = viz.locator(".viz-panel[data-panel='zy']").bounding_box()
        check(f"XY and XZ share a width ({box['width']:.0f} vs {box_xz['width']:.0f})",
              abs(box["width"] - box_xz["width"]) < 2)
        check(f"XY and ZY share a height ({box['height']:.0f} vs {box_zy['height']:.0f})",
              abs(box["height"] - box_zy["height"]) < 2)
        check("ZY sits to the left of XY", box_zy["x"] < box["x"])
        check("XZ sits below XY", box_xz["y"] > box["y"])
        check("XY is the largest panel",
              box["width"] * box["height"] > box_xz["width"] * box_xz["height"]
              and box["width"] * box["height"] > box_zy["width"] * box_zy["height"])

        # crosshair starts centred
        meta = viz.locator('[data-role="viz-meta"]').inner_text()
        check(f"Meta reports the volume and crosshair ({meta})",
              "TS_77" in meta and "3 picks" in meta
              and f"x {nx // 2}" in meta and f"z {nz // 2}" in meta)

        # clicking in XY moves x and y (not z), and refreshes the other panels
        zy_src_before = viz.locator('[data-role="img-zy"]').get_attribute("src")
        viz.locator(".viz-panel[data-panel='xy']").click(position={"x": 20, "y": 20})
        page.wait_for_timeout(900)
        meta2 = viz.locator('[data-role="viz-meta"]').inner_text()
        check(f"Clicking XY moved the crosshair ({meta2.split('·')[-1].strip()})",
              meta2 != meta and f"z {nz // 2}" in meta2)
        check("Clicking XY refetched the ZY slice",
              viz.locator('[data-role="img-zy"]').get_attribute("src") != zy_src_before)

        # scrolling over XY steps through Z only
        xy_src_before = viz.locator('[data-role="viz-img"]').get_attribute("src")
        viz.locator(".viz-panel[data-panel='xy']").hover()
        page.mouse.wheel(0, 200)
        page.wait_for_timeout(700)
        check("Scrolling XY stepped through Z",
              viz.locator('[data-role="viz-img"]').get_attribute("src") != xy_src_before)

        # sliders drive the same state
        viz.locator('[data-role="pos-z"]').fill("3")
        viz.locator('[data-role="pos-z"]').dispatch_event("input")
        page.wait_for_timeout(700)
        check("Z slider moves the crosshair",
              "z 3" in viz.locator('[data-role="viz-meta"]').inner_text())

        # overlays are sized to their panels (picks/crosshair drawn, not blank)
        cv = viz.locator(".viz-panel canvas").evaluate_all(
            "els => els.map(e => [e.width, e.height])")
        check(f"All overlays sized to their panel ({cv})",
              len(cv) == 3 and all(w > 0 and h > 0 for w, h in cv))
        painted = viz.locator('[data-role="ov-xy"]').evaluate(
            "c => { const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;"
            "       for (let i = 3; i < d.length; i += 4) if (d[i]) return true; return false; }")
        check("XY overlay has something drawn on it", painted)

        # toggling the crosshair off clears it
        viz.locator('[data-role="viz-crosshair"]').uncheck()
        viz.locator('[data-role="viz-showpicks"]').uncheck()
        page.wait_for_timeout(400)
        blank = viz.locator('[data-role="ov-xy"]').evaluate(
            "c => { const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;"
            "       for (let i = 3; i < d.length; i += 4) if (d[i]) return false; return true; }")
        check("Turning off picks + crosshair clears the overlay", blank)

        # Close before the Browse-pickers section reopens a fresh visualizer
        # over a different fixture at the same path -- openVisualizer() always
        # creates a new popup rather than reusing one, so a stale popup left
        # open here would leave two and make a later ".first" ambiguous.
        viz.locator(".wb-close").first.click()
        page.wait_for_timeout(300)

        # ==== theme switch ========================================================
        check("Starts on the dark theme",
              page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark")
        dark_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        page.locator("#themeBtn").click()
        page.wait_for_timeout(250)
        check("Toggles to light",
              page.evaluate("document.documentElement.getAttribute('data-theme')") == "light")
        light_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        check("Body background actually changes", dark_bg != light_bg)
        check("Button label reflects the current theme",
              "Light" in page.locator("#themeBtn").inner_text())

        # persists across a reload
        page.reload(wait_until="networkidle")
        page.wait_for_selector(".job-item", timeout=5000)
        check("Theme persists across reload",
              page.evaluate("document.documentElement.getAttribute('data-theme')") == "light")
        page.locator("#themeBtn").click()   # back to dark
        page.wait_for_timeout(200)

        # ==== progress tab ========================================================
        page.locator("#jobSearch").fill("2D classification")
        page.wait_for_timeout(300)
        page.locator(".job-item:visible", has_text="2D classification").first.click()
        page.wait_for_selector(".winbox", timeout=5000)
        win = page.locator(".winbox").first
        page.evaluate(
            "() => { const w = document.querySelector('.winbox');"
            " w.style.height='880px'; w.style.top='30px'; }"
        )

        cmd = win.locator(".command-box").input_value()
        m = re.search(r"--(?:o|output-directory)\s+(\S+)", cmd)
        subdir = (m.group(1).rstrip("/") if m else "Class2D/job001").strip("'\"")
        win.locator(".command-box").fill(f"python3 fake_iterative_job.py {subdir}")
        win.locator('[data-role="run-btn"]').click()
        page.wait_for_timeout(3000)

        prog_tab = win.locator('.tab-btn[data-tab="progress"]')
        check("Progress tab appears for Class2D", prog_tab.is_visible())
        prog_tab.click()
        page.wait_for_timeout(4000)

        check("Live-progress toggle present", win.locator('[data-role="prog-enabled"]').count() == 1)
        check("Thumbnail-interval control present", win.locator('[data-role="prog-every"]').count() == 1)
        check("Iteration picker present, defaulting to Latest",
              win.locator('[data-role="prog-iter-select"]').count() == 1
              and win.locator('[data-role="prog-iter-select"]').input_value() == "latest")
        check("Both charts drawn", win.locator(".progress-chart").count() == 2)
        check("Legend present for the 2-series chart", win.locator(".progress-legend").count() == 1)
        check("Class thumbnails rendered", win.locator(".thumb img").count() >= 1)
        check("Thumbnails actually load",
              win.locator(".thumb img").evaluate_all("els => els.every(e => e.naturalWidth > 0)"))

        status_text = win.locator('[data-role="prog-status"]').inner_text()
        check(f"Status line reports the iteration ({status_text})", "iteration" in status_text)

        # Polling advances while the job runs. Waiting for the text to change
        # rather than sleeping a fixed 5 s: under load the poll interval and
        # the job's own pace both stretch, and a fixed sleep turns that into a
        # flaky failure rather than a slower pass.
        first_iter = status_text
        advanced = True
        try:
            page.wait_for_function(
                """prev => {
                     const el = document.querySelector('[data-role="prog-status"]');
                     return el && el.innerText.trim() && el.innerText !== prev;
                   }""",
                arg=first_iter, timeout=20000)
        except Exception:
            advanced = False
        check(f"Iteration advances while running (from {first_iter!r})", advanced)

        # charts survive a theme switch (they're drawn with resolved colours)
        page.locator("#themeBtn").click()
        page.wait_for_timeout(900)
        check("Charts repaint after a theme switch", win.locator(".progress-chart").count() == 2)
        page.locator("#themeBtn").click()
        page.wait_for_timeout(500)

        # turning it off stops the view entirely
        win.locator('[data-role="prog-enabled"]').uncheck()
        page.wait_for_timeout(700)
        check("Disabling live progress clears the view",
              "off for this job" in win.locator(".progress-empty").first.inner_text())
        win.locator('[data-role="prog-enabled"]').check()
        page.wait_for_timeout(2500)
        check("Re-enabling restores the charts", win.locator(".progress-chart").count() == 2)

        # picking an earlier iteration swaps the thumbnails/status to that
        # iteration instead of following the latest
        options = win.locator('[data-role="prog-iter-select"] option').all_inner_texts()
        check(f"Iteration picker lists more than just Latest ({options})", len(options) > 1)
        earlier_value = win.locator('[data-role="prog-iter-select"] option').nth(1).get_attribute("value")
        win.locator('[data-role="prog-iter-select"]').select_option(earlier_value)
        page.wait_for_timeout(700)
        status_after_pick = win.locator('[data-role="prog-status"]').inner_text()
        check(f"Status line reflects the picked iteration ({status_after_pick!r})",
              f"iteration {earlier_value}" in status_after_pick)
        check("Thumbnails still render for the picked iteration", win.locator(".thumb img").count() >= 1)

        win.locator('[data-action="close"]').click()
        page.wait_for_timeout(300)

        # ==== CTF QC tab (Ctffind only) ==========================================
        # Regression coverage for a real bug found while auditing this suite:
        # refreshCtfQcTabVisibility() only ever called refreshCtfQc() once
        # (guarded by ctfQcContent.dataset.built), same "built once" pattern
        # that broke Progress polling above -- but CTF QC has NO polling loop
        # by design (RELION writes its summary once, at the very end), so a
        # job that was still running when its tab first built would show
        # "not available" forever, even after finishing. Fixed by re-checking
        # on every subsequent refreshToolbarState() call (status transitions,
        # incl. the websocket's "completed" message) until data.available is
        # true. This section starts the job and opens the tab BEFORE the job
        # finishes, specifically to exercise that "still running when first
        # built, must catch up later" path rather than the easier case of
        # opening the tab only after completion.
        page.locator("#jobSearch").fill("CTF Estimation")
        page.wait_for_timeout(300)
        # has_text does a substring match, and "CTF Estimation" is also a
        # prefix of TomoCtffind's own display name "CTF Estimation (Tomo)"
        # -- anchor to the start of the item's own text and require a
        # newline right after (the item's title line, before its
        # description paragraph) to land on the plain SPA job specifically.
        page.locator(".job-item:visible", has_text=re.compile(r"^CTF Estimation\s*\n")).first.click()
        page.wait_for_selector(".winbox", timeout=5000)
        ctf_win = page.locator(".winbox").first
        ctf_cmd = ctf_win.locator(".command-box").input_value()
        ctf_m = re.search(r"--(?:o|output-directory)\s+(\S+)", ctf_cmd)
        ctf_subdir = (ctf_m.group(1).rstrip("/") if ctf_m else "CtfFind/job001").strip("'\"")
        ctf_win.locator(".command-box").fill(f"sleep 2 && python3 fake_ctffind_job.py {ctf_subdir}")
        ctf_win.locator('[data-role="run-btn"]').click()
        page.wait_for_timeout(800)

        ctf_tab = ctf_win.locator('.tab-btn[data-tab="ctfqc"]')
        check("CTF QC tab appears for Ctffind", ctf_tab.is_visible())
        ctf_tab.click()
        page.wait_for_timeout(500)
        check("CTF QC tab shows not-yet-available while the job is still running (sleep 2 hasn't finished)",
              "not available" in ctf_win.locator('[data-tab-content="ctfqc"]').inner_text().lower())

        # Not wait_for_selector(...status-line...completed), state="visible"
        # by default -- the CTF QC tab is the active one now, so the Inputs
        # tab's own status-line (a sibling tab-content) is CSS-hidden even
        # though its text already says "completed". Check the text content
        # directly instead, regardless of which tab happens to be visible.
        page.wait_for_function(
            """() => {
                 const el = document.querySelector('[data-role="status-line"]');
                 return el && el.textContent.includes("completed");
               }""",
            timeout=10000)
        page.wait_for_timeout(1000)
        ctf_status = ctf_win.locator('[data-role="ctfqc-status"]').inner_text()
        check(f"CTF QC tab picks up the results after the job completes ({ctf_status!r})",
              "5 micrograph" in ctf_status)
        check("CTF QC charts render", ctf_win.locator('[data-tab-content="ctfqc"] .progress-chart').count() >= 1)
        check("CTF QC worst-fit thumbnails render", ctf_win.locator('[data-tab-content="ctfqc"] .thumb img').count() >= 1)
        check("CTF QC thumbnails actually load",
              ctf_win.locator('[data-tab-content="ctfqc"] .thumb img').evaluate_all(
                  "els => els.length > 0 && els.every(e => e.naturalWidth > 0)"))

        ctf_win.locator('[data-action="close"]').click()
        page.wait_for_timeout(300)

        # ==== visualizer Browse buttons ==========================================
        # Fixture tree so the picker has folders + files to walk. Overwrites the
        # same Tomograms/job099 path the orthogonal-viewer section used above,
        # with different dims/pick count -- fine, since that section's checks
        # and its own visualizer popup are already done and closed.
        import numpy as np, mrcfile, starfile, pandas as pd  # noqa: E401
        tomo_dir = PROJECT_DIR / "Tomograms" / "job099"
        tomo_dir.mkdir(parents=True, exist_ok=True)
        with mrcfile.new(tomo_dir / "TS_77.mrc", overwrite=True) as mrc:
            mrc.set_data((np.random.rand(12, 24, 24) * 100).astype(np.float32))
            mrc.voxel_size = 10.0
        starfile.write(
            {"particles": pd.DataFrame({
                "rlnTomoName": ["TS_77"] * 2,
                "rlnCoordinateX": [5.0, 9.0], "rlnCoordinateY": [5.0, 9.0],
                "rlnCoordinateZ": [3.0, 6.0]})},
            tomo_dir / "picks99.star", overwrite=True)
        (PROJECT_DIR / "ignore_me.txt").write_text("must not appear in the picker")

        page.locator("#visualizeBtn").click()
        page.wait_for_selector(".viz-popup", timeout=5000)
        # target the viewer window specifically -- the Class2D popup from the
        # progress checks above is still open, so ".winbox".first is not it
        viz = page.locator(".winbox.viz-winbox").first
        check("Both visualizer inputs have a Browse button",
              viz.locator('[data-role="viz-browse-main"]').count() == 1
              and viz.locator('[data-role="viz-browse-particles"]').count() == 1)

        viz.locator('[data-role="viz-browse-main"]').click()
        page.wait_for_selector(".file-picker .project-browser:not([aria-busy])", timeout=5000)
        root_entries = page.locator(".file-picker .browser-entry").all_inner_texts()
        check("Picker hides files that don't match the extension filter",
              not any("ignore_me.txt" in e for e in root_entries))
        page.locator(".file-picker .browser-entry", has_text="Tomograms").click()
        page.wait_for_timeout(400)
        page.locator(".file-picker .browser-entry", has_text="job099").click()
        page.wait_for_timeout(400)
        page.locator(".file-picker .browser-entry", has_text="TS_77.mrc").click()
        page.wait_for_timeout(400)
        check("Picking a file closes the picker", page.locator(".file-picker").count() == 0)
        check("Picked path is project-relative",
              viz.locator('[data-role="viz-path"]').input_value() == "Tomograms/job099/TS_77.mrc")

        viz.locator('[data-role="viz-browse-particles"]').click()
        page.wait_for_selector(".file-picker .project-browser:not([aria-busy])", timeout=5000)
        check("Second picker resumes in the same folder",
              "job099" in page.locator(".file-picker .picker-current").inner_text())
        star_only = page.locator(".file-picker .browser-entry.picker-file").all_inner_texts()
        check("Particles picker offers only .star files",
              star_only and all(e.strip().endswith(".star") for e in star_only))
        page.locator(".file-picker .browser-entry", has_text="picks99.star").click()
        page.wait_for_timeout(400)

        viz.locator('[data-role="viz-load"]').click()
        page.wait_for_selector('[data-role="viz-img"]', state="visible", timeout=8000)
        page.wait_for_timeout(1500)
        meta = viz.locator('[data-role="viz-meta"]').inner_text()
        check(f"Browsed files load in the viewer ({meta})", "TS_77" in meta and "2 picks" in meta)

        # Esc closes the picker without changing the field
        viz.locator('[data-role="viz-browse-main"]').click()
        page.wait_for_selector(".file-picker .project-browser:not([aria-busy])", timeout=5000)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        check("Escape cancels the picker", page.locator(".file-picker").count() == 0)
        check("Cancelling leaves the field unchanged",
              viz.locator('[data-role="viz-path"]').input_value() == "Tomograms/job099/TS_77.mrc")

        browser.close()

    print()
    if errors:
        print("CONSOLE/PAGE ERRORS:")
        for e in errors:
            print(" -", e)
        return False
    print("No console/page errors.")
    return True


clean = main()
print()
print("OVERALL:", "PASS" if (ok and clean) else "FAIL")
sys.exit(0 if (ok and clean) else 1)
