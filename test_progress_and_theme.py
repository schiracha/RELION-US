"""
Playwright smoke test for the job Progress tab (live charts + class thumbnails)
and the dark/light theme switch.

Needs a live backend on an EMPTY project, and writes a small helper script into
the project that emits RELION-style per-iteration files (run_it###_model.star +
run_it###_classes.mrcs) so the Progress tab has real files to read — the same
shape RELION's own Class2D writes.

Usage: python3 test_progress_and_theme.py [base_url] [project_dir]
"""
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

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


FAKE_JOB = '''
import sys, time, numpy as np, mrcfile, starfile, pandas as pd
from pathlib import Path
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
NC = 4
for it in range(1, 5):
    stack = np.random.rand(NC, 48, 48).astype(np.float32) * 0.3
    yy, xx = np.mgrid[0:48, 0:48]
    for k in range(NC):
        stack[k] += np.exp(-(((xx-24)**2 + (yy-24)**2) / (40.0 + 30*k)))
    with mrcfile.new(out/f"run_it{it:03d}_classes.mrcs", overwrite=True) as m:
        m.set_data(stack)
    dist = np.array([0.4, 0.3, 0.2, 0.1])
    starfile.write({
      "model_general": pd.DataFrame({"rlnCurrentResolution":[1/(20.0-it*1.5)],
          "rlnNrClasses":[NC], "rlnReferenceDimensionality":[2], "rlnPixelSize":[1.4]}),
      "model_classes": pd.DataFrame({
          "rlnReferenceImage":[f"{k+1:06d}@run_it{it:03d}_classes.mrcs" for k in range(NC)],
          "rlnClassDistribution": dist,
          "rlnEstimatedResolution":[22.0-it + k for k in range(NC)]})},
      out/f"run_it{it:03d}_model.star", overwrite=True)
    print(f"Iteration {it}/4", flush=True)
    time.sleep(1.5)
print("done")
'''


def main():
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    (PROJECT_DIR / "fake_iterative_job.py").write_text(FAKE_JOB)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
        )
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)

        page.goto(BASE_URL + "/", wait_until="networkidle")
        page.wait_for_selector(".job-item", timeout=5000)

        # ---------------- theme switch ----------------
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

        # ---------------- progress tab ----------------
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
        check("Keep-all toggle present (off by default)",
              win.locator('[data-role="prog-keepall"]').count() == 1
              and not win.locator('[data-role="prog-keepall"]').is_checked())
        check("Both charts drawn", win.locator(".progress-chart").count() == 2)
        check("Legend present for the 2-series chart", win.locator(".progress-legend").count() == 1)
        check("Class thumbnails rendered", win.locator(".thumb img").count() >= 1)
        check("Thumbnails actually load",
              win.locator(".thumb img").evaluate_all("els => els.every(e => e.naturalWidth > 0)"))

        status_text = win.locator('[data-role="prog-status"]').inner_text()
        check(f"Status line reports the iteration ({status_text})", "iteration" in status_text)

        # polling advances while the job runs
        first_iter = status_text
        page.wait_for_timeout(5000)
        check("Iteration advances while running",
              win.locator('[data-role="prog-status"]').inner_text() != first_iter)

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

        # keep-all groups thumbnails by iteration
        win.locator('[data-role="prog-keepall"]').check()
        page.wait_for_timeout(2500)
        check("Keep-all groups thumbnails by iteration", win.locator(".thumb-iter").count() >= 1)

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
