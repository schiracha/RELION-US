import sys
from playwright.sync_api import sync_playwright

errors = []

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    # Chromium logs a generic "Failed to load resource: 404" console error for
    # the browser's own implicit favicon.ico request; this is benign (present
    # even in the original test_frontend.py smoke test) and unrelated to any
    # app functionality, so it's filtered out here rather than chased.
    page.on("console", lambda msg: errors.append(msg.text)
            if msg.type == "error" and "Failed to load resource" not in msg.text else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("response", lambda resp: errors.append(f"HTTP {resp.status} {resp.url}")
            if resp.status >= 400 and "favicon.ico" not in resp.url else None)

    page.goto("http://localhost:8420/", wait_until="networkidle")

    # Project label should reflect the current (cwd-detected) project.
    page.wait_for_timeout(500)
    label = page.locator("#projectDirLabel").inner_text()
    print("Initial project label:", label)
    assert "existing_project" in label, f"expected existing_project in label, got {label!r}"

    # Open Change Project modal
    page.locator("#changeProjectBtn").click()
    page.wait_for_selector("#projectModalOverlay:not(.hidden)", timeout=3000)
    print("Change Project modal opened")

    # Browse into /tmp/relion_test_projects (parent of both test dirs)
    page.locator("#projectPathInput").fill("/tmp/relion_test_projects")
    page.locator("#projectPathGoBtn").click()
    page.wait_for_timeout(500)
    entries = page.locator(".browser-entry").all_inner_texts()
    print("Browser entries at /tmp/relion_test_projects:", entries)
    assert any("plain_folder" in e for e in entries)
    assert any("existing_project" in e for e in entries)

    # Click into plain_folder (already init'd as a project by curl test earlier)
    page.locator(".browser-entry", has_text="plain_folder").first.click()
    page.wait_for_timeout(500)
    badge = page.locator(".project-badge.ok")
    print("plain_folder recognized as project:", badge.count() > 0)

    # Switch to it
    page.locator("#projectSwitchBtn").click()
    page.wait_for_timeout(500)
    label2 = page.locator("#projectDirLabel").inner_text()
    print("Project label after switch:", label2)
    assert "plain_folder" in label2

    # Running jobs bar should now show the history entry created earlier via curl
    page.wait_for_selector(".run-chip", timeout=3000)
    chip_text = page.locator(".run-chip").first.inner_text()
    print("History chip found:", chip_text)

    # Click the chip to reopen that run's popup
    page.locator(".run-chip").first.click()
    page.wait_for_selector(".winbox", timeout=3000)
    print("Run history popup opened:", page.locator(".winbox").count())
    page.wait_for_timeout(500)
    output_text = page.locator(".live-output").first.inner_text()
    print("Live output pane contents:", output_text[:200])

    # Now test the "not a relion project" path against a brand-new folder name
    page.evaluate("document.querySelectorAll('.winbox').forEach(w => w.remove())")
    page.wait_for_timeout(200)
    page.locator("#changeProjectBtn").click(force=True)
    page.wait_for_selector("#projectModalOverlay:not(.hidden)", timeout=3000)
    page.wait_for_selector("#projectModalOverlay:not(.hidden)", timeout=3000)
    page.locator("#projectPathInput").fill("/tmp/relion_test_projects/brand_new_unseen_folder")
    page.wait_for_timeout(200)
    page.screenshot(path="/tmp/debug_before_switch.png")
    page.locator("#projectSwitchBtn").click(force=True)
    page.wait_for_timeout(500)
    page.screenshot(path="/tmp/debug_after_switch.png")
    print("Modal overlay hidden?", page.locator("#projectModalOverlay").get_attribute("class"))
    print("NotAProject overlay class:", page.locator("#notAProjectOverlay").get_attribute("class"))
    page.wait_for_selector("#notAProjectOverlay:not(.hidden)", timeout=3000)
    prompt_text = page.locator("#notAProjectOverlay .modal p").inner_text()
    print("Not-a-project prompt text:", prompt_text)
    assert "doesn't look like a Relion Project" in prompt_text

    page.locator("#startNewProjectBtn").click()
    page.wait_for_timeout(500)
    label3 = page.locator("#projectDirLabel").inner_text()
    print("Project label after starting new project:", label3)
    assert "brand_new_unseen_folder" in label3

    # --- Create Folder button in the Change Project browser ---
    page.locator("#changeProjectBtn").click(force=True)
    page.wait_for_selector("#projectModalOverlay:not(.hidden)", timeout=3000)
    page.locator("#projectPathInput").fill("/tmp/relion_test_projects")
    page.locator("#projectPathGoBtn").click()
    page.wait_for_timeout(400)

    page.locator("#newFolderNameInput").fill("CreatedFromUI")
    page.locator("#createFolderBtn").click()
    page.wait_for_timeout(500)
    entries_after_create = page.locator(".browser-entry").all_inner_texts()
    print("Browser entries after Create Folder:", entries_after_create)
    new_path_value = page.locator("#projectPathInput").input_value()
    print("Path input after Create Folder:", new_path_value)
    assert new_path_value.endswith("CreatedFromUI"), f"expected to navigate into the new folder, got {new_path_value!r}"
    assert page.locator("#projectModalError").get_attribute("class") == "modal-error hidden", \
        "no error banner expected on a successful Create Folder"

    import os
    assert os.path.isdir("/tmp/relion_test_projects/CreatedFromUI"), \
        "Create Folder button did not actually create the directory on disk"

    # Empty name should show an inline error, not a silent no-op or a native alert
    page.locator("#newFolderNameInput").fill("")
    page.locator("#createFolderBtn").click()
    page.wait_for_timeout(300)
    err_class = page.locator("#projectModalError").get_attribute("class")
    err_text = page.locator("#projectModalError").inner_text()
    print("Error banner after empty-name Create Folder click:", err_class, "-", err_text)
    assert "hidden" not in err_class
    assert err_text

    # Trigger the OSError branch for real: an intermediate path component
    # that's a file, not a directory (verified against the live endpoint via
    # curl already; this confirms the frontend surfaces main.py's returned
    # `message` in the error banner rather than swallowing it).
    with open("/tmp/relion_test_projects/CreatedFromUI/blocking_file", "w") as f:
        f.write("x")
    page.locator("#projectPathInput").fill("/tmp/relion_test_projects/CreatedFromUI")
    page.locator("#projectPathGoBtn").click()
    page.wait_for_timeout(300)
    page.locator("#newFolderNameInput").fill("blocking_file/nested")
    page.locator("#createFolderBtn").click()
    page.wait_for_timeout(500)
    err_text2 = page.locator("#projectModalError").inner_text()
    print("Error banner after OSError-triggering Create Folder:", err_text2)
    assert "Could not create folder" in err_text2

    page.screenshot(path="/tmp/relion_us_project_screenshot.png", full_page=False)
    browser.close()

print()
if errors:
    print("Console/page errors observed:")
    for e in errors:
        print(" -", e)
    sys.exit(1)
else:
    print("No console/page errors. Change Project flow OK.")
