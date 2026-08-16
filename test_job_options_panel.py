"""
Playwright test for where a job's options live: RELION's own GUI options in the
top panel (collapsible sections, RELION's tab names and order), and the
Advanced tab reserved for command-line options the GUI never exposes, read from
the installed program's --help.

Needs a live backend whose PATH has a program answering to the job's binary
name; run_tests.sh puts a stub one there.

Usage: python3 test_job_options_panel.py [base_url]
"""
import os
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


def open_job(page, search, name):
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

        # ---- top panel: RELION's own tabs, as sections ----
        # (the headings are CSS-uppercased, so compare case-insensitively)
        names = [n.strip().lower() for n in win.locator(".opt-section-name").all_inner_texts()]
        check(f"Top panel is grouped by RELION's own tab names ({names})",
              names[:3] == ["i/o", "ctf", "optimisation"])
        check("Running is a section too (RELION's own Running tab)", "running" in names)
        check("First section starts expanded",
              win.locator(".opt-section").first.get_attribute("open") is not None)

        # every option RELION defines is reachable in the top panel
        counts = page.evaluate(
            """async () => {
                 const def = await (await fetch('/api/jobs/Class2D')).json();
                 const placed = def.standard_groups.flatMap(g => g.fields);
                 return [def.options.length, placed.length, new Set(placed).size];
               }"""
        )
        check(f"Every RELION option is in the top panel, once ({counts})",
              counts[0] == counts[1] == counts[2])

        # collapsed sections still hold real fields; expanding reveals them
        before = win.locator(".opt-section-grid [data-field-key]:visible").count()
        win.locator(".opt-section-head", has_text="Optimisation").click()
        page.wait_for_timeout(300)
        after = win.locator(".opt-section-grid [data-field-key]:visible").count()
        check(f"Expanding a section reveals its fields ({before} -> {after})", after > before)

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

        # ---- Advanced tab: what the GUI does NOT expose ----
        adv = win.locator('[data-tab-content="advanced"]')
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

        win3 = open_job(page, "AreTomo2", "AreTomo2")
        page.wait_for_timeout(1000)
        note = win3.locator('[data-tab-content="advanced"] .cli-note').first.inner_text()
        check(f"A custom bridge says so instead of listing CLI options ({note[:40]}…)",
              "import bridge" in note)

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
