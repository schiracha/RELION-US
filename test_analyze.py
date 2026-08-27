"""
Playwright test for the Analyze popup (Menu > Tools > Analyze) -- a read-only,
not-a-job window inspired by CNIO_Relion_Tools' relion_analyse.py (see
NOTICE.md). Pipeline (C1) and 2D/3D Classification + 3D Refine (C2) are wired;
Micrographs/Particles (C4) are still a placeholder. This suite covers: the
popup shell (all six tabs present), the Pipeline tab reusing Command Center's
own lineage-graph renderer plus the click-for-job-summary panel, and the 2D
Classification tab's run picker + convergence/class-distribution charts.

Needs a live backend already pointed at run_tests.sh's branching fixture
project (same one test_network_branching.py uses -- make_legacy_branchy_project)
plus one real Class2D run with iteration files
(add_analyze_classification_run), so both the Pipeline tab's node/edge shape
and the 2D Classification tab's charts have known-correct data to check
against.

Usage: python3 test_analyze.py [base_url] [project_dir]
"""
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
        # one of those fanning out to 2 more), PLUS one extra, unconnected
        # root -- job022, this app's own Class2D fixture run added by
        # run_tests.sh's add_analyze_classification_run for the 2D
        # Classification tab below. 9 branching jobs + 1 isolated root = 10
        # nodes, still 8 edges (job022 has none), still 5 rows (job022 sits
        # alongside job004 in the existing root row, adding no new depth).
        node_count = win.locator(".cc-network-node").count()
        check(f"All 10 jobs appear as Pipeline nodes ({node_count})", node_count == 10)
        edge_count = win.locator(".cc-network-edge").count()
        check(f"8 edges for the 9-job branching pipeline + 1 isolated root ({edge_count})", edge_count == 8)
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

        # ---- unimplemented tabs say so plainly rather than showing nothing ----
        win.locator('.tab-btn[data-tab="micrographs"]').click()
        page.wait_for_timeout(200)
        check("Micrographs tab shows a coming-soon placeholder",
              "coming soon" in win.locator('[data-tab-content="micrographs"]').inner_text().lower())
        check("Pipeline tab content is no longer the active one",
              "active" not in (win.locator('[data-tab-content="pipeline"]').get_attribute("class") or ""))

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

        # ---- a classification tab with no matching runs says so plainly ----
        win.locator('.tab-btn[data-tab="class3d"]').click()
        page.wait_for_timeout(300)
        check("3D Classification tab reports no matching runs (this fixture has none)",
              "no class3d runs" in win.locator('[data-tab-content="class3d"]').inner_text().lower())

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
        check("Reopened Pipeline tab still shows all 10 nodes",
              win2.locator(".cc-network-node").count() == 10)

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
