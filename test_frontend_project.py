"""
Playwright test for the Change Project dialog: browsing, switching, the
"not a RELION project" prompt, Create Folder (including its two error paths),
and the recent-projects list filling in as you switch around.

Builds its own fixture tree under /tmp/relion_test_projects, so it needs only
a live backend — no particular starting project.

Usage: python3 test_frontend_project.py [base_url]
"""
import os
import shutil
import sys
from pathlib import Path

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

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8420"
FIXTURES = Path("/tmp/relion_test_projects")

errors = []
ok = True


def check(label, cond):
    global ok
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        ok = False


def make_fixtures():
    shutil.rmtree(FIXTURES, ignore_errors=True)
    # existing_project: already a RELION project (has RELION's own pipeline file)
    (FIXTURES / "existing_project").mkdir(parents=True)
    (FIXTURES / "existing_project" / "default_pipeline.star").write_text(
        "# fake pipeline star for testing\n")
    # plain_folder: a real folder that is NOT a project yet
    (FIXTURES / "plain_folder").mkdir(parents=True)


def main():
    make_fixtures()

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("console", lambda msg: errors.append(msg.text)
                if msg.type == "error" and "Failed to load resource" not in msg.text else None)
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", lambda resp: errors.append(f"HTTP {resp.status} {resp.url}")
                if resp.status >= 400 and "favicon.ico" not in resp.url else None)

        page.goto(BASE_URL + "/", wait_until="networkidle")
        page.wait_for_timeout(500)

        # --- browse to the fixture tree and switch to a real project ---
        page.locator("#changeProjectBtn").click()
        page.wait_for_selector("#projectModalOverlay:not(.hidden)", timeout=3000)
        page.locator("#projectPathInput").fill(str(FIXTURES))
        page.locator("#projectPathGoBtn").click()
        page.wait_for_timeout(500)
        entries = page.locator("#projectBrowser .browser-entry").all_inner_texts()
        check(f"Fixture folders listed ({entries})",
              any("plain_folder" in e for e in entries)
              and any("existing_project" in e for e in entries))

        page.locator("#projectBrowser .browser-entry", has_text="existing_project").first.click()
        page.wait_for_timeout(500)
        check("A folder with default_pipeline.star is badged as a project",
              page.locator(".project-badge.ok").count() > 0)

        page.locator("#projectSwitchBtn").click()
        page.wait_for_timeout(600)
        check("Switched to existing_project",
              "existing_project" in page.locator("#projectDirLabel").inner_text())

        # --- a folder that isn't a project yet: prompt, then start one ---
        page.locator("#changeProjectBtn").click(force=True)
        page.wait_for_selector("#projectModalOverlay:not(.hidden)", timeout=3000)
        page.locator("#projectPathInput").fill(str(FIXTURES / "brand_new_unseen_folder"))
        page.locator("#projectSwitchBtn").click(force=True)
        page.wait_for_selector("#notAProjectOverlay:not(.hidden)", timeout=3000)
        prompt_text = page.locator("#notAProjectOverlay .modal p").inner_text()
        check("Not-a-project prompt shown",
              "doesn't look like a Relion Project" in prompt_text)

        page.locator("#startNewProjectBtn").click()
        page.wait_for_timeout(600)
        check("Starting a new project switches to it",
              "brand_new_unseen_folder" in page.locator("#projectDirLabel").inner_text())
        check("Starting a new project does NOT fabricate default_pipeline.star",
              not (FIXTURES / "brand_new_unseen_folder" / "default_pipeline.star").exists())

        # --- recent projects: both visited projects should now be listed ---
        page.locator("#changeProjectBtn").click(force=True)
        page.wait_for_selector("#projectModalOverlay:not(.hidden)", timeout=3000)
        page.wait_for_timeout(500)
        recent = page.locator(".recent-entry .recent-entry-path").all_inner_texts()
        check(f"Both visited projects are in the recent list ({len(recent)} entries)",
              any(e.endswith("existing_project") for e in recent)
              and any(e.endswith("brand_new_unseen_folder") for e in recent))
        check("Most recently opened is first",
              recent and recent[0].endswith("brand_new_unseen_folder"))

        # double-clicking a recent entry switches straight to it
        page.locator(".recent-entry", has_text="existing_project").first.dblclick()
        page.wait_for_timeout(700)
        check("Double-clicking a recent project switches to it",
              "existing_project" in page.locator("#projectDirLabel").inner_text())

        # --- Create Folder ---
        page.locator("#changeProjectBtn").click(force=True)
        page.wait_for_selector("#projectModalOverlay:not(.hidden)", timeout=3000)
        page.locator("#projectPathInput").fill(str(FIXTURES))
        page.locator("#projectPathGoBtn").click()
        page.wait_for_timeout(400)
        page.locator("#newFolderNameInput").fill("CreatedFromUI")
        page.locator("#createFolderBtn").click()
        page.wait_for_timeout(600)
        check("Create Folder navigates into the new folder",
              page.locator("#projectPathInput").input_value().endswith("CreatedFromUI"))
        check("Create Folder actually created it on disk",
              os.path.isdir(FIXTURES / "CreatedFromUI"))
        check("No error banner on a successful create",
              "hidden" in page.locator("#projectModalError").get_attribute("class"))

        # empty name -> inline error, not a silent no-op and not a native alert
        page.locator("#newFolderNameInput").fill("")
        page.locator("#createFolderBtn").click()
        page.wait_for_timeout(300)
        check("Empty folder name shows an inline error",
              "hidden" not in page.locator("#projectModalError").get_attribute("class")
              and page.locator("#projectModalError").inner_text())

        # a file where a directory component is expected -> backend OSError,
        # surfaced in the banner rather than swallowed
        (FIXTURES / "CreatedFromUI" / "blocking_file").write_text("x")
        page.locator("#projectPathInput").fill(str(FIXTURES / "CreatedFromUI"))
        page.locator("#projectPathGoBtn").click()
        page.wait_for_timeout(300)
        page.locator("#newFolderNameInput").fill("blocking_file/nested")
        page.locator("#createFolderBtn").click()
        page.wait_for_timeout(600)
        check("Backend error text reaches the banner",
              "Could not create folder" in page.locator("#projectModalError").inner_text())

        page.locator("#projectModalCancelBtn").click()
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
