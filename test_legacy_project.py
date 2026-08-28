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
        # Only Class2D/job005 has a job.star (make_legacy_project writes one
        # for it alone, to give the Progress tab something real to plot) --
        # project_manager.estimate_job_timestamps reads that file's own
        # mtime as a best-effort start-time signal for a RELION-native job
        # with no recorded timing, and app.js's formatTimestamp marks it
        # with a leading "~" so it never reads as a recorded fact. The other
        # four jobs have no marker files at all, so they stay blank. Neither
        # is an "invented" (fabricated, unmarked) timestamp -- that's what
        # this check actually guards against.
        ts_texts = rows.locator("td:nth-child(4)").all_inner_texts()
        non_blank = [t for t in ts_texts if t.strip() not in ("", "—", "-")]
        check(f"No invented (unmarked, fabricated) timestamps ({ts_texts})",
              len(non_blank) == 1 and non_blank[0].strip().startswith("~"))

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

        # Every edge's endpoints should land exactly on a node's bottom-center
        # (its start) and another node's top-center (its end) -- not just
        # "close", since app.js reads these back from the laid-out DOM rather
        # than computing them by hand (see renderNetwork's comment).
        #
        # This deliberately re-derives both sides in real page pixels via
        # getBoundingClientRect() rather than comparing offsetTop/Left (as
        # app.js itself does) against the path's raw `d` coordinates. Those
        # two are the same numbers by construction -- app.js computes one
        # from the other -- so that comparison can never catch app.js
        # computing the *wrong* numbers in the first place. It didn't: the
        # overlay SVG is a sibling of #ccNetworkRows, both children of
        # #ccNetworkCanvas, and #ccNetworkCanvas used to carry the padding
        # that visually insets the whole view. An absolutely positioned
        # child's inset:0 is measured from its containing block's *padding
        # edge*, which ignores that block's own padding -- so the SVG (sized
        # to the canvas) rendered padding-px above and left of where
        # #ccNetworkRows (a normal-flow child, which the padding does push
        # in) actually sat on screen. Every edge landed that far short of the
        # node it was meant to touch, on every project, not just wide or
        # tall ones -- exactly "the lines never quite reach the top of the
        # following job". Fixed by moving the padding to #ccNetworkView (the
        # scrolling viewport) instead, so #ccNetworkCanvas carries none and
        # the SVG's inset:0 lines up with #ccNetworkRows's top-left again.
        def edges_touch_nodes():
            return page.evaluate("""() => {
                const nodes = Array.from(document.querySelectorAll('.cc-network-node')).map((n) => {
                    const r = n.getBoundingClientRect();
                    return { cx: r.left + r.width / 2, top: r.top, bottom: r.bottom };
                });
                const svgRect = document.getElementById('ccNetworkEdges').getBoundingClientRect();
                const near = (a, b) => Math.abs(a - b) < 0.5;
                return Array.from(document.querySelectorAll('.cc-network-edge')).every((path) => {
                    const parts = path.getAttribute('d').replace(/,/g, ' ').split(/\\s+/).filter(Boolean);
                    const x1 = svgRect.left + parseFloat(parts[1]);
                    const y1 = svgRect.top + parseFloat(parts[2]);
                    const x2 = svgRect.left + parseFloat(parts[parts.length - 2]);
                    const y2 = svgRect.top + parseFloat(parts[parts.length - 1]);
                    const startsAtABottom = nodes.some((n) => near(n.cx, x1) && near(n.bottom, y1));
                    const endsAtATop = nodes.some((n) => near(n.cx, x2) && near(n.top, y2));
                    return startsAtABottom && endsAtATop;
                });
            }""")
        check("Every edge touches a node's bottom at one end and a node's top at the other, "
              "in real screen pixels",
              edges_touch_nodes())

        # Edges are read back from the DOM, so anything that moves the boxes
        # without changing the data (closing/reopening the Jobs sidebar) has
        # to trigger a recompute -- see app.js's ensureNetworkResizeObserver
        # and the toggleSidebarBtn listener -- or the lines stay drawn at
        # their old coordinates while the boxes move out from under them.
        first_node_x_before = page.locator(".cc-network-node").first.evaluate("el => el.offsetLeft")
        page.locator("#toggleSidebarBtn").click()
        page.wait_for_timeout(400)  # .15s CSS transition on #sidebar + a margin
        first_node_x_after = page.locator(".cc-network-node").first.evaluate("el => el.offsetLeft")
        check(f"Closing the sidebar actually moves the network's boxes "
              f"({first_node_x_before} -> {first_node_x_after})",
              first_node_x_before != first_node_x_after)
        check("...and the edges follow them there",
              edges_touch_nodes())
        page.locator("#toggleSidebarBtn").click()  # reopen, for the rest of the test
        page.wait_for_timeout(400)
        check("Edges still touch after reopening the sidebar too",
              edges_touch_nodes())

        # ---- flipping newest/oldest, same control as the Timeline view ----
        dir_btn = page.locator("#ccDirectionBtn")
        check("Direction button is available in Network view too", dir_btn.is_visible())
        check(f"Network defaults to oldest-first ({dir_btn.inner_text()!r})",
              "Oldest" in dir_btn.inner_text())
        dir_btn.click()
        page.wait_for_timeout(300)
        check(f"Clicking it switches to newest-first ({dir_btn.inner_text()!r})",
              "Newest" in dir_btn.inner_text())
        rows_flipped = page.locator(".cc-network-row").all_inner_texts()
        check(f"job011 (newest) is now in the top row ({rows_flipped[:1]})",
              "job011" in rows_flipped[0])
        check(f"job001 (oldest) is now in the bottom row ({rows_flipped[-1:]})",
              "job001" in rows_flipped[-1])
        check("Edges still touch after flipping direction (parent/child, not top/bottom)",
              edges_touch_nodes())
        dir_btn.click()  # back to the default for the rest of the test
        page.wait_for_timeout(300)

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

        # ---- ...except alias/note, which stay editable even for a job RELION
        # owns -- both are a purely local overlay here, never written into
        # RELION's own files, so they can't leave its record describing
        # something untrue the way abort/overwrite/delete could. ----
        name_title = win.locator('[data-role="job-name-display"]').get_attribute("title") or ""
        check(f"Rename is NOT blocked for a job RELION owns ({name_title!r})",
              "Click to rename" in name_title)
        win.locator('[data-role="job-name-display"]').click()
        page.wait_for_timeout(300)
        rename_input = page.locator(".mini-dialog input, .mini-dialog textarea")
        check("Rename prompt opens for a job RELION owns", rename_input.count() >= 1)
        if rename_input.count():
            rename_input.first.fill("renamed_from_relion_us")
            page.locator(".mini-dialog-actions button", has_text="OK").or_(
                page.locator(".mini-dialog-actions button.primary")
            ).first.click()
            page.wait_for_timeout(500)
            check("Job name display now shows the new alias",
                  win.locator('[data-role="job-name-display"]').inner_text().strip()
                  == "renamed_from_relion_us")
            check("Command Center reflects the rename too",
                  "renamed_from_relion_us" in page.locator("#ccTableBody").inner_text())

        note_btn = win.locator('[data-action="note"]')
        check("Note button is NOT hidden for a job RELION owns", not note_btn.is_hidden())
        note_btn.click()
        page.wait_for_timeout(300)
        note_input = page.locator(".mini-dialog input, .mini-dialog textarea")
        check("Note prompt opens for a job RELION owns", note_input.count() >= 1)
        if note_input.count():
            note_input.first.fill("a note from RELION-US")
            page.locator(".mini-dialog-actions button", has_text="OK").or_(
                page.locator(".mini-dialog-actions button.primary")
            ).first.click()
            page.wait_for_timeout(500)
            check("Note row shows the saved note",
                  "a note from RELION-US" in win.locator('[data-role="note-row"]').inner_text())

        # Reopening from a fresh Command Center refresh must still show
        # both -- confirms they actually persisted (project_manager.
        # set_relion_overlay), not just updated in-memory for this popup.
        win.locator('[data-action="close"]').click()
        page.wait_for_timeout(300)
        page.locator("#ccTableBody tr", has_text="renamed_from_relion_us").first.click()
        page.wait_for_selector(".winbox", timeout=5000)
        win = page.locator(".winbox").last   # reassigned: the rest of this test keeps using `win`
        page.wait_for_timeout(500)
        check("Rename survived reopening the job",
              win.locator('[data-role="job-name-display"]').inner_text().strip()
              == "renamed_from_relion_us")
        check("Note survived reopening the job too",
              "a note from RELION-US" in win.locator('[data-role="note-row"]').inner_text())

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
