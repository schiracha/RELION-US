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
    cmd.fill(f"echo prepared > {sub}/dataset.star && printf '1 10.5 20 30\\n2 11 21 31\\n' > {sub}/picks.coords"
             f" && printf 'epoch,loss\\n1,0.5\\n2,0.25\\n' > {sub}/metrics.csv")
    win.locator('[data-role="run-btn"]').click()
    page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)
    win.locator('.tab-btn[data-tab="outputs"]').click()
    page.wait_for_timeout(500)
    check("output shows up in the job's Outputs tab",
          "dataset.star" in win.locator('[data-tab-content="outputs"]').inner_text())

    # ---- .coords and .csv tables preview like STAR files do ----
    outputs = win.locator('[data-tab-content="outputs"]')
    for name in ("picks.coords", "metrics.csv"):
        row = outputs.locator(".outputs-row", has_text=name)
        check(f"{name} is clickable for a preview", row.locator(".out-path-previewable").count() == 1)
    outputs.locator(".outputs-row", has_text="picks.coords").locator(".out-path").click()
    page.wait_for_selector('[data-role="star-preview-body"] table', timeout=5000)
    coords_text = outputs.locator('[data-role="star-preview-body"]').inner_text()
    check("a .coords file previews as a table with class and xyz columns",
          all(h in coords_text for h in ("class_id", "x", "y", "z")) and "10.5" in coords_text and "31" in coords_text
          and "2 rows" in coords_text)
    outputs.locator(".outputs-row", has_text="metrics.csv").locator(".out-path").click()
    page.wait_for_function("document.querySelector('[data-role=\"star-preview-body\"]').innerText.includes('epoch')", timeout=5000)
    csv_text = outputs.locator('[data-role="star-preview-body"]').inner_text()
    check("a .csv previews with its header and rows", "loss" in csv_text and "0.25" in csv_text and "2 rows" in csv_text)
    check("Command Center has the run", page.locator("#ccTableBody tr").count() == 1)

    # ---- a command edited by hand is flagged: Clone would not reproduce it ----
    win.locator('.tab-btn[data-tab="inputs"]').click()
    note = win.locator('[data-role="edited-note"]')
    check("the job says its command was edited after the form drew it up",
          note.is_visible() and "edited after it was drawn up" in note.inner_text()
          and "Clone starts from the form" in note.inner_text())
    win.locator('[data-action="clone"]').click()
    page.wait_for_selector(".mini-dialog", timeout=3000)
    dialog = page.locator(".mini-dialog").inner_text()
    check("Clone warns before cloning an edited job", "Clone anyway" in dialog and "may differ from what actually ran" in dialog)
    page.locator('.mini-dialog [data-role="cancel"]').click()
    page.wait_for_timeout(300)
    check("cancelling leaves the job open", win.locator('[data-role="edited-note"]').is_visible())

    # a run started with an untouched command is not flagged
    plain = page.request.post(BASE_URL + "/api/runs", data={
        "internal_name": "DeepETPickerPredict", "command": "true", "command_edited": False}).json()
    check("the API records an untouched command as not edited", plain["command_edited"] is False)
    page.wait_for_timeout(500)
    flagged = page.request.post(BASE_URL + "/api/runs", data={
        "internal_name": "DeepETPickerPredict", "command": "true", "command_edited": True}).json()
    check("and an edited one as edited", flagged["command_edited"] is True)
    page.wait_for_timeout(500)
    history = page.request.get(BASE_URL + "/api/runs").json()
    by_id = {r["run_id"]: r for r in history}
    check("the flag is kept in the history", by_id[flagged["run_id"]]["command_edited"] is True
          and by_id[plain["run_id"]]["command_edited"] is False)

    # the three ways a plain RELION job can go: untouched, form changed after
    # the command was drawn up (nothing recomputes it by itself), and recomputed
    def latest_import_run():
        runs = [r for r in page.request.get(BASE_URL + "/api/runs").json() if "Import" in r["internal_name"]]
        return sorted(runs, key=lambda r: r["job_number"])[-1]

    def run_import(change_form=False, recompute_first=False):
        page.locator(".winbox .wb-close").first.click()
        page.wait_for_timeout(200)
        w = open_job(page, "Import Tomo Tilt Series")
        field = w.locator(".job-standard-form input[type=text]").first
        if change_form:
            field.fill("some/other/value")
        if recompute_first:
            recompute(page, w)
        before = {r["run_id"] for r in page.request.get(BASE_URL + "/api/runs").json()}
        w.locator('[data-role="run-btn"]').click()
        for _ in range(60):
            page.wait_for_timeout(250)
            now = [r for r in page.request.get(BASE_URL + "/api/runs").json() if r["run_id"] not in before]
            if now:
                return now[0]
        return None

    untouched = run_import()
    check("an untouched command is not flagged", untouched is not None and untouched["command_edited"] is False)
    changed = run_import(change_form=True)
    check("a form changed after the command was drawn up is flagged",
          changed is not None and changed["command_edited"] is True)
    recomputed = run_import(change_form=True, recompute_first=True)
    check("recomputing the command after changing the form clears the flag",
          recomputed is not None and recomputed["command_edited"] is False)

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
