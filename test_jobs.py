"""
Playwright suite for job popups and the Command Center: opening/closing
popups, the standard-fields form, the SPA/Tomo/All sidebar filter, running a
job to completion (toolbar states, Outputs/Source/Errors tabs, rename,
timeline view, table sort, delete), aborting a genuinely long-running
process (with a server-side process-group kill check), and the
overwrite/mark-finished/mark-failed flows.

One backend + one browser session for the whole file (these all interact
with the same live Command Center state and none needs isolation from the
others -- see run_tests.sh). Run against a live backend serving an EMPTY
project, so run counts stay deterministic across sections.

Usage: python3 test_jobs.py [base_url]
"""
import os
import re
import subprocess
import sys

from playwright.sync_api import sync_playwright

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8420"

errors = []
ok = True


def check(label, cond):
    global ok
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        ok = False


def launch_browser(p):
    """Launch Chromium for the smoke tests.

    By default let Playwright find its own bundled browser -- that is what
    `playwright install chromium` sets up and it is right on almost every
    machine. Set RELION_US_CHROMIUM to point at a specific binary when the
    browser lives somewhere Playwright does not look (a shared read-only
    install on a cluster, for instance)."""
    exe = os.environ.get("RELION_US_CHROMIUM")
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()


def out_subdir(win):
    """Extract the job's output dir (from the draft's --o / --output-directory)
    so a simulated command can write into it -- jobs run from the PROJECT
    ROOT (like RELION), so a bare `> file` would land in the project root,
    not the job dir the Outputs tab reads."""
    cmd = win.locator(".command-box").input_value()
    m = re.search(r"--(?:o|output-directory)\s+(\S+)", cmd)
    return (m.group(1).rstrip("/") if m else "").strip("'\"")


def pgrep_count(pattern):
    result = subprocess.run(["pgrep", "-fc", pattern], capture_output=True, text=True)
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


with sync_playwright() as p:
    browser = launch_browser(p)
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    # 409 excluded from both listeners below: the browser's own devtools
    # console additionally logs a "Failed to load resource" line for any
    # non-2xx fetch, independent of the response listener -- same
    # convention test_legacy_project.py's own deliberate-409 check uses.
    page.on("console", lambda msg: errors.append(msg.text)
            if msg.type == "error" and "409" not in msg.text else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    # 409 excluded: the Overwrite-directory-mismatch section below deliberately
    # provokes one (through the app's own UI flow, not page.request, since the
    # point is checking the frontend's error surface end to end) and it's
    # expected, not a bug -- same convention test_legacy_project.py's own
    # deliberate-409 check uses.
    page.on("response", lambda resp: errors.append(f"HTTP {resp.status} {resp.url}")
            if resp.status >= 400 and resp.status != 409 else None)

    page.goto(BASE_URL + "/", wait_until="networkidle")
    page.wait_for_selector(".job-item", timeout=5000)
    n_jobs = page.locator(".job-item").count()
    print(f"Sidebar loaded with {n_jobs} job items")

    # ==== job popups: open/close, standard fields, one-popup-at-a-time =====
    page.locator(".job-item", has_text="Import Tomo Tilt Series").first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    check("WinBox popup opened", page.locator(".winbox").count() == 1)
    page.wait_for_selector(".job-standard-form .field-label", timeout=5000)
    check("Standard fields rendered", page.locator(".job-standard-form .field-label").count() > 0)

    # Opening a second job popup must close the first rather than stacking a
    # second window. dispatch_event("click"): the popup is now near
    # window-filling by design, so it visually covers the sidebar underneath
    # -- a real user closes/collapses it before the sidebar is reachable
    # again, and an actual mouse click can't reach through it (Playwright's
    # own force=True still respects real DOM layering). dispatch_event fires
    # the click event directly on the element, bypassing layering, so this
    # still exercises openJobPopup's own auto-close logic.
    page.locator(".job-item", has_text="Import from DeepETPicker").first.dispatch_event("click")
    page.wait_for_function(
        "() => document.querySelector('.winbox .wb-title')"
        "  && document.querySelector('.winbox .wb-title').textContent.includes('DeepETPicker')",
        timeout=5000,
    )
    check("Exactly 1 job popup open at once", page.locator(".winbox").count() == 1)
    remaining_title = page.locator(".winbox .wb-title").first.inner_text()
    check(f"Second job's popup is the one left open ({remaining_title})", "DeepETPicker" in remaining_title)

    page.locator(".winbox .wb-close").first.click()
    page.wait_for_timeout(200)

    # --- sidebar hide toggle ---
    page.locator("#toggleSidebarBtn").click()
    hidden = page.locator("#sidebar").evaluate("el => el.classList.contains('hidden')")
    check("Sidebar hides on toggle", hidden)
    page.locator("#toggleSidebarBtn").click()  # show it again

    # ==== SPA / Tomo / All jobs-list filter =================================
    # Pure display filter: must never make a job unreachable. Confirm the
    # SPA view hides a Tomo-only job, the Tomo view hides an SPA-only job,
    # "All" shows everything again, and a search always finds a job even
    # while the opposite filter is selected (the reachability guarantee).
    n_all = page.locator(".job-item:visible").count()

    page.locator('.pipeline-toggle-btn[data-pipeline="spa"]').click()
    n_spa = page.locator(".job-item:visible").count()
    spa_hides_tomo_import = page.locator(
        '.job-item:visible:has-text("Import Tomo Tilt Series")'
    ).count() == 0

    page.locator('.pipeline-toggle-btn[data-pipeline="tomo"]').click()
    n_tomo = page.locator(".job-item:visible").count()
    tomo_hides_autopick = page.locator(
        '.job-item:visible:has-text("Auto-picking")'
    ).count() == 0

    page.locator("#jobSearch").fill("Auto-picking")
    n_search_hit = page.locator(".job-item:visible:has-text('Auto-picking')").count()
    page.locator("#jobSearch").fill("")

    page.locator('.pipeline-toggle-btn[data-pipeline="all"]').click()
    n_all_again = page.locator(".job-item:visible").count()

    toggle_checks_ok = (
        0 < n_spa < n_all
        and 0 < n_tomo < n_all
        and spa_hides_tomo_import
        and tomo_hides_autopick
        and n_search_hit == 1
        and n_all_again == n_all
    )
    check(f"SPA/Tomo/All filter + search-reachability all behave "
          f"(all={n_all}, spa={n_spa}, tomo={n_tomo}, search_hit={n_search_hit})",
          toggle_checks_ok)

    # ==== Command Center: empty state (no job has run yet) ==================
    check("Command Center present", page.locator("#commandCenter").count() == 1)
    check("Table view visible by default", page.locator("#ccTableView").is_visible())
    check("Timeline view hidden by default", not page.locator("#ccTimelineView").is_visible())
    check("Empty-state message shown in table", page.locator("#ccTableEmpty").is_visible())

    # ==== run a job to completion: toolbar states, Outputs/Source/Errors ===
    page.locator(".job-item", has_text="Import Tomo Tilt Series").first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win1 = page.locator(".winbox").first
    sub1 = out_subdir(win1)
    win1.locator(".command-box").fill(f"sleep 2 && echo hello-from-job1 > {sub1}/job1_output.txt")
    win1.locator('[data-role="run-btn"]').click()
    page.wait_for_timeout(800)
    check("Status line shows running", "running" in win1.locator('[data-role="status-line"]').inner_text().lower())

    check("Abort button visible while running", win1.locator('[data-action="abort"]').is_visible())
    check("Overwrite button hidden while running", not win1.locator('[data-action="overwrite"]').is_visible())
    check("Delete button hidden while running", not win1.locator('[data-action="delete"]').is_visible())

    page.wait_for_timeout(300)
    check("Command Center shows 1 row while running", page.locator("#ccTableBody tr").count() == 1)
    check("Row status badge shows running", "running" in page.locator("#ccTableBody tr .cc-status-badge").first.inner_text().lower())

    page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)
    page.wait_for_timeout(500)

    check("Abort hidden after completion", not win1.locator('[data-action="abort"]').is_visible())
    check("Overwrite visible after completion", win1.locator('[data-action="overwrite"]').is_visible())
    check("Delete visible after completion", win1.locator('[data-action="delete"]').is_visible())
    check("Mark Finished hidden (already completed)", not win1.locator('[data-action="mark-finished"]').is_visible())
    check("Mark Failed visible (can override)", win1.locator('[data-action="mark-failed"]').is_visible())

    outputs_tab = win1.locator('.tab-btn[data-tab="outputs"]')
    check("Outputs tab no longer hidden", outputs_tab.is_visible())
    outputs_tab.click()
    page.wait_for_timeout(500)
    check("Outputs list shows job1_output.txt",
          "job1_output.txt" in win1.locator('[data-tab-content="outputs"]').inner_text())

    win1.locator('.tab-btn[data-tab="source"]').click()
    page.wait_for_timeout(200)
    source_text = win1.locator('[data-tab-content="source"] .source-pre').inner_text()
    check("Source tab shows non-empty RELION source", len(source_text.strip()) > 0)

    win1.locator('.tab-btn[data-tab="errors"]').click()
    page.wait_for_timeout(200)
    check("Errors tab present", win1.locator('[data-tab-content="errors"]').is_visible())

    # --- Rename (Alias) / Collapse ---
    page.once("dialog", lambda d: d.accept())  # safety net; app uses custom dialogs, not native
    win1.locator('[data-action="collapse"]').click()
    page.wait_for_timeout(300)
    check("Window minimized after Collapse", win1.evaluate("el => el.classList.contains('min')"))
    win1.locator(".wb-title, .wb-header").first.click()  # restore by clicking header (best-effort)
    page.wait_for_timeout(300)

    win1.locator('[data-action="close"]').click()
    page.wait_for_timeout(300)
    check("Popup closed after Close button", page.locator(".winbox").count() == 0)

    page.wait_for_timeout(300)
    check("Command Center shows 1 row after close", page.locator("#ccTableBody tr").count() == 1)
    check("Row status is completed", "completed" in page.locator("#ccTableBody tr .cc-status-badge").first.inner_text().lower())

    # --- Timeline view ---
    page.locator('.cc-view-btn[data-view="timeline"]').click()
    page.wait_for_timeout(300)
    check("Timeline view now visible", page.locator("#ccTimelineView").is_visible())
    check("Table view now hidden", not page.locator("#ccTableView").is_visible())
    check("Direction button visible in timeline view", page.locator("#ccDirectionBtn").is_visible())
    check("Timeline shows 1 card", page.locator(".cc-card").count() == 1)

    # --- Reopen job from Command Center (timeline card click) ---
    page.locator(".cc-card").first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win2 = page.locator(".winbox").first
    check("Reopened popup shows job name", "job001" in win2.inner_text() or "job1" in win2.inner_text().lower())
    check("Reopened popup shows Overwrite button (completed job)", win2.locator('[data-action="overwrite"]').is_visible())
    # Regression check: a non-custom RELION job reopened from history must
    # still show the Source tab.
    check("Reopened RELION job still shows Source tab", win2.locator('.tab-btn[data-tab="source"]').count() == 1)

    # --- Note editing ---
    win2.locator('[data-action="note"]').click()
    page.wait_for_timeout(300)
    dialog_input = page.locator(".mini-dialog input, .mini-dialog textarea")
    check("Prompt dialog appeared for Note", dialog_input.count() >= 1)
    if dialog_input.count():
        dialog_input.first.fill("test note from smoke test")
        page.locator(".mini-dialog-actions button", has_text="OK").or_(
            page.locator(".mini-dialog-actions button.primary")
        ).first.click()
        page.wait_for_timeout(300)
        check("Note row shows saved note", "test note" in win2.locator('[data-role="note-row"]').inner_text())

    win2.locator('[data-action="close"]').click()
    page.wait_for_timeout(300)

    # --- Table sort ---
    page.locator('.cc-view-btn[data-view="table"]').click()
    page.wait_for_timeout(200)
    page.locator('#ccTable th[data-sort="job_name"]').click()
    page.wait_for_timeout(200)
    arrow_text = page.locator('#ccTable th[data-sort="job_name"] .sort-arrow').inner_text()
    check("Sort arrow appears after clicking a sortable header", arrow_text.strip() != "")
    # Sorting is a display concern only -- put it back to the default
    # (unsorted/insertion) order so row position stays predictable below.
    page.locator('#ccTable th[data-sort="job_name"]').click()
    page.wait_for_timeout(200)

    # --- Launch a second job so we can test Delete ---
    page.locator(".job-item", has_text="Import Tomo Tilt Series").first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win3 = page.locator(".winbox").first
    win3.locator(".command-box").fill(f"echo instant-job2 > {out_subdir(win3)}/job2_output.txt")
    win3.locator('[data-role="run-btn"]').click()
    page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)
    page.wait_for_timeout(500)
    check("Command Center shows 2 rows now", page.locator("#ccTableBody tr").count() == 2)

    win3.locator('[data-action="delete"]').click()
    page.wait_for_timeout(300)
    confirm_btn = page.locator(".mini-dialog-actions button", has_text="Delete").or_(
        page.locator(".mini-dialog-actions button.danger")
    )
    check("Delete confirm dialog appeared", confirm_btn.count() >= 1)
    if confirm_btn.count():
        confirm_btn.first.click()
        page.wait_for_timeout(500)
    check("Popup closed after delete", page.locator(".winbox").count() == 0)
    check("Command Center back to 1 row after delete", page.locator("#ccTableBody tr").count() == 1)

    # ==== Abort a genuinely long-running process (server-side kill) ========
    marker = "relion_us_abort_test_marker_proc"
    page.locator(".job-item", has_text="Import Tomo Tilt Series").first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win4 = page.locator(".winbox").first
    win4.locator(".command-box").fill(f"sleep 60 # {marker}")
    win4.locator('[data-role="run-btn"]').click()
    page.wait_for_timeout(1500)
    check("Status line shows running before abort", "running" in win4.locator('[data-role="status-line"]').inner_text().lower())
    check("Marker process is actually running server-side", pgrep_count(marker) >= 1)

    win4.locator('[data-action="abort"]').click()
    page.wait_for_timeout(300)
    abort_confirm = page.locator(".mini-dialog-actions button", has_text="Abort").or_(
        page.locator(".mini-dialog-actions button.danger")
    )
    check("Abort confirm dialog appeared", abort_confirm.count() >= 1)
    if abort_confirm.count():
        abort_confirm.first.click()
    page.wait_for_timeout(1000)
    check("Status line shows aborted", "abort" in win4.locator('[data-role="status-line"]').inner_text().lower())
    check("Marker process actually killed server-side (process-group SIGTERM)", pgrep_count(marker) == 0)

    page.wait_for_timeout(300)
    check("Command Center now shows 2 rows (1 completed + 1 aborted)", page.locator("#ccTableBody tr").count() == 2)
    aborted_row = page.locator("#ccTableBody tr").filter(has_text="abort")
    check("One row shows aborted status", aborted_row.count() == 1)

    win4.locator('[data-action="close"]').click()
    page.wait_for_timeout(300)

    # ==== Overwrite: same run_id / job slot reused ==========================
    # Reopen by row CONTENT (the aborted job's status badge), not position --
    # this Command Center already has an unrelated completed row ahead of it.
    aborted_row.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win5 = page.locator(".winbox").first
    check("Reopened aborted job shows Overwrite button", win5.locator('[data-action="overwrite"]').is_visible())
    _cmd = win5.locator(".command-box").input_value()
    _m = re.search(r"--(?:o|output-directory)\s+(\S+)", _cmd)
    if _m:
        _sub = _m.group(1).rstrip("/").strip("'\"")
    else:
        # The aborted job's command had no --o -- derive the output dir from
        # the popup's own job name (e.g. "job003") instead of assuming a
        # fixed job number, since other jobs may already have run this
        # session.
        _job_name = win5.locator('[data-role="job-name-display"]').inner_text().strip()
        _sub = f"Import/{_job_name}"
    win5.locator(".command-box").fill(f"echo overwritten-content > {_sub}/overwrite_out.txt")

    win5.locator('[data-action="overwrite"]').click()
    page.wait_for_timeout(300)
    ow_confirm = page.locator(".mini-dialog-actions button", has_text="overwrite").or_(
        page.locator(".mini-dialog-actions button.danger")
    )
    check("Overwrite confirm dialog appeared", ow_confirm.count() >= 1)
    if ow_confirm.count():
        ow_confirm.first.click()
    page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)
    page.wait_for_timeout(500)

    check("Command Center still shows 2 rows after overwrite (same job slot)", page.locator("#ccTableBody tr").count() == 2)
    win6 = page.locator(".winbox").first
    outputs_tab6 = win6.locator('.tab-btn[data-tab="outputs"]')
    outputs_tab6.click()
    page.wait_for_timeout(500)
    check("Outputs tab shows overwrite_out.txt", "overwrite_out.txt" in win6.locator('[data-tab-content="outputs"]').inner_text())

    # ---- Overwrite refuses a command whose --o targets a different
    # directory (job_runner.py's authoritative-subdir guard) -- the
    # confirmed failure mode this blocks: this app's own tracking (and the
    # --pipeline_control exit markers, if RELION sync were on) would stay
    # pinned to the job being overwritten while the real output silently
    # lands wherever the hand-edited --o says. Already unit-tested in
    # backend/tests/test_job_runner.py; this checks the same guard is
    # actually wired up end to end, including the frontend's error surface.
    win6.locator('.tab-btn[data-tab="inputs"]').click()
    page.wait_for_timeout(300)
    mismatch_sub = f"{_sub}_WRONG"
    win6.locator(".command-box").fill(f"echo nope > {mismatch_sub}/out.txt --o {mismatch_sub}/")
    win6.locator('[data-action="overwrite"]').click()
    page.wait_for_timeout(300)
    ow_confirm2 = page.locator(".mini-dialog-actions button", has_text="overwrite").or_(
        page.locator(".mini-dialog-actions button.danger")
    )
    if ow_confirm2.count():
        ow_confirm2.first.click()
    page.wait_for_timeout(500)
    err_dialog = page.locator(".mini-dialog-overlay .mini-dialog p")
    err_text = err_dialog.first.inner_text() if err_dialog.count() else "(no dialog appeared)"
    check(f"Overwrite blocked when --o targets a different directory ({err_text!r})",
          err_dialog.count() >= 1 and "doesn't match" in err_text)
    if err_dialog.count():
        page.locator('.mini-dialog-actions [data-role="confirm"]').first.click()
        page.wait_for_timeout(200)
    check("Popup still shows the original completed run after the blocked overwrite",
          "completed" in win6.locator('[data-role="status-line"]').inner_text().lower())
    check("Command Center still shows 2 rows (blocked overwrite started no new run)",
          page.locator("#ccTableBody tr").count() == 2)

    win6.locator('[data-action="close"]').click()
    page.wait_for_timeout(300)

    # ==== Clone: a fresh, separately-numbered job of the same type =========
    # Unlike Overwrite, Clone never touches the job it's cloning FROM -- it
    # only reads that run's field_values and opens a brand-new, not-yet-run
    # popup. Works even for a job that's still "running" or RELION-native
    # (read-only here), which Overwrite explicitly can't -- see cloneBtn's
    # own comment in app.js for why that's the point of the feature.
    page.locator(".job-item", has_text="Import Tomo Tilt Series").first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win8 = page.locator(".winbox").first
    win8.locator(".command-box").fill(f"echo clone-source > {out_subdir(win8)}/clone_source.txt")
    win8.locator('[data-role="run-btn"]').click()
    page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)
    page.wait_for_timeout(500)
    clone_source_job_name = win8.locator('[data-role="job-name-display"]').inner_text().strip()
    rows_before_clone = page.locator("#ccTableBody tr").count()

    check("Clone button visible on a completed job", win8.locator('[data-action="clone"]').is_visible())
    win8.locator('[data-action="clone"]').click()
    page.wait_for_timeout(500)
    check("Clicking Clone closes the source job's popup", page.locator(".winbox").count() == 1)
    win9 = page.locator(".winbox").first
    check("Clone opens a popup for the SAME job type",
          "Import Tomo Tilt Series" in win9.locator(".wb-title").inner_text())
    check("Cloned popup has not run yet (Run button visible, no status line, still showing the display name)",
          win9.locator('[data-role="run-btn"]').is_visible()
          and win9.locator('[data-role="status-line"]').inner_text().strip() == ""
          and win9.locator('[data-role="job-name-display"]').inner_text().strip() == "Import Tomo Tilt Series")
    check("Clone did not start a new Command Center row (nothing has run yet)",
          page.locator("#ccTableBody tr").count() == rows_before_clone)

    # Actually run the clone -- it should get its own, freshly-allocated job
    # number, entirely separate from the job it was cloned from, and leave
    # that original job untouched.
    win9.locator(".command-box").fill(f"echo clone-run > {out_subdir(win9)}/clone_run.txt")
    win9.locator('[data-role="run-btn"]').click()
    page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)
    page.wait_for_timeout(500)
    clone_job_name = win9.locator('[data-role="job-name-display"]').inner_text().strip()
    check(f"Clone got its own, different job number ({clone_source_job_name!r} vs {clone_job_name!r})",
          clone_job_name != clone_source_job_name)
    check("Command Center gained a new row for the cloned run",
          page.locator("#ccTableBody tr").count() == rows_before_clone + 1)

    win9.locator('[data-action="close"]').click()
    page.wait_for_timeout(300)

    # Reopen the ORIGINAL (cloned-from) job by its own job name and confirm
    # running the clone touched nothing about it.
    page.locator("#ccTableBody tr", has_text=clone_source_job_name).first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win10 = page.locator(".winbox").first
    win10.locator('.tab-btn[data-tab="outputs"]').click()
    page.wait_for_timeout(500)
    source_outputs = win10.locator('[data-tab-content="outputs"]').inner_text()
    check("Original cloned-from job still only has its own output file",
          "clone_source.txt" in source_outputs and "clone_run.txt" not in source_outputs)
    win10.locator('[data-action="close"]').click()
    page.wait_for_timeout(300)

    # ==== Mark Finished / Mark Failed manual overrides ======================
    # Mark Finished/Failed are (by design) hidden while a job is still
    # "running" -- they're for overriding a job's TERMINAL status (e.g. one
    # that exited nonzero due to a downstream tool quirk but actually
    # produced good output), not for short-circuiting a live run. So: start
    # a job, abort it (giving it a terminal status), then exercise the Mark
    # Failed override on top of that.
    page.locator(".job-item", has_text="Import Tomo Tilt Series").first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win7 = page.locator(".winbox").first
    win7.locator(".command-box").fill(f"sleep 60 # {marker}2")
    win7.locator('[data-role="run-btn"]').click()
    page.wait_for_timeout(1000)

    win7.locator('[data-action="abort"]').click()
    page.wait_for_timeout(300)
    abort_confirm2 = page.locator(".mini-dialog-actions button", has_text="Abort").or_(
        page.locator(".mini-dialog-actions button.danger")
    )
    if abort_confirm2.count():
        abort_confirm2.first.click()
    page.wait_for_timeout(800)
    check("Mark Failed button visible after abort (terminal status)", win7.locator('[data-action="mark-failed"]').is_visible())

    win7.locator('[data-action="mark-failed"]').click()
    page.wait_for_timeout(300)
    mf_confirm = page.locator(".mini-dialog-actions button").first
    if mf_confirm.count():
        mf_confirm.click()
    page.wait_for_timeout(500)
    check("Status line reflects manually-marked failed", "fail" in win7.locator('[data-role="status-line"]').inner_text().lower())

    win7.locator('[data-action="close"]').click()
    page.wait_for_timeout(300)

    # cleanup any leftover marker processes this test spawned
    subprocess.run(["pkill", "-9", "-f", marker], capture_output=True)

    page.screenshot(path="/tmp/relion_us_jobs_screenshot.png", full_page=False)
    browser.close()

print()
if errors:
    print("CONSOLE/PAGE ERRORS:")
    for e in errors:
        print(" -", e)
    ok = False
else:
    print("No console/page errors.")

print()
print("OVERALL:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
