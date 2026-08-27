"""
Playwright test for the Analyze popup (Menu > Tools > Analyze) -- a read-only,
not-a-job window inspired by CNIO_Relion_Tools' relion_analyse.py (see
NOTICE.md). Only the Pipeline tab is wired so far (issue-tracked sub-phases
C2-C4 add the rest); this suite covers what exists: the popup shell (all six
tabs present), the Pipeline tab reusing Command Center's own lineage-graph
renderer, and the click-for-job-summary panel.

Needs a live backend already pointed at a branching fixture project (same one
test_network_branching.py uses -- run_tests.sh's make_legacy_branchy_project),
so the Pipeline tab's node/edge counts have a known-correct shape to check
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
        # one of those fanning out to 2 more) ----
        node_count = win.locator(".cc-network-node").count()
        check(f"All 9 jobs appear as Pipeline nodes ({node_count})", node_count == 9)
        edge_count = win.locator(".cc-network-edge").count()
        check(f"8 edges for the 9-job branching pipeline ({edge_count})", edge_count == 8)
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
        check("Reopened Pipeline tab still shows all 9 nodes",
              win2.locator(".cc-network-node").count() == 9)

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
