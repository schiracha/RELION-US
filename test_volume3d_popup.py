"""
Playwright suite for the 3D volume viewer (Mol*, vendored in frontend/vendor):
the topbar button opens it, a volume is added as an isosurface card with a
contour/opacity/mesh/colour/show/remove control set that really changes the
Mol* representation, a mask gets an absolute contour, bad paths give plain
messages, nothing is requested from any host but this app (it must work
offline on a cluster), and closing the window releases the viewer.

The test volume is written by a real job (a python one-liner run through the
app), so the suite needs no files of its own in the project.

Usage: python3 test_volume3d_popup.py [base_url]
"""
import os
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8420"

errors = []
external = set()
ok = True


def check(label, cond):
    global ok
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        ok = False


def launch_browser(p):
    exe = os.environ.get("RELION_US_CHROMIUM")
    args = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
    return p.chromium.launch(executable_path=exe, args=args) if exe else p.chromium.launch(args=args)


MAKE_VOLUMES = (
    "import mrcfile, numpy as np\n"
    "z, y, x = np.mgrid[:24, :24, :24]\n"
    "ball = (np.exp(-(((x-12)**2 + (y-12)**2 + (z-12)**2) / 30.0))).astype(np.float32)\n"
    "mrcfile.new('vol3d_density.mrc', ball, overwrite=True).voxel_size = 4.0\n"
    "mask = (ball > 0.3).astype(np.uint16)\n"
    "mrcfile.new('vol3d_mask.mrc', mask, overwrite=True).voxel_size = 4.0\n"
    "mrcfile.new('vol3d_image.mrc', np.zeros((1, 16, 16), dtype=np.float32), overwrite=True)\n"
)


def make_volumes(page):
    """Run one job whose command writes the test volumes into the project root."""
    command = f"{sys.executable} -c \"{MAKE_VOLUMES}\""
    resp = page.request.post(BASE_URL + "/api/runs", data={"internal_name": "IsonetDeconv", "command": command})
    run = resp.json()
    for _ in range(100):
        status = page.request.get(f"{BASE_URL}/api/runs/{run['run_id']}").json()
        if status["status"] in ("completed", "failed"):
            return status
        time.sleep(0.2)
    return status


with sync_playwright() as p:
    browser = launch_browser(p)
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "status of 400" not in m.text else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    host = urlparse(BASE_URL).netloc
    page.on("request", lambda r: external.add(urlparse(r.url).netloc) if urlparse(r.url).netloc not in ("", host) and r.url.startswith("http") else None)

    page.goto(BASE_URL + "/", wait_until="networkidle")
    page.wait_for_selector(".job-item", timeout=5000)

    made = make_volumes(page)
    check("test volumes were written by a job", made["status"] == "completed")

    check("3D button is in the top bar", page.locator("#volume3dBtn").count() == 1)
    check("Mol* is not loaded until it is needed", page.evaluate("typeof window.molstar") == "undefined")
    page.locator("#volume3dBtn").click()
    page.wait_for_selector(".vol3d-popup .vol3d-stage canvas", timeout=30000)
    check("viewer window opens with a WebGL canvas", page.locator(".vol3d-stage canvas").count() >= 1)
    check("Mol*'s own panels are hidden", page.evaluate(
        "JSON.stringify(window.molstar.Viewer.instances[0].plugin.layout.state.regionState)").count("full") == 0)

    def add(path):
        page.fill('[data-role="v3-path"]', path)
        page.click('[data-role="v3-add"]')

    def reps():
        return page.evaluate("""() => { const out = [];
          window.molstar.Viewer.instances[0].plugin.state.data.cells.forEach((c) => {
            if (c.transform.transformer.id === 'ms-plugin.volume-representation-3d') {
              const t = c.transform.params.type.params;
              out.push({ iso: t.isoValue, alpha: t.alpha, visuals: t.visuals,
                         color: c.transform.params.colorTheme.params.value, hidden: !!c.state.isHidden });
            } });
          return out; }""")

    add("vol3d_density.mrc")
    page.wait_for_selector(".vol3d-card", timeout=20000)
    check("a density map gets a card named after the file", page.locator(".vol3d-name").first.inner_text() == "vol3d_density.mrc")
    check("its first contour is 1.5 sigma", "1.50 σ" in page.locator('[data-role="c-readout"]').first.inner_text())
    check("it is one isosurface in Mol*", len(reps()) == 1 and reps()[0]["iso"]["kind"] == "relative")

    card = page.locator(".vol3d-card").first
    card.locator('[data-role="c-level"]').evaluate("(e) => { e.value = 2.5; e.dispatchEvent(new Event('input', {bubbles: true})); }")
    page.wait_for_timeout(800)
    check("moving the level slider changes the contour", abs(reps()[0]["iso"]["relativeValue"] - 2.5) < 1e-6
          and "2.50 σ" in card.locator('[data-role="c-readout"]').inner_text())
    card.locator('[data-role="c-mode"]').select_option("abs")
    page.wait_for_timeout(800)
    check("switching to an absolute value keeps the same level", reps()[0]["iso"]["kind"] == "absolute"
          and "(2.50 σ)" in card.locator('[data-role="c-readout"]').inner_text())
    card.locator('[data-role="c-style"]').select_option("wireframe")
    card.locator('[data-role="c-alpha"]').evaluate("(e) => { e.value = 0.4; e.dispatchEvent(new Event('input', {bubbles: true})); }")
    card.locator('[data-role="c-color"]').evaluate("(e) => { e.value = '#e5484d'; e.dispatchEvent(new Event('input', {bubbles: true})); }")
    page.wait_for_timeout(1200)
    r = reps()[0]
    check("mesh, opacity and colour apply", r["visuals"] == ["wireframe"] and abs(r["alpha"] - 0.4) < 1e-6 and r["color"] == 0xe5484d)
    card.locator('[data-role="c-visible"]').uncheck()
    page.wait_for_timeout(500)
    check("hiding a map hides it in Mol*", reps()[0]["hidden"] is True)
    card.locator('[data-role="c-visible"]').check()

    add("vol3d_mask.mrc")
    page.wait_for_function("document.querySelectorAll('.vol3d-card').length === 2", timeout=20000)
    mask_card = page.locator(".vol3d-card").nth(1)
    check("a 0/1 mask is recognised and contoured at 0.5", "mask" in mask_card.inner_text()
          and mask_card.locator('[data-role="c-mode"]').input_value() == "abs"
          and abs(reps()[1]["iso"]["absoluteValue"] - 0.5) < 1e-6)
    check("two maps are overlaid", len(reps()) == 2)

    add("vol3d_image.mrc")
    page.wait_for_function("document.querySelector('[data-role=\"v3-status\"]').textContent.includes('not a volume')", timeout=10000)
    check("a single image is refused in plain words", page.locator('[data-role="v3-status"]').inner_text().count("not a volume") == 1)
    add("no/such/file.mrc")
    page.wait_for_function("document.querySelector('[data-role=\"v3-status\"]').textContent.includes('not found')", timeout=10000)
    check("a missing file is reported", True)
    check("failed adds left no card behind", page.locator(".vol3d-card").count() == 2)

    card.locator('[data-role="c-remove"]').click()
    page.wait_for_timeout(800)
    check("removing a map removes it from Mol*", page.locator(".vol3d-card").count() == 1 and len(reps()) == 1)

    page.locator('[data-role="v3-advanced"]').check()
    page.wait_for_timeout(700)
    check("Mol*'s panels can be shown", page.locator(".vol3d-stage .msp-layout-right").count() >= 1
          and page.evaluate("window.molstar.Viewer.instances[0].plugin.layout.state.regionState.right") == "full")

    check("nothing was requested from outside this app", not external)

    page.evaluate("document.querySelector('.vol3d-popup').closest('.winbox').querySelector('.wb-close').click()")
    page.wait_for_timeout(800)
    check("closing the window releases the viewer", page.locator(".vol3d-popup").count() == 0
          and page.evaluate("window.molstar.Viewer.instances.length") == 0)
    page.locator("#volume3dBtn").click()
    page.wait_for_selector(".vol3d-popup .vol3d-stage canvas", timeout=30000)
    check("it opens again afterwards", page.evaluate("window.molstar.Viewer.instances.length") == 1)

    check(f"no console/page errors ({errors})", not errors)
    browser.close()

print("\nALL PASSED" if ok else "\nSOME FAILED")
sys.exit(0 if ok else 1)
