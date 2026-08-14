import sys
from playwright.sync_api import sync_playwright

errors = []

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("response", lambda resp: errors.append(f"HTTP {resp.status} {resp.url}") if resp.status >= 400 else None)

    page.goto("http://localhost:8420/", wait_until="networkidle")
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

    # Open a second job (a custom import) to confirm multiple popups work
    page.locator(".job-item", has_text="Import from DeepETPicker").first.click()
    page.wait_for_timeout(500)
    print("Popup count after opening second job:", page.locator(".winbox").count())

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

    page.screenshot(path="/tmp/relion_job_manager_screenshot.png", full_page=False)
    browser.close()

print()
if errors:
    print("CONSOLE/PAGE ERRORS:")
    for e in errors:
        print(" -", e)
    sys.exit(1)
else:
    print("No console/page errors.")
