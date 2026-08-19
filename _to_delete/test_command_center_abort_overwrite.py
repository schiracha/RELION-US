"""
Playwright smoke test #2 for the Command Center: exercises Abort on a
genuinely long-running process (and confirms the process is actually
killed server-side, not just marked aborted in the UI), plus the
Overwrite flow (same run_id/job slot reused) and Mark Finished/Failed
overrides.

Run against a live backend serving an EMPTY project.

Usage: python3 test_command_center_abort_overwrite.py [base_url]
"""
import os
import subprocess
import sys

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

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8423"

errors = []
ok = True


def check(label, cond):
    global ok
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        ok = False


def pgrep_count(pattern):
    result = subprocess.run(["pgrep", "-fc", pattern], capture_output=True, text=True)
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


with sync_playwright() as p:
    browser = launch_browser(p)
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("response", lambda resp: errors.append(f"HTTP {resp.status} {resp.url}") if resp.status >= 400 else None)

    page.goto(BASE_URL + "/", wait_until="networkidle")
    page.wait_for_selector(".job-item", timeout=5000)

    # --- Abort a genuinely long-running process, verify server-side kill --
    marker = "relion_us_abort_test_marker_proc"
    page.locator(".job-item", has_text="Import Tomo Tilt Series").first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win = page.locator(".winbox").first
    win.locator(".command-box").fill(f"sleep 60 # {marker}")
    win.locator('[data-role="run-btn"]').click()
    page.wait_for_timeout(1500)
    check("Status line shows running before abort", "running" in win.locator('[data-role="status-line"]').inner_text().lower())
    check("Marker process is actually running server-side", pgrep_count(marker) >= 1)

    win.locator('[data-action="abort"]').click()
    page.wait_for_timeout(300)
    # Custom confirm dialog for abort
    confirm_btn = page.locator(".mini-dialog-actions button", has_text="Abort").or_(
        page.locator(".mini-dialog-actions button.danger")
    )
    check("Abort confirm dialog appeared", confirm_btn.count() >= 1)
    if confirm_btn.count():
        confirm_btn.first.click()
    page.wait_for_timeout(1000)
    check("Status line shows aborted", "abort" in win.locator('[data-role="status-line"]').inner_text().lower())
    check("Marker process actually killed server-side (process-group SIGTERM)", pgrep_count(marker) == 0)

    page.wait_for_timeout(300)
    check("Command Center row shows aborted status", "abort" in page.locator("#ccTableBody tr .cc-status-badge").first.inner_text().lower())

    win.locator('[data-action="close"]').click()
    page.wait_for_timeout(300)

    # --- Overwrite: same run_id / job slot reused ------------------------
    row = page.locator("#ccTableBody tr").first
    row.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win2 = page.locator(".winbox").first
    check("Reopened aborted job shows Overwrite button", win2.locator('[data-action="overwrite"]').is_visible())
    import re as _re
    _cmd = win2.locator(".command-box").input_value()
    _m = _re.search(r"--(?:o|output-directory)\s+(\S+)", _cmd)
    # The aborted job's command had no --o, so fall back to its known output
    # dir: this is the first job in a fresh project, so Import/job001, and
    # Overwrite reuses that same slot.
    _sub = (_m.group(1).rstrip("/") if _m else "Import/job001").strip("'\"")
    win2.locator(".command-box").fill(f"echo overwritten-content > {_sub}/overwrite_out.txt")

    win2.locator('[data-action="overwrite"]').click()
    page.wait_for_timeout(300)
    ow_confirm = page.locator(".mini-dialog-actions button", has_text="overwrite").or_(
        page.locator(".mini-dialog-actions button.danger")
    )
    check("Overwrite confirm dialog appeared", ow_confirm.count() >= 1)
    if ow_confirm.count():
        ow_confirm.first.click()
    page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)
    page.wait_for_timeout(500)

    check("Command Center still shows exactly 1 row after overwrite (same job slot)", page.locator("#ccTableBody tr").count() == 1)
    check("Row status is completed after overwrite", "completed" in page.locator("#ccTableBody tr .cc-status-badge").first.inner_text().lower())

    win3 = page.locator(".winbox").first
    outputs_tab = win3.locator('.tab-btn[data-tab="outputs"]')
    outputs_tab.click()
    page.wait_for_timeout(500)
    check("Outputs tab shows overwrite_out.txt", "overwrite_out.txt" in win3.locator('[data-tab-content="outputs"]').inner_text())

    win3.locator('[data-action="close"]').click()
    page.wait_for_timeout(300)

    # --- Mark Finished / Mark Failed manual overrides ---------------------
    # Mark Finished/Failed are (by design) hidden while a job is still
    # "running" -- they're for overriding a job's TERMINAL status (e.g. one
    # that exited nonzero due to a downstream tool quirk but actually
    # produced good output), not for short-circuiting a live run. So: start
    # a job, abort it (giving it a terminal status), then exercise the
    # Mark Failed override on top of that.
    page.locator(".job-item", has_text="Import Tomo Tilt Series").first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    win4 = page.locator(".winbox").first
    win4.locator(".command-box").fill(f"sleep 60 # {marker}2")
    win4.locator('[data-role="run-btn"]').click()
    page.wait_for_timeout(1000)

    win4.locator('[data-action="abort"]').click()
    page.wait_for_timeout(300)
    abort_confirm2 = page.locator(".mini-dialog-actions button", has_text="Abort").or_(
        page.locator(".mini-dialog-actions button.danger")
    )
    if abort_confirm2.count():
        abort_confirm2.first.click()
    page.wait_for_timeout(800)
    check("Mark Failed button visible after abort (terminal status)", win4.locator('[data-action="mark-failed"]').is_visible())

    win4.locator('[data-action="mark-failed"]').click()
    page.wait_for_timeout(300)
    mf_confirm = page.locator(".mini-dialog-actions button").first
    if mf_confirm.count():
        mf_confirm.click()
    page.wait_for_timeout(500)
    check("Status line reflects manually-marked failed", "fail" in win4.locator('[data-role="status-line"]').inner_text().lower())

    win4.locator('[data-action="close"]').click()
    page.wait_for_timeout(300)

    # cleanup any leftover marker processes this test spawned
    subprocess.run(["pkill", "-9", "-f", marker], capture_output=True)

    page.screenshot(path="/tmp/relion_us_cc_screenshot2.png", full_page=False)
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
