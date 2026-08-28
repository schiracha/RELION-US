"""
Playwright test for where a job's options live: RELION's own GUI options in
the Inputs tab (collapsible sections, RELION's tab names and order), and the
Advanced section -- inside the Inputs tab, past every one of RELION's own
groups -- reserved for command-line options the GUI never exposes, read from
the installed program's --help.

Needs a live backend whose PATH has a program answering to the job's binary
name; run_tests.sh puts a stub one there.

Usage: python3 test_job_options_panel.py [base_url]
"""
import os
import re
import sys

from playwright.sync_api import sync_playwright


def launch_browser(p):
    exe = os.environ.get("RELION_US_CHROMIUM")
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8431"

errors = []
ok = True


def check(label, cond):
    global ok
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        ok = False


def out_subdir(win):
    """Extract the job's output dir (from the draft's --o / --output-directory)
    so a simulated command can write into it -- jobs run from the PROJECT
    ROOT (like RELION), so a bare `> file` would land in the project root,
    not the job dir the Outputs tab reads. (Same helper as test_jobs.py.)"""
    cmd = win.locator(".command-box").input_value()
    m = re.search(r"--(?:o|output-directory)\s+(\S+)", cmd)
    return (m.group(1).rstrip("/") if m else "").strip("'\"")


def open_job(page, search, name):
    # Job popups are near window-filling and only one is ever open at once
    # (see app.js's currentJobWinbox) -- close whichever is open first, the
    # same way a real user would, so the sidebar underneath is reachable.
    if page.locator(".winbox").count() > 0:
        page.locator(".winbox .wb-close").first.click()
        page.wait_for_timeout(200)
    page.locator("#jobSearch").fill(search)
    page.wait_for_timeout(300)
    page.locator(".job-item:visible", has_text=name).first.click()
    page.wait_for_selector(".winbox", timeout=5000)
    return page.locator(".winbox").last


def main():
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}")
                if r.status >= 400 and "favicon" not in r.url else None)

        page.goto(BASE_URL + "/", wait_until="networkidle")
        page.wait_for_selector(".job-item", timeout=5000)

        win = open_job(page, "2D classification", "2D classification")
        page.evaluate("() => { const w = document.querySelector('.winbox');"
                      " w.style.height='900px'; w.style.top='20px'; }")
        page.wait_for_timeout(1200)

        # ---- Inputs tab: RELION's own tabs, as sections ----
        check("Inputs tab is the one open by default",
              "active" in (win.locator('.tab-btn[data-tab="inputs"]').get_attribute("class") or "")
              and "active" in (win.locator('[data-tab-content="inputs"]').get_attribute("class") or ""))
        # (the headings are CSS-uppercased, so compare case-insensitively)
        names = [n.strip().lower() for n in win.locator(".opt-section-name").all_inner_texts()]
        check(f"Inputs tab is grouped by RELION's own tab names ({names})",
              names[:3] == ["i/o", "ctf", "optimisation"])
        check("Running is a section too (RELION's own Running tab)", "running" in names)
        check(f"Advanced is its own section, last, past Running ({names})",
              names[-1] == "advanced" and names.index("running") < len(names) - 1)
        check("First section starts expanded",
              win.locator(".opt-section").first.get_attribute("open") is not None)
        check("Advanced section starts collapsed",
              win.locator('[data-role="advanced-section"]').get_attribute("open") is None)

        # every option RELION defines is reachable in the Inputs tab
        counts = page.evaluate(
            """async () => {
                 const def = await (await fetch('/api/jobs/Class2D')).json();
                 const placed = def.standard_groups.flatMap(g => g.fields);
                 return [def.options.length, placed.length, new Set(placed).size];
               }"""
        )
        check(f"Every RELION option is in the Inputs tab, once ({counts})",
              counts[0] == counts[1] == counts[2])

        # collapsed sections still hold real fields; expanding reveals them
        before = win.locator(".opt-section-grid [data-field-key]:visible").count()
        win.locator(".opt-section-head", has_text="Optimisation").click()
        page.wait_for_timeout(300)
        after = win.locator(".opt-section-grid [data-field-key]:visible").count()
        check(f"Expanding a section reveals its fields ({before} -> {after})", after > before)

        # ---- Browse buttons: any single-file field, not just STAR/glob params ----
        # fn_img (I/O, open by default) is a genuine file field (RELION's own
        # pattern mixes STAR and image-stack extensions) and should get one;
        # psi_sampling (Sampling tab) is one of job_definitions_raw.json's
        # mis-extracted numeric "patterns" on a plain float field (its
        # pattern is literally "0.5", no glob or parens) and must not.
        check("A genuine file field (fn_img) has a Browse button",
              win.locator('[data-field-key="fn_img"] .field-browse-row').count() == 1)
        win.locator(".opt-section-head", has_text="Sampling").click()
        page.wait_for_timeout(300)
        check("A mis-extracted numeric field (psi_sampling) gets no Browse button",
              win.locator('[data-field-key="psi_sampling"] .field-browse-row').count() == 0)

        # the Running fields are real and drive the command
        win.locator(".opt-section-head", has_text="Running").click()
        page.wait_for_timeout(300)
        check("MPI procs field present", win.locator('[data-field-key="nr_mpi"]').count() == 1)
        check("Threads field present", win.locator('[data-field-key="nr_threads"]').count() == 1)
        check("Additional arguments field present",
              win.locator('[data-field-key="other_args"]').count() == 1)

        cmd = win.locator(".command-box").input_value()
        check(f"Serial draft uses the non-MPI binary ({cmd.split()[0]})",
              "relion_refine" in cmd and "_mpi" not in cmd and not cmd.startswith("mpirun"))
        check("Threads reach the command as RELION's --j", "--j " in cmd)

        # MPI: RELION's own wrapping (mpirun -n N + the _mpi binary)
        win.locator('[data-field-key="nr_mpi"] input[type="number"], '
                    '[data-field-key="nr_mpi"] input[type="range"]').first.fill("4")
        win.locator('[data-role="recompute-btn"]').click()
        page.wait_for_timeout(900)
        cmd2 = win.locator(".command-box").input_value()
        check(f"MPI > 1 prefixes mpirun -n and swaps in the _mpi binary ({cmd2[:46]}…)",
              cmd2.startswith("mpirun -n 4 ") and "relion_refine_mpi" in cmd2)

        # additional arguments are appended verbatim, exactly as RELION does
        win.locator('[data-field-key="other_args"] input[type="text"]').fill("--dont_check_norm --verb 2")
        win.locator('[data-role="recompute-btn"]').click()
        page.wait_for_timeout(900)
        cmd3 = win.locator(".command-box").input_value()
        check("Additional arguments are appended verbatim, last",
              cmd3.rstrip().endswith("--dont_check_norm --verb 2"))

        # ---- Advanced section (inside Inputs): what the GUI does NOT expose ----
        # Collapsed by default and loaded lazily on first expand (see app.js's
        # "toggle" listener on advancedSection) -- open it before checking
        # its content, and give the async cli-options fetch time to resolve.
        adv = win.locator('[data-role="advanced-section"]')
        adv.locator(".opt-section-head").click()
        page.wait_for_timeout(900)
        check("Advanced section is open after clicking it",
              adv.get_attribute("open") is not None)
        flags = adv.locator(".cli-option-flag").all_inner_texts()
        check(f"Advanced lists the program's non-GUI options ({flags})",
              "--dont_check_norm" in flags and "--verb" in flags
              # --angpix is a real example: relion_refine accepts it, but
              # Class2D's own form never offers it (it comes from the STAR).
              and "--angpix" in flags)
        check("Advanced does NOT repeat options already in the form",
              # --pad and --j are emitted by Class2D's own builder and --o by
              # the draft; offering them again would be duplicate, conflicting UI.
              "--pad" not in flags and "--j" not in flags and "--o" not in flags)
        check("Advanced explains where the list came from",
              "--help" in adv.locator(".cli-note").first.inner_text())

        # a boolean option offers no value box; a valued one does
        row_bool = adv.locator(".cli-option[data-flag='--dont_check_norm']")
        row_val = adv.locator(".cli-option[data-flag='--verb']")
        check("Boolean option has no value box", row_bool.locator(".cli-option-value").count() == 0)
        check("Valued option has a value box", row_val.locator(".cli-option-value").count() == 1)

        # filtering
        adv.locator(".cli-search").fill("onlyflip")
        page.wait_for_timeout(250)
        check("Filter narrows the list",
              adv.locator(".cli-option:visible").count() == 1
              and adv.locator(".cli-option:visible").first.get_attribute("data-flag")
                  == "--onlyflipphases")
        adv.locator(".cli-search").fill("")
        page.wait_for_timeout(250)

        # adding appends to the command box rather than applying invisibly
        before_cmd = win.locator(".command-box").input_value()
        row_val.locator(".cli-option-value").fill("3")
        row_val.locator('[data-role="cli-add"]').click()
        page.wait_for_timeout(300)
        after_cmd = win.locator(".command-box").input_value()
        check(f"Adding an advanced option appends it to the command box (…{after_cmd[-12:]!r})",
              after_cmd == before_cmd.rstrip() + " --verb 3")

        # ---- a job with no MPI variant, and a custom job ----
        win2 = open_job(page, "Import", "Import")
        page.wait_for_timeout(1000)
        names2 = [n.strip().lower() for n in win2.locator(".opt-section-name").all_inner_texts()]
        check(f"Import is grouped too, with a Running section ({names2})", "running" in names2)
        check("Import has no MPI field (RELION gives it none)",
              win2.locator('[data-field-key="nr_mpi"]').count() == 0)
        win2.locator(".opt-section-head", has_text="Running").click()
        page.wait_for_timeout(300)
        check("Import still has Additional arguments",
              win2.locator('[data-field-key="other_args"]').count() == 1)

        # ---- Any single-file field gets a Browse button, STAR and otherwise ----
        # Import's own options mix a plain-text raw-movie glob field with two
        # genuine single-file fields of different types: fn_in_raw ("Raw
        # input files:", RELION's own pattern is a movie/image extension
        # list) and fn_mtf (a STAR file). Both are field_type filename in
        # RELION's own definitions, so both get a button -- expand every
        # section so both are in the DOM regardless of which RELION tab they
        # live in.
        # (excludes [data-role="advanced-section"] -- also an .opt-section,
        # but opening it triggers its own lazy --help fetch, unrelated here)
        for section in win2.locator(".opt-section:not([data-role='advanced-section'])").all():
            if section.get_attribute("open") is None:
                section.locator(".opt-section-head").click()
        page.wait_for_timeout(300)
        raw_browse = win2.locator('[data-field-key="fn_in_raw"] .field-browse-row')
        check("The raw-movie file field (fn_in_raw) has a Browse button too",
              raw_browse.count() == 1 and raw_browse.locator("button").count() == 1)
        mtf_browse = win2.locator('[data-field-key="fn_mtf"] .field-browse-row')
        check("The single-STAR-file field (fn_mtf) has a Browse button",
              mtf_browse.count() == 1 and mtf_browse.locator("button").count() == 1)

        mtf_browse.locator("button").click()
        page.wait_for_selector(".file-picker .project-browser:not([aria-busy])", timeout=5000)
        check("Browse opens the same file picker the visualizer uses",
              ".star" in page.locator(".file-picker .modal-hint").inner_text().lower())
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        check("Escape cancels the picker without touching the field",
              page.locator(".file-picker").count() == 0
              and win2.locator('[data-field-key="fn_mtf"] input[type="text"]').input_value() == "")

        # fn_in_raw starts pre-filled with RELION's own example default
        # ("Micrographs/*.tif") rather than empty like fn_mtf -- clear it
        # first so Browse opens the project root instead of a folder that
        # doesn't exist yet in this fresh test project (a real project
        # wouldn't have one either before the user has imported anything).
        win2.locator('[data-field-key="fn_in_raw"] input[type="text"]').fill("")
        raw_browse.locator("button").click()
        page.wait_for_selector(".file-picker .project-browser:not([aria-busy])", timeout=5000)
        check("The raw-movie picker filters by its own (non-STAR) extensions",
              ".mrc" in page.locator(".file-picker .modal-hint").inner_text().lower())
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

        win3 = open_job(page, "AreTomo2", "AreTomo2")
        page.wait_for_timeout(1000)
        win3.locator('[data-role="advanced-section"] .opt-section-head').click()
        page.wait_for_timeout(600)
        note = win3.locator('[data-role="advanced-section"] .cli-note').first.inner_text()
        check(f"A custom bridge says so instead of listing CLI options ({note[:40]}…)",
              "import bridge" in note)

        # ---- Settings' job-run defaults: applied to a FRESH job only, never
        # to a historical run's own recorded values ----
        def put_settings(values):
            # Also invalidates the frontend's cachedGlobalSettings via its
            # exposed reset hook, same as a real Save click in the Settings
            # popup would -- a raw API write here (bypassing the popup UI)
            # needs to do the same, or every popup opened after this would
            # still see the settings from before this call.
            page.evaluate(
                "(v) => fetch('/api/settings', {method: 'PUT', "
                "headers: {'Content-Type': 'application/json'}, "
                "body: JSON.stringify({values: v})}).then(() => "
                "window.invalidateGlobalSettingsCache())",
                values,
            )

        put_settings({"job_defaults.nr_mpi": None})  # clean slate

        # Extract, not 2D classification -- Class2D's --o carries an
        # output_suffix ("run", a file-rootname prefix, not a real
        # directory: --o Class2D/jobNNN/run), so out_subdir() would point
        # this test's echo redirect at a path that doesn't exist. Extract
        # has no output_suffix (--o Extract/jobNNN/, a bare directory) and
        # still has nr_mpi, so it's a safe stand-in for this check.
        win4 = open_job(page, "Particle Extraction", "Particle Extraction")
        page.wait_for_timeout(1000)
        win4.locator(".opt-section-head", has_text="Running").click()
        page.wait_for_timeout(300)
        check("Fresh job with no global default shows the job type's own default (1)",
              win4.locator('[data-field-key="nr_mpi"] input').input_value() == "1")
        sub4 = out_subdir(win4)
        win4.locator(".command-box").fill(f"echo hello-from-job4 > {sub4}/job4_output.txt")
        win4.locator('[data-role="run-btn"]').click()
        page.wait_for_selector('[data-role="status-line"]:has-text("completed")', timeout=10000)
        page.wait_for_timeout(300)
        win4.locator('[data-action="close"]').click()
        page.wait_for_timeout(300)

        put_settings({"job_defaults.nr_mpi": 4})

        win5 = open_job(page, "Particle Extraction", "Particle Extraction")
        page.wait_for_timeout(1000)
        win5.locator(".opt-section-head", has_text="Running").click()
        page.wait_for_timeout(300)
        check("A NEW job of the same type now prefills the global default (4)",
              win5.locator('[data-field-key="nr_mpi"] input').input_value() == "4")
        win5.locator('[data-action="close"]').click()
        page.wait_for_timeout(300)

        page.locator('.cc-view-btn[data-view="table"]').click()
        page.wait_for_timeout(200)
        # The only completed run in this project's Command Center is the
        # job4 run just above -- win5 was closed without ever clicking Run.
        page.locator("#ccTableBody tr").first.click()
        page.wait_for_selector(".winbox", timeout=5000)
        win6 = page.locator(".winbox").first
        win6.locator(".opt-section-head", has_text="Running").click()
        page.wait_for_timeout(300)
        check("Reopening that COMPLETED run still shows its own recorded value (1), "
              "not the global default set afterward",
              win6.locator('[data-field-key="nr_mpi"] input').input_value() == "1")
        win6.locator('[data-action="close"]').click()
        page.wait_for_timeout(200)

        put_settings({"job_defaults.nr_mpi": None})  # leave clean for any later suite

        # ---- Settings popup driven through the real DOM ----
        # Everything above uses put_settings() (a raw fetch PUT) deliberately,
        # to set up state without exercising the popup's own UI. This section
        # is the other half: open it via the actual menu, fill a field, click
        # Save, and confirm the value round-trips through the real Save
        # button -- not just the API.
        def open_settings_popup():
            page.locator("#menuBtn").click()
            page.wait_for_timeout(200)
            page.locator("#menuSettingsBtn").click()
            page.wait_for_selector(".settings-winbox", timeout=5000)
            return page.locator(".settings-winbox")

        settings = open_settings_popup()
        mpi_field = settings.locator('input[data-key="job_defaults.nr_mpi"]')
        check("Settings popup opens with the MPI field empty (cleaned up above)",
              mpi_field.input_value() == "")
        mpi_field.fill("6")
        settings.locator('[data-role="save"]').click()
        # openSettingsPopup()'s save handler awaits the PUT then calls
        # win.close() synchronously, and WinBox.close() removes the popup's
        # DOM node immediately (no animation/setTimeout) -- so wait for that
        # removal rather than a guessed sleep.
        page.wait_for_selector(".settings-winbox", state="detached", timeout=5000)
        check("Settings popup closes itself after a successful Save",
              page.locator(".settings-winbox").count() == 0)

        settings = open_settings_popup()
        mpi_field = settings.locator('input[data-key="job_defaults.nr_mpi"]')
        check("Reopened Settings popup shows the value saved via the real Save button (6)",
              mpi_field.input_value() == "6")

        settings.locator('[data-role="cancel"]').click()
        page.wait_for_selector(".settings-winbox", state="detached", timeout=5000)

        # Cancel must not persist an unsaved change (a different field, so
        # this doesn't disturb the nr_mpi value just verified above). Pin the
        # baseline via put_settings() first -- reading whatever value happens
        # to already be in the field wouldn't distinguish "Cancel worked"
        # from "Cancel is broken but the field already held the new value".
        put_settings({"job_defaults.nr_threads": 3})
        settings = open_settings_popup()
        threads_field = settings.locator('input[data-key="job_defaults.nr_threads"]')
        check("Threads field shows the known baseline before the Cancel check (3)",
              threads_field.input_value() == "3")
        threads_field.fill("99")
        settings.locator('[data-role="cancel"]').click()
        page.wait_for_selector(".settings-winbox", state="detached", timeout=5000)

        settings = open_settings_popup()
        threads_field = settings.locator('input[data-key="job_defaults.nr_threads"]')
        check("Cancel does not persist an unsaved change (still the baseline 3, not 99)",
              threads_field.input_value() == "3")
        settings.locator('[data-role="cancel"]').click()
        page.wait_for_selector(".settings-winbox", state="detached", timeout=5000)
        put_settings({"job_defaults.nr_threads": None})  # leave clean for any later suite

        put_settings({"job_defaults.nr_mpi": None})  # leave clean for any later suite

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
