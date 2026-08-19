"""
Playwright smoke test for the Command Center feature (job history table +
timeline views, unified job popup with actions toolbar, Outputs tab).

Run against a live backend serving an EMPTY project (see test invocation in
the session — a fresh /tmp project or the checked-in default
relion_project/ with .relion_us/history.json cleared first), so run counts
are deterministic.

Usage: python3 test_command_center.py [base_url]
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8421"

errors = []
ok = True


def check(label, cond):
    global ok
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        ok = False


import re as _re



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
    so a simulated command can write into it -- jobs now run from the PROJECT
    ROOT (like RELION), so a bare `> file` would land in the project root, not
    the job dir the Outputs tab reads."""
    cmd = win.locator(".command-box").input_value()
    m = _re.search(r"--(?:o|output-directory)\s+(\S+)", cmd)
    return (m.group(1).rstrip("/") if m else "").strip("'\"")


with sync_playwright() as p:
    browser = launch_browser(p)
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("response", lambda resp: errors.append(f"HTTP {resp.status} {resp.url}") if resp.status >= 400 else None)

    page.goto(BASE_URL + "/", wait_until="networkidle")
    page.wait_for_selector(".job-item", timeout=5000)

    # --- Command Center empty state -----------------------------------
    check("Command Center present", page.locator("#commandCenter").count() == 1)
    check("Table view visible by default", page.locator("#ccTableView").is_visible())
    check("Timeline view hidden by default", not page.locator("#ccTimelineView").is_visible())
    check("Empty-state message shown in table", page.locator("#ccTableEmpty").is_visible())

    # --- Launch a fast job (edit command box to something trivial) ----
    page.locator(".job-item", has_text="Import Tomo Tilt Series").first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win1 = page.locator(".winbox").first
    cmd_box = win1.locator(".command-box")
    sub1 = out_subdir(win1)
    cmd_box.fill(f"sleep 2 && echo hello-from-job1 > {sub1}/job1_output.txt")
    win1.locator('[data-role="run-btn"]').click()
    page.wait_for_timeout(800)
    check("Status line shows running", "running" in (win1.locator('[data-role="status-line"]').inner_text() or "").lower())

    # Toolbar during running: Abort visible, Overwrite/Delete hidden
    check("Abort button visible while running", win1.locator('[data-action="abort"]').is_visible())
    check("Overwrite button hidden while running", not win1.locator('[data-action="overwrite"]').is_visible())
    check("Delete button hidden while running", not win1.locator('[data-action="delete"]').is_visible())

    # Command Center should reflect the running job live
    page.wait_for_timeout(300)
    check("Command Center shows 1 row while running", page.locator("#ccTableBody tr").count() == 1)
    check("Row status badge shows running", "running" in (page.locator("#ccTableBody tr .cc-status-badge").first.inner_text() or "").lower())

    # Wait for completion
    page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)
    check("Status line shows completed", True)
    page.wait_for_timeout(500)

    # Toolbar after completion: Overwrite/Delete visible, Abort hidden
    check("Abort hidden after completion", not win1.locator('[data-action="abort"]').is_visible())
    check("Overwrite visible after completion", win1.locator('[data-action="overwrite"]').is_visible())
    check("Delete visible after completion", win1.locator('[data-action="delete"]').is_visible())
    check("Mark Finished hidden (already completed)", not win1.locator('[data-action="mark-finished"]').is_visible())
    check("Mark Failed visible (can override)", win1.locator('[data-action="mark-failed"]').is_visible())

    # --- Outputs tab ----------------------------------------------------
    outputs_tab = win1.locator('.tab-btn[data-tab="outputs"]')
    check("Outputs tab no longer hidden", outputs_tab.is_visible())
    outputs_tab.click()
    page.wait_for_timeout(500)
    outputs_content = win1.locator('[data-tab-content="outputs"]')
    check("Outputs list shows job1_output.txt", "job1_output.txt" in outputs_content.inner_text())

    # --- Errors / Source tabs still work --------------------------------
    win1.locator('.tab-btn[data-tab="source"]').click()
    page.wait_for_timeout(200)
    source_text = win1.locator('[data-tab-content="source"] .source-pre').inner_text()
    check("Source tab shows non-empty RELION source", len(source_text.strip()) > 0)

    win1.locator('.tab-btn[data-tab="errors"]').click()
    page.wait_for_timeout(200)
    check("Errors tab present", win1.locator('[data-tab-content="errors"]').is_visible())

    # --- Rename (Alias) ---------------------------------------------------
    page.once("dialog", lambda d: d.accept())  # safety net; app uses custom dialogs, not native
    win1.locator('[data-action="collapse"]').click()  # exercise Collapse
    page.wait_for_timeout(300)
    check("Window minimized after Collapse", win1.evaluate("el => el.classList.contains('min')"))
    win1.locator(".wb-title, .wb-header").first.click()  # restore by clicking header (best-effort)
    page.wait_for_timeout(300)

    win1.locator('[data-action="close"]').click()
    page.wait_for_timeout(300)
    check("Popup closed after Close button", page.locator(".winbox").count() == 0)

    # --- Command Center after completion: table + timeline -------------
    page.wait_for_timeout(300)
    check("Command Center shows 1 row after close", page.locator("#ccTableBody tr").count() == 1)
    row_status = page.locator("#ccTableBody tr .cc-status-badge").first.inner_text()
    check("Row status is completed", "completed" in row_status.lower())

    # Toggle to timeline view
    page.locator('.cc-view-btn[data-view="timeline"]').click()
    page.wait_for_timeout(300)
    check("Timeline view now visible", page.locator("#ccTimelineView").is_visible())
    check("Table view now hidden", not page.locator("#ccTableView").is_visible())
    check("Direction button visible in timeline view", page.locator("#ccDirectionBtn").is_visible())
    check("Timeline shows 1 card", page.locator(".cc-card").count() == 1)

    # Direction toggle
    dir_btn_text_before = page.locator("#ccDirectionBtn").inner_text()
    page.locator("#ccDirectionBtn").click()
    page.wait_for_timeout(200)
    dir_btn_text_after = page.locator("#ccDirectionBtn").inner_text()
    check("Direction button label changes on toggle", dir_btn_text_before != dir_btn_text_after)

    # --- Reopen job from Command Center (timeline card click) -----------
    page.locator(".cc-card").first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win2 = page.locator(".winbox").first
    check("Reopened popup shows job name", "job001" in win2.inner_text() or "job1" in win2.inner_text().lower())
    check("Reopened popup shows Overwrite button (completed job)", win2.locator('[data-action="overwrite"]').is_visible())
    # This is the key regression check for the isCustom bug fix: a non-custom
    # RELION job reopened from history must still show the Source tab.
    check("Reopened RELION job still shows Source tab", win2.locator('.tab-btn[data-tab="source"]').count() == 1)

    # --- Note editing -----------------------------------------------------
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

    # --- Table sort ---------------------------------------------------
    page.locator('.cc-view-btn[data-view="table"]').click()
    page.wait_for_timeout(200)
    page.locator('#ccTable th[data-sort="job_name"]').click()
    page.wait_for_timeout(200)
    arrow_text = page.locator('#ccTable th[data-sort="job_name"] .sort-arrow').inner_text()
    check("Sort arrow appears after clicking a sortable header", arrow_text.strip() != "")

    # --- Launch a second job so we can test Delete -----------------------
    page.locator(".job-item", has_text="Import Tomo Tilt Series").first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win3 = page.locator(".winbox").first
    win3.locator(".command-box").fill(f"echo instant-job2 > {out_subdir(win3)}/job2_output.txt")
    win3.locator('[data-role="run-btn"]').click()
    page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)
    page.wait_for_timeout(500)
    check("Command Center shows 2 rows now", page.locator("#ccTableBody tr").count() == 2)

    # Delete it (with remove_files)
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

    page.screenshot(path="/tmp/relion_us_cc_screenshot.png", full_page=False)
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
