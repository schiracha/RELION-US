"""
Playwright smoke test for the base UI: the jobs list, opening a job popup,
its tabs and command box, and the SPA/Tomo/All sidebar toggle.

Usage: python3 test_frontend.py [base_url]
"""
import os
import sys
from playwright.sync_api import sync_playwright

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8420"


def launch_browser(p):
    """Launch Chromium for the smoke tests.

    By default let Playwright find its own bundled browser -- that is what
    `playwright install chromium` sets up and it is right on almost every
    machine. Set RELION_US_CHROMIUM to point at a specific binary when the
    browser lives somewhere Playwright does not look (a shared read-only
    install on a cluster, for instance)."""
    exe = os.environ.get("RELION_US_CHROMIUM")
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()

errors = []

with sync_playwright() as p:
    browser = launch_browser(p)
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("response", lambda resp: errors.append(f"HTTP {resp.status} {resp.url}") if resp.status >= 400 else None)

    page.goto(BASE_URL + "/", wait_until="networkidle")
    page.wait_for_selector(".job-item", timeout=5000)
    n_jobs = page.locator(".job-item").count()
    print(f"Sidebar loaded with {n_jobs} job items")

    # Open the TomoImport job (central to the user's tomography focus)
    page.locator(".job-item", has_text="Import Tomo Tilt Series").first.click()
    try:
        page.wait_for_selector(".winbox", timeout=5000)
    except Exception:
        print("!! WinBox popup did not appear. Console/page errors so far:")
        for e in errors:
            print("  -", e)
        browser.close()
        sys.exit(1)
    print("WinBox popup opened:", page.locator(".winbox").count(), "popup(s)")

    page.wait_for_selector(".job-standard-form .field-label", timeout=5000)
    n_standard = page.locator(".job-standard-form .field-label").count()
    print(f"Standard fields rendered: {n_standard}")

    page.locator('[data-role="recompute-btn"]').first.click()
    page.wait_for_timeout(500)
    cmd_value = page.locator(".command-box").first.input_value()
    print("Command box after recompute:", cmd_value[:120])

    # Open a second job (a custom import) to confirm only one job popup is
    # ever open at once -- opening this one must close the first rather than
    # stacking a second window. dispatch_event("click"): the popup is now
    # near window-filling by design (see app.js), so it visually covers the
    # sidebar underneath it -- a real user closes/collapses the open popup
    # before the sidebar is reachable again, an actual mouse click cannot
    # reach through it (Playwright's own force=True still respects real
    # DOM layering for a simulated mouse click, it only skips Playwright's
    # own pre-click checks). dispatch_event fires the click event directly
    # on the element, bypassing layering entirely, so this still exercises
    # openJobPopup's own auto-close logic -- the same handler a reachable
    # click would trigger -- even though the sidebar itself is intentionally
    # unreachable by mouse while a popup is open.
    second_job_item = page.locator(".job-item", has_text="Import from DeepETPicker").first
    second_job_item.dispatch_event("click")
    page.wait_for_function(
        "() => document.querySelector('.winbox .wb-title')"
        "  && document.querySelector('.winbox .wb-title').textContent.includes('DeepETPicker')",
        timeout=5000,
    )
    popup_count = page.locator(".winbox").count()
    print("Popup count after opening second job:", popup_count)
    if popup_count != 1:
        errors.append(f"Expected exactly 1 job popup open at once, found {popup_count}")
    remaining_title = page.locator(".winbox .wb-title").first.inner_text()
    print("Remaining popup's title:", remaining_title)
    if "DeepETPicker" not in remaining_title:
        errors.append(f"Expected the second job's popup to be the one left open, title was: {remaining_title}")

    # Close it so the sidebar is reachable again for the checks below.
    page.locator(".winbox .wb-close").first.click()
    page.wait_for_timeout(200)

    # Test the zoom slider
    page.locator("#zoomSlider").fill("130")
    page.locator("#zoomSlider").dispatch_event("input")
    page.wait_for_timeout(200)
    zoom_style = page.locator("#layout").evaluate("el => el.style.zoom")
    print("Zoom after setting slider to 130:", zoom_style)

    # Test sidebar hide toggle
    page.locator("#toggleSidebarBtn").click()
    hidden = page.locator("#sidebar").evaluate("el => el.classList.contains('hidden')")
    print("Sidebar hidden after toggle:", hidden)
    page.locator("#toggleSidebarBtn").click()  # show it again for the checks below

    # --- SPA / Tomo / All jobs-list toggle ---------------------------------
    # Pure display filter: must never make a job unreachable. Confirm the
    # SPA view hides a Tomo-only job, the Tomo view hides an SPA-only job,
    # "All" shows everything again, and a search always finds a job even
    # while the opposite filter is selected (the reachability guarantee).
    n_all = page.locator(".job-item:visible").count()
    print("Job items visible on 'All':", n_all)

    page.locator('.pipeline-toggle-btn[data-pipeline="spa"]').click()
    n_spa = page.locator(".job-item:visible").count()
    spa_hides_tomo_import = page.locator(
        '.job-item:visible:has-text("Import Tomo Tilt Series")'
    ).count() == 0
    print(f"Job items visible on 'SPA': {n_spa} (TomoImport hidden: {spa_hides_tomo_import})")

    page.locator('.pipeline-toggle-btn[data-pipeline="tomo"]').click()
    n_tomo = page.locator(".job-item:visible").count()
    tomo_hides_autopick = page.locator(
        '.job-item:visible:has-text("Auto-picking")'
    ).count() == 0
    print(f"Job items visible on 'Tomo': {n_tomo} (Autopick hidden: {tomo_hides_autopick})")

    # Reachability guarantee: still on the "Tomo" filter, searching for an
    # SPA-only job must surface it anyway -- nothing is ever truly hidden.
    page.locator("#jobSearch").fill("Auto-picking")
    n_search_hit = page.locator(".job-item:visible:has-text('Auto-picking')").count()
    print(f"'Auto-picking' found via search while on 'Tomo' filter: {n_search_hit == 1}")
    page.locator("#jobSearch").fill("")

    page.locator('.pipeline-toggle-btn[data-pipeline="all"]').click()
    n_all_again = page.locator(".job-item:visible").count()
    print("Job items visible after switching back to 'All':", n_all_again)

    toggle_checks_ok = (
        0 < n_spa < n_all
        and 0 < n_tomo < n_all
        and spa_hides_tomo_import
        and tomo_hides_autopick
        and n_search_hit == 1
        and n_all_again == n_all
    )
    print("SPA/Tomo/All toggle checks passed:", toggle_checks_ok)
    if not toggle_checks_ok:
        errors.append("SPA/Tomo/All toggle behaved unexpectedly -- see counts above")

    page.screenshot(path="/tmp/relion_us_screenshot.png", full_page=False)
    browser.close()

print()
if errors:
    print("CONSOLE/PAGE ERRORS:")
    for e in errors:
        print(" -", e)
    sys.exit(1)
else:
    print("No console/page errors.")
