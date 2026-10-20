"""
Playwright suite for the DeepETPicker job types (Prepare / Train / Predict):
they appear in their own sidebar category, each popup opens with a draft
`conda run ... deepetpicker <command>` in the command box, editing a field
rewrites that command, a boolean becomes a bare switch, Run is refused with a
readable message when required inputs are missing, and a hand-edited command
runs as a real subprocess into the job's own directory.

The command itself is replaced with an `echo` for the run: the real one needs
conda and a DeepETPicker install (the backend tests assert the exact text).

Usage: python3 test_deepetpicker_popup.py [base_url]
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8420"

errors = []
ok = True


def check(label, cond):
    global ok
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        ok = False


def launch_browser(p):
    exe = os.environ.get("RELION_US_CHROMIUM")
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()


def recompute(page, win):
    """The command box is rewritten by the Recompute button, not live."""
    win.locator('[data-role="recompute-btn"]').click()
    page.wait_for_timeout(500)


def open_job(page, title):
    page.locator(".job-item", has_text=title).first.dispatch_event("click")
    page.wait_for_selector(".winbox .job-standard-form .field-label", timeout=5000)
    return page.locator(".winbox").first


with sync_playwright() as p:
    browser = launch_browser(p)
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "status of 400" not in m.text else None)  # the deliberate refusal
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE_URL + "/", wait_until="networkidle")
    page.wait_for_selector(".job-item", timeout=5000)

    block = page.locator('.category-block[data-category="DeepETPicker (Beta)"]')
    check("DeepETPicker category listed", block.count() == 1)
    items = block.locator(".job-item").all_inner_texts()
    check(f"three job types in it ({items})", len(items) == 3 and all("DeepETPicker" in t for t in items))
    check("the existing converter is still under Import & Conversion",
          page.locator('.category-block[data-category="Import & Conversion"] .job-item',
                       has_text="Import from DeepETPicker").count() == 1)

    # ---- Train ----
    win = open_job(page, "DeepETPicker – Train")
    cmd = win.locator(".command-box")
    check("draft command is a conda-run deepetpicker train",
          cmd.input_value().startswith("conda run --no-capture-output -n deepetpicker deepetpicker train --output_dir DeepETPickerTrain/job"))
    win.locator('[data-field-key="val_tomo"] input').fill("TS_009")
    recompute(page, win)
    check("editing a field rewrites the command", "--val_tomo TS_009" in cmd.input_value())
    win.locator('[data-field-key="conda_env"] input').fill("my_env")
    recompute(page, win)
    check("conda environment goes into -n, not a flag",
          " -n my_env deepetpicker train" in cmd.input_value() and "--conda_env" not in cmd.input_value())
    page.locator(".winbox .wb-close").first.click()
    page.wait_for_timeout(200)

    # ---- Predict: boolean is a bare switch ----
    win = open_job(page, "DeepETPicker – Predict")
    cmd = win.locator(".command-box")
    check("no refinement switch by default", "--refine_centroids" not in cmd.input_value())
    win.locator(".opt-section-head", has_text="Output").click()      # groups are collapsed <details>
    win.locator('[data-field-key="refine_centroids"] input[type="checkbox"]').check()
    recompute(page, win)
    check("ticking the box adds the bare switch", "--refine_centroids " in cmd.input_value() + " "
          and "--refine_centroids True" not in cmd.input_value())
    page.locator(".winbox .wb-close").first.click()
    page.wait_for_timeout(200)

    # ---- Prepare: missing inputs refused with a message, nothing started ----
    win = open_job(page, "DeepETPicker – Prepare")
    win.locator('[data-role="run-btn"]').click()
    page.wait_for_timeout(800)
    shown = (win.text_content() or "").lower()
    check("Run without inputs is refused with a readable reason", "exactly one of a tomograms" in shown)
    check("Run button is usable again after the refusal", win.locator('[data-role="run-btn"]').is_enabled())
    check("no run was created", page.locator("#ccTableBody tr").count() == 0)

    # ---- hand-edited command runs as a real subprocess in the job's directory ----
    cmd = win.locator(".command-box")
    sub = cmd.input_value().split("--output_dir ")[1].split()[0].rstrip("/")
    cmd.fill(f"echo prepared > {sub}/dataset.star")
    win.locator('[data-role="run-btn"]').click()
    page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)
    win.locator('.tab-btn[data-tab="outputs"]').click()
    page.wait_for_timeout(500)
    check("output shows up in the job's Outputs tab",
          "dataset.star" in win.locator('[data-tab-content="outputs"]').inner_text())
    check("Command Center has the run", page.locator("#ccTableBody tr").count() == 1)

    # ---- Train job gets a Progress tab (loss curve view) ----
    page.locator(".winbox .wb-close").first.click()
    page.wait_for_timeout(200)
    win = open_job(page, "DeepETPicker – Train")
    cmd = win.locator(".command-box")
    sub = cmd.input_value().split("--output_dir ")[1].split()[0].rstrip("/")
    cmd.fill(f"sleep 3 && echo done > {sub}/metrics.csv")
    win.locator('[data-role="run-btn"]').click()
    page.wait_for_timeout(800)
    check("Progress tab offered for a DeepETPicker training run",
          win.locator('.tab-btn[data-tab="progress"]').is_visible())
    page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)

    check(f"no console/page/HTTP errors ({errors})", not errors)
    browser.close()

print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
