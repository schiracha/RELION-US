"""
Playwright suite for the Filter Picks job (interactive select-on-value) and its tuner window.

Fixtures are written by a real job (a python one-liner run through the app), with scores chosen so
every count can be checked by hand:

  tomogram A: 10 picks scoring 0.1 ... 1.0, all within the middle slice;   tomogram B: 6 picks
  scoring 0.15 0.35 0.55 0.75 0.85 0.95 (no volume given for it: drawn as a scatter)
  micrograph m1: 5 picks scoring 0.2 0.4 0.6 0.8 1.0 (single particle)

A minimum of 0.5 keeps 6 of A's, 4 of B's, 3 of m1's.

Usage: python3 test_pick_filter_popup.py [base_url]
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8420"

errors = []
ok = True


def check(label, cond):
    global ok
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        ok = False


def launch_browser(p):
    exe = os.environ.get("RELION_US_CHROMIUM")
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()


MAKE_FIXTURES = (
    "import mrcfile, numpy as np, pandas as pd, starfile, os\n"
    "os.makedirs('Picks', exist_ok=True); os.makedirs('Tomograms', exist_ok=True); os.makedirs('mics', exist_ok=True)\n"
    "rng = np.random.default_rng(0)\n"
    "with mrcfile.new('Tomograms/A.mrc', overwrite=True) as m:\n"
    "    m.set_data(rng.normal(size=(30, 60, 80)).astype(np.float32)); m.voxel_size = 10.0\n"
    "with mrcfile.new('mics/m1.mrc', overwrite=True) as m:\n"
    "    m.set_data(rng.normal(size=(60, 80)).astype(np.float32)); m.voxel_size = 1.0\n"
    "a = [round(0.1 * i, 2) for i in range(1, 11)]\n"
    "b = [0.15, 0.35, 0.55, 0.75, 0.85, 0.95]\n"
    "rows = [dict(rlnTomoName='A', rlnCoordinateX=5.0 + 6 * i, rlnCoordinateY=10.0 + 4 * i, rlnCoordinateZ=15.0,\n"
    "             rlnClassNumber=1, rlnAutopickFigureOfMerit=s) for i, s in enumerate(a)]\n"
    "rows += [dict(rlnTomoName='B', rlnCoordinateX=8.0 + 9 * i, rlnCoordinateY=7.0 + 5 * i, rlnCoordinateZ=12.0 + i,\n"
    "              rlnClassNumber=1, rlnAutopickFigureOfMerit=s) for i, s in enumerate(b)]\n"
    "starfile.write({'particles': pd.DataFrame(rows)}, 'Picks/tomo.star', overwrite=True)\n"
    "starfile.write({'': pd.DataFrame({'rlnTomoName': ['A'], 'rlnTomoReconstructedTomogram': ['Tomograms/A.mrc']})},\n"
    "               'Tomograms/tomograms.star', overwrite=True)\n"
    "spa = [dict(rlnMicrographName='mics/m1.mrc', rlnCoordinateX=10.0 + 12 * i, rlnCoordinateY=8.0 + 9 * i,\n"
    "            rlnAutopickFigureOfMerit=s) for i, s in enumerate([0.2, 0.4, 0.6, 0.8, 1.0])]\n"
    "starfile.write({'particles': pd.DataFrame(spa)}, 'Picks/spa.star', overwrite=True)\n"
)


def make_fixtures(page):
    command = f"{sys.executable} -c \"{MAKE_FIXTURES}\""
    run = page.request.post(BASE_URL + "/api/runs", data={"internal_name": "IsonetDeconv", "command": command}).json()
    for _ in range(100):
        status = page.request.get(f"{BASE_URL}/api/runs/{run['run_id']}").json()
        if status["status"] in ("completed", "failed"):
            return status
        time.sleep(0.2)
    return status


def open_job(page, title):
    page.locator(".job-item", has_text=title).first.dispatch_event("click")
    page.wait_for_selector(".winbox .job-standard-form .field-label", timeout=5000)
    return page.locator(".winbox").last


def pick_filter_runs(page):
    return [r for r in page.request.get(BASE_URL + "/api/runs").json() if r["internal_name"] == "PickFilter"]


def output_rows(page, run_id):
    resp = page.request.get(f"{BASE_URL}/api/runs/{run_id}/files/preview", params={"path": "particles.star"})
    blocks = resp.json()["blocks"] if resp.ok else []
    return blocks[0]["total_rows"] if blocks else None


with sync_playwright() as p:
    browser = launch_browser(p)
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE_URL + "/", wait_until="networkidle")
    page.wait_for_selector(".job-item", timeout=5000)

    check("fixtures were written by a job", make_fixtures(page)["status"] == "completed")

    # ---- the job ----
    names = page.locator(".job-item").all_inner_texts()
    check("Filter Picks is in the job list", any("Filter Picks" in n for n in names))
    win = open_job(page, "Filter Picks")
    fields = win.locator(".job-standard-form input[type=text]")
    fields.nth(0).fill("Picks/tomo.star")
    fields.nth(1).fill("Tomograms/tomograms.star")
    win.locator('[data-role="run-btn"]').click()
    page.wait_for_selector('[data-role="status-line"]:has-text("running")', timeout=10000)
    tuner_btn = win.locator('[data-action="picker"]')
    check("the job stays running with an Open Tuner button and a Done button",
          tuner_btn.is_visible() and "Tuner" in tuner_btn.inner_text() and win.locator('[data-role="done-btn"]').is_visible())
    run = pick_filter_runs(page)[-1]
    check("it started by keeping every pick", output_rows(page, run["run_id"]) == 16)
    check("it is numbered as a Select job", run["cwd"].rstrip("/").split("/")[-2] == "Select")

    # ---- the tuner ----
    tuner_btn.click()
    page.wait_for_selector(".pf-popup", timeout=10000)
    page.wait_for_function("document.querySelector('[data-role=\"pf-count\"]').textContent.includes('of 16')", timeout=10000)
    q = lambda role: page.locator(f'.pf-popup [data-role="{role}"]')   # noqa: E731
    check("the pick score is the default column", q("pf-col").input_value() == "rlnAutopickFigureOfMerit")
    groups = page.evaluate("[...document.querySelectorAll('.pf-popup [data-role=\"pf-col\"] optgroup')].map(g => g.label)")
    check("columns are grouped, score-like first", groups[0] == "Scores and quality" and "Position" in groups)
    check("everything is kept at first", "16 of 16 kept (100%)" in q("pf-count").inner_text())
    check("both tomograms are listed with their counts", page.locator(".pf-image-row").count() == 2
          and "10 / 10" in page.locator(".pf-image-row").first.inner_text() + page.locator(".pf-image-row").nth(1).inner_text())
    check("the image that can be drawn is chosen first, and sliced", q("pf-imgname").inner_text() == "A"
          and "Tomograms%2FA.mrc" in (q("pf-img").get_attribute("src") or ""))
    page.wait_for_function("document.querySelector('[data-role=\"pf-previewinfo\"]').textContent.includes('in slice')", timeout=10000)
    check("its picks are all in the slice: 10 kept, none excluded", "10 picks in slice z=15" in q("pf-previewinfo").inner_text()
          and "10 kept, 0 excluded" in q("pf-previewinfo").inner_text())
    check("the histogram is drawn", page.evaluate("document.querySelector('.pf-popup [data-role=\"pf-hist\"]').width") > 100)

    q("pf-lo").fill("0.5")
    page.wait_for_function("document.querySelector('[data-role=\"pf-count\"]').textContent.includes('10 of 16')", timeout=5000)
    check("a minimum of 0.5 keeps 10 of 16", "10 of 16 kept (63%)" in q("pf-count").inner_text()
          and "6 excluded" in q("pf-count-sub").inner_text())
    rows_text = page.locator(".pf-images").inner_text()
    check("per-image counts follow (A 6 of 10, B 4 of 6)", "6 / 10" in rows_text and "4 / 6" in rows_text)
    check("the preview says which picks are kept and excluded", "6 kept, 4 excluded" in q("pf-previewinfo").inner_text())
    check("unapplied changes are flagged", "Not applied yet" in q("pf-status").inner_text())
    painted = page.evaluate("""() => { const c = document.querySelector('.pf-popup [data-role="pf-overlay"]');
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let g = 0, r = 0;
        for (let i = 0; i < d.length; i += 4) { if (d[i+3] > 100) { if (d[i+1] > 150 && d[i] < 120) g++; if (d[i] > 180 && d[i+1] < 120) r++; } }
        return [g, r]; }""")
    check("kept picks are drawn green and excluded ones red", painted[0] > 20 and painted[1] > 20)

    page.locator(".pf-image-row", has_text="B").click()
    page.wait_for_function("document.querySelector('[data-role=\"pf-imgname\"]').textContent === 'B'"
                           " && document.querySelector('[data-role=\"pf-img\"]').hidden"
                           " && document.querySelector('[data-role=\"pf-previewinfo\"]').textContent.includes('in slice')", timeout=8000)
    check("a tomogram without a volume is a scatter of its picks (hidden image, depth slider)",
          q("pf-img").is_hidden() and q("pf-z").is_visible())
    page.locator(".pf-image-row", has_text="A").click()
    page.wait_for_function("document.querySelector('[data-role=\"pf-imgname\"]').textContent === 'A'"
                           " && !document.querySelector('[data-role=\"pf-img\"]').hidden", timeout=8000)

    # the bound lines can be dragged on the histogram itself
    dom = page.evaluate("""() => { const r = document.querySelector('.pf-popup [data-role="pf-lo-range"]');
        return [Number(r.min), Number(r.max)]; }""")
    box = page.locator('.pf-popup [data-role="pf-hist"]').bounding_box()
    px = lambda v: box["x"] + 40 + (v - dom[0]) / (dom[1] - dom[0]) * (box["width"] - 52)   # noqa: E731
    page.mouse.move(px(0.5), box["y"] + box["height"] * 0.5)
    page.mouse.down()
    page.mouse.move(px(0.7), box["y"] + box["height"] * 0.5, steps=6)
    page.mouse.up()
    dragged = float(q("pf-lo").input_value())
    check(f"dragging the lower bound on the histogram moves it ({dragged})", 0.66 < dragged < 0.74)
    q("pf-lo").fill("0.5")
    page.wait_for_function("document.querySelector('[data-role=\"pf-count\"]').textContent.includes('10 of 16')", timeout=5000)

    # column change
    q("pf-col").select_option("rlnCoordinateZ")
    page.wait_for_function("document.querySelector('[data-role=\"pf-count\"]').textContent.includes('16 of 16')", timeout=5000)
    check("another column resets the range and keeps everything", "16 of 16 kept" in q("pf-count").inner_text()
          and "Not applied" not in q("pf-status").inner_text())
    q("pf-col").select_option("rlnAutopickFigureOfMerit")
    page.wait_for_function("document.querySelector('[data-role=\"pf-count\"]').textContent.includes('of 16')", timeout=5000)
    q("pf-lo").fill("0.5")
    page.wait_for_function("document.querySelector('[data-role=\"pf-count\"]').textContent.includes('10 of 16')", timeout=5000)

    # closing with an unapplied selection asks first
    page.locator(".pf-winbox .wb-close").click()
    page.wait_for_selector(".mini-dialog", timeout=3000)
    check("closing with unapplied changes asks first", "has not been applied" in page.locator(".mini-dialog").inner_text())
    page.locator('.mini-dialog [data-role="cancel"]').click()
    page.wait_for_timeout(300)
    check("cancelling keeps the tuner open", page.locator(".pf-popup").count() == 1)

    # apply
    q("pf-apply").click()
    page.wait_for_function("document.querySelector('[data-role=\"pf-status\"]').textContent.includes('Applied')", timeout=10000)
    check("applying writes the selection", "Applied: 10 of 16" in q("pf-status").inner_text())
    check("the job's particles.star holds exactly those picks", output_rows(page, run["run_id"]) == 10)
    state = page.request.get(f"{BASE_URL}/api/runs/{run['run_id']}").json()
    fv = state["field_values"]
    check("the job remembers what was tuned", fv["select_label"] == "rlnAutopickFigureOfMerit" and fv["select_minval"] == "0.5")

    # a reopened tuner starts from what the job holds
    page.locator(".pf-winbox .wb-close").click()                  # applied: closes without a question
    page.wait_for_timeout(500)
    check("an applied tuner closes without asking", page.locator(".pf-popup").count() == 0)
    tuner_btn.click()
    page.wait_for_selector(".pf-popup", timeout=10000)
    page.wait_for_function("document.querySelector('[data-role=\"pf-count\"]').textContent.includes('of 16')", timeout=10000)
    check("reopened, it starts from the job's selection", q("pf-lo").input_value() == "0.5"
          and "10 of 16 kept" in q("pf-count").inner_text())
    page.locator(".pf-winbox .wb-close").click()
    page.wait_for_timeout(400)

    # Done / Continue
    win.locator('[data-role="done-btn"]').click()
    page.wait_for_selector(".mini-dialog", timeout=3000)
    check("Done asks, in the tuner's own words", "tuning session" in page.locator(".mini-dialog").inner_text())
    page.locator('.mini-dialog [data-role="confirm"]').click()
    page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)
    late = page.request.post(f"{BASE_URL}/api/pick-filter/{run['run_id']}/apply",
                             data={"column": "rlnAutopickFigureOfMerit", "lo": 0.1, "hi": 1.0})
    check("after Done the output is frozen until Continue", late.status == 409)
    win.locator('[data-action="continue-picking"]').click()
    page.wait_for_selector('[data-role="status-line"]:has-text("running")', timeout=10000)
    check("Continue reopens the session with the output intact", output_rows(page, run["run_id"]) == 10)

    # ---- single particle: micrographs name their own image ----
    page.locator(".winbox .wb-close").first.click()
    page.wait_for_timeout(300)
    win2 = open_job(page, "Filter Picks")
    win2.locator(".job-standard-form input[type=text]").nth(0).fill("Picks/spa.star")
    win2.locator('[data-role="run-btn"]').click()
    page.wait_for_selector('[data-role="status-line"]:has-text("running")', timeout=10000)
    win2.locator('[data-action="picker"]').click()
    page.wait_for_selector(".pf-popup", timeout=10000)
    page.wait_for_function("document.querySelector('[data-role=\"pf-count\"]').textContent.includes('of 5')", timeout=10000)
    check("single-particle picks are listed by micrograph", "m1.mrc" in page.locator(".pf-image-row").first.inner_text())
    page.wait_for_function("(document.querySelector('[data-role=\"pf-img\"]').getAttribute('src') || '').includes('m1.mrc')", timeout=8000)
    check("the micrograph is drawn without a depth slider",
          "mics%2Fm1.mrc" in (q("pf-img").get_attribute("src") or "") and q("pf-z").is_hidden())
    q("pf-lo").fill("0.5")
    page.wait_for_function("document.querySelector('[data-role=\"pf-count\"]').textContent.includes('3 of 5')", timeout=5000)
    check("a minimum of 0.5 keeps 3 of 5", "3 of 5 kept" in q("pf-count").inner_text()
          and "3 kept, 2 excluded" in q("pf-previewinfo").inner_text())

    check(f"no console/page errors ({errors})", not errors)
    browser.close()

print("\nALL PASSED" if ok else "\nSOME FAILED")
sys.exit(0 if ok else 1)
