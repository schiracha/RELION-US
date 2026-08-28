"""
Playwright test for the Analyze popup (Menu > Tools > Analyze) -- not a job
(never appears in the Command Center, nothing here polls or streams output),
inspired by CNIO_Relion_Tools' relion_analyse.py (see NOTICE.md). Pipeline
(C1), 2D/3D Classification + 3D Refine (C2), the FSC chart + viewing-
direction heatmap (C3), and the Micrographs and Particles tabs' scatter
plots with rectangle-select + STAR export (C4) are all wired -- every tab
has real content now. This suite covers: the popup shell (all six tabs
present), the Pipeline tab reusing Command Center's own lineage-graph
renderer plus the click-for-job-summary panel, the Particles and
Micrographs tabs' shared scatter widget (path input + axis pickers + canvas
scatter + drag-to-select + export -- this repo's first STAR-*writing* code,
plus its path/filename-traversal guards; Micrographs additionally checks
that MotionCorr's motion-tracking columns get merged in), the 2D
Classification tab's run picker + convergence/class-distribution charts,
and the 3D Classification tab's additional FSC chart (with its FSC/SSNR
metric picker) and on-demand viewing-direction heatmap (reusing the
Progress tab's own drawOrientationHeatmap and /orientation-distribution
endpoint).

Needs a live backend already pointed at run_tests.sh's branching fixture
project (same one test_network_branching.py uses -- make_legacy_branchy_project)
plus a real Class2D run, a real Class3D run, a real particles.star, and a
CtfFind+MotionCorr micrographs pair (add_analyze_classification_run), so the
Pipeline tab's node/edge shape and every wired tab's own data have
known-correct fixtures to check against.

Usage: python3 test_analyze.py [base_url] [project_dir]
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright


def launch_browser(p):
    exe = os.environ.get("RELION_US_CHROMIUM")
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8434"

errors = []
ok = True


def check(label, cond):
    global ok
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        ok = False


def main():
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}")
                if r.status >= 400 and "favicon" not in r.url else None)

        page.goto(BASE_URL + "/", wait_until="networkidle")
        page.wait_for_selector(".job-item", timeout=5000)
        page.wait_for_timeout(600)

        # ---- opening it ----
        page.locator("#menuBtn").click()
        page.locator("#menuToolsBtn").click()
        page.locator("#menuAnalyzeBtn").click()
        page.wait_for_selector(".analyze-winbox", timeout=5000)
        win = page.locator(".analyze-winbox")
        page.wait_for_timeout(600)

        tab_labels = win.locator(".tab-btn").all_inner_texts()
        check(f"All six tabs present ({tab_labels})",
              tab_labels == ["Pipeline", "Micrographs", "Particles",
                              "2D Classification", "3D Classification", "3D Refine"])
        check("Pipeline is the tab open by default",
              "active" in (win.locator('.tab-btn[data-tab="pipeline"]').get_attribute("class") or ""))

        # ---- Pipeline tab: same lineage DAG Command Center's Network view
        # shows for this fixture (test_network_branching.py pins these exact
        # numbers: TomoExcludeTilt/job004 -> ... -> job010 fanning out to 4,
        # one of those fanning out to 2 more), PLUS two extra, unconnected
        # roots -- job022 (Class2D) and job023 (Class3D), this app's own
        # fixture runs added by run_tests.sh's add_analyze_classification_run
        # for the classification tabs below. 9 branching jobs + 2 isolated
        # roots = 11 nodes, still 8 edges (neither fixture run has any),
        # still 5 rows (both sit alongside job004 in the existing root row,
        # adding no new depth).
        node_count = win.locator(".cc-network-node").count()
        check(f"All 11 jobs appear as Pipeline nodes ({node_count})", node_count == 11)
        edge_count = win.locator(".cc-network-edge").count()
        check(f"8 edges for the 9-job branching pipeline + 2 isolated roots ({edge_count})", edge_count == 8)
        rows = win.locator(".cc-network-row").all_inner_texts()
        check(f"5 rows deep ({len(rows)})", len(rows) == 5)
        check("job010's fan-out row has all 4 of its children",
              all(j in rows[3] for j in ("job011", "job013", "job014", "job015")))

        # ---- clicking a node opens the job-summary panel ----
        summary = win.locator('[data-role="an-summary"]')
        check("Job-summary panel starts hidden", not summary.is_visible())
        win.locator(".cc-network-node", has_text="job004").first.click()
        page.wait_for_timeout(500)
        check("Job-summary panel opens on node click", summary.is_visible())
        check("Job-summary panel names the clicked job", "job004" in summary.inner_text())
        # This fixture's jobs have no real job.star (run_tests.sh's
        # make_legacy_branchy_project only mkdir's the job directories) --
        # the honest response is "no recorded field values", not an error.
        check("No error surfaced for a job with no job.star to read",
              "Could not load" not in summary.inner_text())

        summary.locator('[data-role="an-summary-close"]').click()
        page.wait_for_timeout(200)
        check("Close button hides the summary panel again", not summary.is_visible())

        win.locator('.tab-btn[data-tab="micrographs"]').click()
        page.wait_for_timeout(200)
        check("Pipeline tab content is no longer the active one after switching tabs",
              "active" not in (win.locator('[data-tab-content="pipeline"]').get_attribute("class") or ""))

        # ---- Particles tab: path input (not a run picker -- both scatter
        # tabs share renderAnalyzeScatterTab, see its own docstring) + canvas
        # scatter, against run_tests.sh's real particles.star fixture
        # (Extract/job024) ----
        win.locator('.tab-btn[data-tab="particles"]').click()
        page.wait_for_timeout(300)
        particles_tab = win.locator('[data-tab-content="particles"]')
        particles_tab.locator('[data-role="an-scatter-path"]').fill("Extract/job024/particles.star")
        particles_tab.locator('[data-role="an-scatter-load"]').click()
        page.wait_for_timeout(700)
        p_status = particles_tab.locator('[data-role="an-scatter-status"]').inner_text()
        check(f"Particles tab loads the fixture STAR ({p_status!r})", "40 particles" in p_status)
        x_options = particles_tab.locator('[data-role="an-scatter-x"] option').all_inner_texts()
        check(f"Axis pickers exclude Name columns ({x_options})",
              "rlnMicrographName" not in x_options and "rlnImageName" not in x_options
              and "rlnCoordinateX" in x_options)
        check("Scatter canvas renders",
              particles_tab.locator('[data-role="an-scatter-canvas"]').count() == 1)
        canvas_box = particles_tab.locator('[data-role="an-scatter-canvas"]').bounding_box()
        check(f"Canvas has real drawn dimensions ({canvas_box})",
              canvas_box is not None and canvas_box["width"] > 100 and canvas_box["height"] > 100)

        # ---- rectangle-select + export (C4's write path) ----
        sel_count = particles_tab.locator('[data-role="an-scatter-selcount"]')
        export_selected_btn = particles_tab.locator('[data-role="an-scatter-export-selected"]')
        export_rest_btn = particles_tab.locator('[data-role="an-scatter-export-rest"]')
        check("No selection at first", sel_count.inner_text() == "No selection")
        check("Export buttons start disabled", export_selected_btn.is_disabled() and export_rest_btn.is_disabled())

        # Drag a rectangle covering the whole plot area -- with 40 fixture
        # particles spread across the full axis range (run_tests.sh's
        # add_analyze_classification_run), this should select all of them.
        cx, cy = canvas_box["x"], canvas_box["y"]
        page.mouse.move(cx + 5, cy + 5)
        page.mouse.down()
        page.mouse.move(cx + canvas_box["width"] - 5, cy + canvas_box["height"] - 5, steps=5)
        page.mouse.up()
        page.wait_for_timeout(300)
        sel_text = sel_count.inner_text()
        check(f"Dragging a rectangle over the whole plot selects particles ({sel_text!r})",
              "selected" in sel_text and "No selection" not in sel_text)
        check("Export selected becomes enabled once something is selected", not export_selected_btn.is_disabled())

        name_input = particles_tab.locator('[data-role="an-scatter-export-name"]')
        name_input.fill("browser_test_export.star")
        export_selected_btn.click()
        page.wait_for_timeout(500)
        export_status = particles_tab.locator('[data-role="an-scatter-export-status"]').inner_text()
        check(f"Export writes a real file and reports it ({export_status!r})",
              "Wrote" in export_status and "browser_test_export.star" in export_status)

        # A click with no real drag (same mousedown/up point) clears the
        # selection rather than selecting the nearest point -- rectangle
        # is the only selection gesture this widget has.
        page.mouse.click(cx + canvas_box["width"] / 2, cy + canvas_box["height"] / 2)
        page.wait_for_timeout(200)
        check("A plain click (no drag) clears the selection",
              sel_count.inner_text() == "No selection" and export_selected_btn.is_disabled())

        # Exporting to a name that already exists is refused, not silently
        # overwritten -- via page.request for the same response-listener
        # reason as the path-traversal check below.
        overwrite_resp = page.request.post(
            f"{BASE_URL}/api/analyze/export-star",
            data=json.dumps({
                "path": "Extract/job024/particles.star", "row_indices": [0, 1],
                "complement": False, "filename": "browser_test_export.star", "block": "particles",
            }),
            headers={"Content-Type": "application/json"},
        )
        check(f"Exporting over an existing file is refused (HTTP {overwrite_resp.status})",
              overwrite_resp.status == 400)

        # A destination filename with a path separator is refused too --
        # the write side's own guard, distinct from (and stricter than) the
        # read side's project-containment check above (see
        # analyze.export_star_subset's own docstring on why).
        filename_escape_resp = page.request.post(
            f"{BASE_URL}/api/analyze/export-star",
            data=json.dumps({
                "path": "Extract/job024/particles.star", "row_indices": [0],
                "complement": False, "filename": "../escape.star", "block": "particles",
            }),
            headers={"Content-Type": "application/json"},
        )
        check(f"A destination filename with a path separator is refused (HTTP {filename_escape_resp.status})",
              filename_escape_resp.status == 400)

        # A path outside the project is refused, not silently ignored or
        # crashed on -- via page.request (Playwright's own APIRequestContext,
        # not routed through the page's browsing context) rather than
        # page.evaluate(fetch(...)), so this deliberately-provoked 400
        # doesn't also trip the page.on("response") listener above and read
        # as a page error at the very end of this test.
        escape_resp = page.request.post(
            f"{BASE_URL}/api/analyze/particle-scatter",
            data=json.dumps({"path": "../../../../etc/passwd"}),
            headers={"Content-Type": "application/json"},
        )
        check(f"A path outside the project is refused, not read (HTTP {escape_resp.status})",
              escape_resp.status == 400)

        # ---- Micrographs tab: same shared scatter widget, against
        # run_tests.sh's CtfFind/job003 fixture -- its rlnMicrographName
        # values point back at MotionCorr/job002/, so the merged column
        # list should include MotionCorr's own motion-tracking fields on
        # top of CtfFind's own (add_analyze_classification_run) ----
        win.locator('.tab-btn[data-tab="micrographs"]').click()
        page.wait_for_timeout(300)
        mics_tab = win.locator('[data-tab-content="micrographs"]')
        mics_tab.locator('[data-role="an-scatter-path"]').fill("CtfFind/job003/micrographs_ctf.star")
        mics_tab.locator('[data-role="an-scatter-load"]').click()
        page.wait_for_timeout(700)
        m_status = mics_tab.locator('[data-role="an-scatter-status"]').inner_text()
        check(f"Micrographs tab loads the fixture STAR ({m_status!r})", "6 micrographs" in m_status)
        m_x_options = mics_tab.locator('[data-role="an-scatter-x"] option').all_inner_texts()
        check(f"Axis pickers exclude Name columns and include CtfFind's own columns ({m_x_options})",
              "rlnMicrographName" not in m_x_options and "rlnDefocusU" in m_x_options)
        check(f"Merged-in MotionCorr columns appear too ({m_x_options})",
              "rlnAccumMotionTotal" in m_x_options and "rlnAccumMotionEarly" in m_x_options)
        check("Micrographs scatter canvas renders",
              mics_tab.locator('[data-role="an-scatter-canvas"]').count() == 1)
        m_canvas_box = mics_tab.locator('[data-role="an-scatter-canvas"]').bounding_box()
        check(f"Micrographs canvas has real drawn dimensions ({m_canvas_box})",
              m_canvas_box is not None and m_canvas_box["width"] > 100 and m_canvas_box["height"] > 100)

        m_sel_count = mics_tab.locator('[data-role="an-scatter-selcount"]')
        m_export_selected_btn = mics_tab.locator('[data-role="an-scatter-export-selected"]')
        mcx, mcy = m_canvas_box["x"], m_canvas_box["y"]
        page.mouse.move(mcx + 5, mcy + 5)
        page.mouse.down()
        page.mouse.move(mcx + m_canvas_box["width"] - 5, mcy + m_canvas_box["height"] - 5, steps=5)
        page.mouse.up()
        page.wait_for_timeout(300)
        m_sel_text = m_sel_count.inner_text()
        check(f"Dragging a rectangle over the whole plot selects micrographs ({m_sel_text!r})",
              "selected" in m_sel_text and "No selection" not in m_sel_text)

        m_name_input = mics_tab.locator('[data-role="an-scatter-export-name"]')
        m_name_input.fill("browser_test_mic_export.star")
        m_export_selected_btn.click()
        page.wait_for_timeout(500)
        m_export_status = mics_tab.locator('[data-role="an-scatter-export-status"]').inner_text()
        check(f"Micrographs export writes a real file and reports it ({m_export_status!r})",
              "Wrote" in m_export_status and "browser_test_mic_export.star" in m_export_status)

        # export-star validates block against an allowlist -- neither
        # "particles" nor "micrographs" trusts a caller-supplied arbitrary
        # string for which STAR block to read/write.
        bad_block_resp = page.request.post(
            f"{BASE_URL}/api/analyze/export-star",
            data=json.dumps({
                "path": "CtfFind/job003/micrographs_ctf.star", "row_indices": [0],
                "complement": False, "filename": "bad_block.star", "block": "optics",
            }),
            headers={"Content-Type": "application/json"},
        )
        check(f"An unrecognized export block is refused (HTTP {bad_block_resp.status})",
              bad_block_resp.status == 400)

        # ---- 2D Classification tab: run picker + convergence + distribution
        # charts, against job022's 3 real iterations (add_analyze_classification_run) ----
        win.locator('.tab-btn[data-tab="class2d"]').click()
        page.wait_for_timeout(600)
        class2d = win.locator('[data-tab-content="class2d"]')
        options = class2d.locator('[data-role="an-run-select"] option').all_inner_texts()
        check(f"Run picker lists the Class2D fixture run ({options})",
              any("job022" in o for o in options))
        check("Convergence chart renders", class2d.locator('[data-role="an-convergence-chart"] svg').count() == 1)
        col_options = class2d.locator('[data-role="an-convergence-col"] option').all_inner_texts()
        check(f"Convergence column picker offers all three real columns ({col_options})",
              set(col_options) == {"Orientation changes", "Offset changes", "Particles that changed class"})
        check("Class-distribution chart renders",
              class2d.locator('[data-role="an-distribution-chart"] svg').count() == 1)
        band_count = class2d.locator('[data-role="an-distribution-chart"] path').count()
        check(f"Distribution chart draws one band per class (3 classes, {band_count} paths)", band_count == 3)

        # switching the convergence column redraws with a different chart title
        col_select = class2d.locator('[data-role="an-convergence-col"]')
        col_select.select_option(label="Particles that changed class")
        page.wait_for_timeout(300)
        # .text_content(), not .inner_text() -- Playwright's inner_text()
        # requires an HTMLElement and an SVG <title> isn't one.
        chart_title = class2d.locator('[data-role="an-convergence-chart"] svg title').text_content()
        check(f"Switching the convergence column redraws the chart ({chart_title!r})",
              "Particles that changed class" in chart_title)

        # ---- 3D Classification tab: same two charts as 2D, PLUS the FSC
        # chart and the on-demand viewing-direction heatmap (3D-only --
        # show3d) against job023's real model_class_N/data.star fixtures ----
        win.locator('.tab-btn[data-tab="class3d"]').click()
        page.wait_for_timeout(600)
        class3d = win.locator('[data-tab-content="class3d"]')
        c3d_options = class3d.locator('[data-role="an-run-select"] option').all_inner_texts()
        check(f"Run picker lists the Class3D fixture run ({c3d_options})",
              any("job023" in o for o in c3d_options))
        check("FSC chart renders", class3d.locator('[data-role="an-fsc-chart"] svg').count() == 1)
        fsc_paths = class3d.locator('[data-role="an-fsc-chart"] path').count()
        check(f"FSC chart draws one line per class (2 classes, {fsc_paths} paths)", fsc_paths == 2)
        metric_options = class3d.locator('[data-role="an-fsc-metric"] option').all_inner_texts()
        check(f"FSC metric picker offers FSC and SSNR ({metric_options})",
              metric_options == ["Gold-standard FSC", "SSNR"])

        metric_select = class3d.locator('[data-role="an-fsc-metric"]')
        metric_select.select_option(label="SSNR")
        page.wait_for_timeout(300)
        fsc_title = class3d.locator('[data-role="an-fsc-chart"] svg title').text_content()
        check(f"Switching the FSC metric redraws the chart ({fsc_title!r})", "SSNR" in fsc_title)

        check("Viewing-direction heatmap starts empty (on-demand only)",
              class3d.locator('[data-role="an-orient-body"] svg').count() == 0)
        class3d.locator('[data-role="an-orient-btn"]').click()
        page.wait_for_timeout(600)
        check("Generate button fetches and renders the heatmap",
              class3d.locator('[data-role="an-orient-body"] svg').count() == 1)
        orient_status = class3d.locator('[data-role="an-orient-status"]').inner_text()
        check(f"Status line reports the particle count ({orient_status!r})", "200 particles" in orient_status)

        # ---- a classification tab with no matching runs says so plainly ----
        win.locator('.tab-btn[data-tab="refine3d"]').click()
        page.wait_for_timeout(300)
        check("3D Refine tab reports no matching runs (this fixture has none)",
              "no autorefine runs" in win.locator('[data-tab-content="refine3d"]').inner_text().lower())

        # ---- reopening after closing rebuilds cleanly (no stale DOM refs) ----
        win.locator(".wb-close").first.click()
        page.wait_for_timeout(300)
        check("Popup closes", page.locator(".analyze-winbox").count() == 0)
        page.locator("#menuBtn").click()
        page.locator("#menuToolsBtn").click()
        page.locator("#menuAnalyzeBtn").click()
        page.wait_for_selector(".analyze-winbox", timeout=5000)
        page.wait_for_timeout(600)
        win2 = page.locator(".analyze-winbox")
        check("Reopened Pipeline tab still shows all 11 nodes",
              win2.locator(".cc-network-node").count() == 11)

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
