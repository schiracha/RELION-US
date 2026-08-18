"""
Playwright test for opening a project that was built in RELION's own GUI:
its jobs show in the Command Center, they're read-only, reopening one shows
the settings it actually ran with, and a new job continues the project's
numbering instead of landing on existing results.

Needs a live backend already pointed at the fixture project (run_tests.sh
builds one); pass the project directory as the second argument.

Usage: python3 test_legacy_project.py [base_url] [project_dir]
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def launch_browser(p):
    exe = os.environ.get("RELION_US_CHROMIUM")
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8433"
PROJECT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/legacy_relion_project")

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
        # The deliberate 409 below (asking the API to delete a job RELION owns)
        # also surfaces as a console error; both are expected.
        page.on("console", lambda m: errors.append(m.text)
                if m.type == "error" and "409" not in m.text else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        # The 409 below is deliberate -- the test asks the API to delete a job
        # RELION owns, to confirm it refuses.
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}")
                if r.status >= 400 and "favicon" not in r.url and r.status != 409 else None)

        page.goto(BASE_URL + "/", wait_until="networkidle")
        page.wait_for_selector(".job-item", timeout=5000)
        page.wait_for_timeout(600)

        # ---- the project's existing jobs are there ----
        rows = page.locator("#ccTableBody tr")
        check(f"RELION's own jobs fill the Command Center ({rows.count()} rows)",
              rows.count() == 5)
        names = rows.locator("td:first-child").all_inner_texts()
        check(f"Job numbers are RELION's, not renumbered ({names})",
              all(n in " ".join(names) for n in ("job001", "job005", "job011")))
        check("Imported rows are tagged as RELION's",
              page.locator(".cc-relion-tag").count() == 5)
        check("Types are resolved through RELION's sub-labels",
              "2D Classification" in " ".join(
                  rows.locator("td:nth-child(2)").all_inner_texts()))
        check("A failed job keeps its status",
              "failed" in " ".join(
                  rows.locator("td:nth-child(3)").all_inner_texts()).lower())
        check("No invented timestamps",
              all(t.strip() in ("", "—", "-") for t in
                  rows.locator("td:nth-child(4)").all_inner_texts()))

        # ---- network view: RELION's own edges, not a directory-path guess ----
        # The fixture chains Import -> MotionCorr -> CtfFind -> Class2D ->
        # Refine3D through pipeline_output_edges/pipeline_input_edges (see
        # run_tests.sh's make_legacy_project) -- this app never ran
        # _detect_inputs on any of these jobs, so this lineage can only have
        # come from reading RELION's own edge tables.
        page.locator('.cc-view-btn[data-view="network"]').click()
        page.wait_for_timeout(400)
        check("Network view becomes visible", page.locator("#ccNetworkView").is_visible())
        check("Table view hidden", not page.locator("#ccTableView").is_visible())
        node_count = page.locator(".cc-network-node").count()
        check(f"All 5 jobs appear as network nodes ({node_count})", node_count == 5)
        edge_count = page.locator(".cc-network-edge").count()
        check(f"4 edges for a 5-job chain ({edge_count})", edge_count == 4)
        rows_top_to_bottom = page.locator(".cc-network-row").all_inner_texts()
        check(f"job001 (the root) is in the top row ({rows_top_to_bottom[:1]})",
              "job001" in rows_top_to_bottom[0])
        check(f"job011 (the end of the chain) is in the bottom row ({rows_top_to_bottom[-1:]})",
              "job011" in rows_top_to_bottom[-1])
        page.locator('.cc-view-btn[data-view="table"]').click()
        page.wait_for_timeout(300)

        # ---- reopening one shows what it actually ran with ----
        page.locator("#ccTableBody tr", has_text="job005").first.click()
        page.wait_for_selector(".winbox", timeout=5000)
        win = page.locator(".winbox").last
        page.wait_for_timeout(1200)

        check("Reopened as the right job type",
              "2D Classification" in win.locator(".wb-title").inner_text()
              or win.locator('[data-role="job-name-display"]').inner_text() == "job005")
        check("Says plainly that RELION ran this one",
              win.locator(".job-relion-bar").count() == 1)

        def field(key):
            loc = win.locator(f'[data-field-key="{key}"] input, [data-field-key="{key}"] select')
            return loc.first.input_value() if loc.count() else None

        check(f"Input file is the one RELION used ({field('fn_img')})",
              field("fn_img") == "Select/job004/particles.star")
        check(f"Number of classes is RELION's, not the default ({field('nr_classes')})",
              field("nr_classes") == "50")
        win.locator(".opt-section-head", has_text="Running").click()
        page.wait_for_timeout(300)
        check(f"Running-tab values come across too (mpi={field('nr_mpi')}, j={field('nr_threads')})",
              field("nr_mpi") == "5" and field("nr_threads") == "8")

        # ---- and it is read-only ----
        for action, label in (("abort", "Abort"), ("overwrite", "Overwrite"),
                              ("delete", "Delete"), ("mark-finished", "Mark Finished")):
            check(f"{label} is hidden for a job RELION owns",
                  win.locator(f'[data-action="{action}"]').is_hidden())
        check("Outputs tab is still available (browsing is fine)",
              not win.locator('.tab-btn[data-tab="outputs"]').is_hidden())

        # An old classification's own per-iteration output is still on disk, so
        # the Progress tab works on it the same as on a job run here.
        prog_tab = win.locator('.tab-btn[data-tab="progress"]')
        if prog_tab.is_visible():
            prog_tab.click()
            page.wait_for_timeout(2500)
            check(f"Progress charts render for an old RELION classification "
                  f"({win.locator('.progress-chart').count()} charts, "
                  f"{win.locator('.thumb img').count()} class images)",
                  win.locator(".progress-chart").count() == 2
                  and win.locator(".thumb img").count() >= 1)
        else:
            check("Progress tab is offered for an imported Class2D", False)

        # the API refuses too, not just the UI
        refused = page.evaluate(
            """async () => {
                 const r = await fetch('/api/runs/' + encodeURIComponent('relion:job005'),
                                       {method: 'DELETE'});
                 return [r.status, (await r.json()).detail || ''];
               }"""
        )
        check(f"The API refuses to delete it as well ({refused[0]})",
              refused[0] == 409 and "RELION" in refused[1])

        # ---- a new job continues the numbering ----
        # Job popups are near window-filling and only one is open at once --
        # close the reopened job005 popup first so the sidebar underneath is
        # reachable again, same as a real user would have to.
        win.locator(".wb-close").first.click()
        page.wait_for_timeout(200)
        page.locator("#jobSearch").fill("3D classification")
        page.wait_for_timeout(300)
        page.locator(".job-item:visible", has_text="3D classification").first.click()
        page.wait_for_selector(".winbox", timeout=5000)
        page.wait_for_timeout(900)
        new_win = page.locator(".winbox").last
        cmd = new_win.locator(".command-box").input_value()
        check(f"New job continues RELION's numbering ({cmd[cmd.find('--o'):][:24]}…)",
              "Class3D/job012/" in cmd)
        check("...and that directory does not already exist",
              not (PROJECT_DIR / "Class3D" / "job012").exists())

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
