"""
Playwright test for the orthogonal (three-panel) tomogram viewer and the
recent-projects list in the Change Project dialog.

Needs a live backend. Writes a small MRC + picks STAR fixture into the active
project so the viewer has a real volume to slice.

Usage: python3 test_viewer_and_recents.py [base_url] [project_dir]
"""
import os
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

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8429"
PROJECT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("relion_project")

errors = []
ok = True


def check(label, cond):
    global ok
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        ok = False


def make_fixtures():
    import numpy as np
    import mrcfile
    import starfile
    import pandas as pd

    d = PROJECT_DIR / "Tomograms" / "job099"
    d.mkdir(parents=True, exist_ok=True)
    # Deliberately anisotropic (nx != ny != nz) so a panel that used the wrong
    # dimension shows up as a wrong aspect ratio rather than passing by luck.
    nx, ny, nz = 64, 48, 24
    vol = (np.random.rand(nz, ny, nx) * 50).astype(np.float32)
    vol[10:14, 20:26, 30:38] += 400          # a blob to see
    with mrcfile.new(d / "TS_77.mrc", overwrite=True) as m:
        m.set_data(vol)
        m.voxel_size = 10.0
    starfile.write(
        {"particles": pd.DataFrame({
            "rlnTomoName": ["TS_77"] * 3,
            "rlnCoordinateX": [32.0, 12.0, 50.0],
            "rlnCoordinateY": [24.0, 8.0, 40.0],
            "rlnCoordinateZ": [12.0, 4.0, 20.0]})},
        d / "picks99.star", overwrite=True)
    return nx, ny, nz


def main():
    nx, ny, nz = make_fixtures()

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)

        page.goto(BASE_URL + "/", wait_until="networkidle")
        page.wait_for_selector(".job-item", timeout=5000)

        # ---------------- recent projects ----------------
        page.locator("#changeProjectBtn").click()
        page.wait_for_selector("#projectModalOverlay:not(.hidden)", timeout=5000)
        page.wait_for_timeout(500)
        check("Recent projects section is shown",
              page.locator("#recentProjectsWrap").is_visible())
        entries = page.locator(".recent-entry")
        check("Current project appears in the recent list", entries.count() >= 1)
        first_path = entries.first.locator(".recent-entry-path").inner_text()
        check(f"Recent entry shows a full path ({first_path})", first_path.startswith("/"))
        check("Each entry has a forget button",
              entries.first.locator(".recent-forget").count() == 1)

        # clicking browses to it (fills the path box) rather than switching outright
        page.locator("#projectPathInput").fill("")
        entries.first.click()
        page.wait_for_timeout(600)
        check("Clicking a recent project browses to it",
              page.locator("#projectPathInput").input_value() == first_path)

        # forgetting removes it from the list but not from disk
        before = entries.count()
        entries.first.locator(".recent-forget").click()
        page.wait_for_timeout(600)
        after = page.locator(".recent-entry").count()
        check(f"Forget removes the entry ({before} -> {after})", after == before - 1)
        check("Forget did not delete the folder", PROJECT_DIR.is_dir())

        page.locator("#projectModalCancelBtn").click()
        page.wait_for_timeout(300)

        # ---------------- orthogonal viewer ----------------
        page.locator("#visualizeBtn").click()
        page.wait_for_selector(".viz-popup", timeout=5000)
        viz = page.locator(".winbox.viz-winbox").first

        check("Three orthogonal panels exist", viz.locator(".viz-panel").count() == 3)
        check("Controls live in a right-hand rail", viz.locator(".viz-side").count() == 1)
        check("Path inputs use the compact class",
              viz.locator('input.viz-input-sm[data-role="viz-path"]').count() == 1)

        viz.locator('[data-role="viz-path"]').fill("Tomograms/job099/TS_77.mrc")
        viz.locator('[data-role="viz-particles"]').fill("Tomograms/job099/picks99.star")
        viz.locator('[data-role="viz-load"]').click()
        page.wait_for_selector('[data-role="viz-img"]', state="visible", timeout=8000)
        page.wait_for_timeout(1800)

        # every panel actually loaded an image
        loaded = viz.locator(".viz-panel img").evaluate_all(
            "els => els.map(e => e.naturalWidth)")
        check(f"All three panels loaded a slice ({loaded})",
              len(loaded) == 3 and all(w > 0 for w in loaded))

        # ...and each has the dimensions its plane implies
        sizes = viz.locator(".viz-panel img").evaluate_all(
            "els => els.map(e => [e.getAttribute('data-role'), e.naturalWidth, e.naturalHeight])")
        by_role = {r: (w, h) for r, w, h in sizes}
        check(f"XY panel is nx x ny ({by_role.get('viz-img')})", by_role["viz-img"] == (nx, ny))
        check(f"ZY panel is nz x ny — transposed ({by_role.get('img-zy')})", by_role["img-zy"] == (nz, ny))
        check(f"XZ panel is nx x nz ({by_role.get('img-xz')})", by_role["img-xz"] == (nx, nz))

        # panels are laid out with one isotropic voxel scale, so the shared
        # edges match: XY and XZ share width, XY and ZY share height
        box = viz.locator(".viz-panel[data-panel='xy']").bounding_box()
        box_xz = viz.locator(".viz-panel[data-panel='xz']").bounding_box()
        box_zy = viz.locator(".viz-panel[data-panel='zy']").bounding_box()
        check(f"XY and XZ share a width ({box['width']:.0f} vs {box_xz['width']:.0f})",
              abs(box["width"] - box_xz["width"]) < 2)
        check(f"XY and ZY share a height ({box['height']:.0f} vs {box_zy['height']:.0f})",
              abs(box["height"] - box_zy["height"]) < 2)
        check("ZY sits to the left of XY", box_zy["x"] < box["x"])
        check("XZ sits below XY", box_xz["y"] > box["y"])
        check("XY is the largest panel",
              box["width"] * box["height"] > box_xz["width"] * box_xz["height"]
              and box["width"] * box["height"] > box_zy["width"] * box_zy["height"])

        # crosshair starts centred
        meta = viz.locator('[data-role="viz-meta"]').inner_text()
        check(f"Meta reports the volume and crosshair ({meta})",
              "TS_77" in meta and "3 picks" in meta
              and f"x {nx // 2}" in meta and f"z {nz // 2}" in meta)

        # clicking in XY moves x and y (not z), and refreshes the other panels
        zy_src_before = viz.locator('[data-role="img-zy"]').get_attribute("src")
        viz.locator(".viz-panel[data-panel='xy']").click(position={"x": 20, "y": 20})
        page.wait_for_timeout(900)
        meta2 = viz.locator('[data-role="viz-meta"]').inner_text()
        check(f"Clicking XY moved the crosshair ({meta2.split('·')[-1].strip()})",
              meta2 != meta and f"z {nz // 2}" in meta2)
        check("Clicking XY refetched the ZY slice",
              viz.locator('[data-role="img-zy"]').get_attribute("src") != zy_src_before)

        # scrolling over XY steps through Z only
        xy_src_before = viz.locator('[data-role="viz-img"]').get_attribute("src")
        viz.locator(".viz-panel[data-panel='xy']").hover()
        page.mouse.wheel(0, 200)
        page.wait_for_timeout(700)
        check("Scrolling XY stepped through Z",
              viz.locator('[data-role="viz-img"]').get_attribute("src") != xy_src_before)

        # sliders drive the same state
        viz.locator('[data-role="pos-z"]').fill("3")
        viz.locator('[data-role="pos-z"]').dispatch_event("input")
        page.wait_for_timeout(700)
        check("Z slider moves the crosshair",
              "z 3" in viz.locator('[data-role="viz-meta"]').inner_text())

        # overlays are sized to their panels (picks/crosshair drawn, not blank)
        cv = viz.locator(".viz-panel canvas").evaluate_all(
            "els => els.map(e => [e.width, e.height])")
        check(f"All overlays sized to their panel ({cv})",
              len(cv) == 3 and all(w > 0 and h > 0 for w, h in cv))
        painted = viz.locator('[data-role="ov-xy"]').evaluate(
            "c => { const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;"
            "       for (let i = 3; i < d.length; i += 4) if (d[i]) return true; return false; }")
        check("XY overlay has something drawn on it", painted)

        # toggling the crosshair off clears it
        viz.locator('[data-role="viz-crosshair"]').uncheck()
        viz.locator('[data-role="viz-showpicks"]').uncheck()
        page.wait_for_timeout(400)
        blank = viz.locator('[data-role="ov-xy"]').evaluate(
            "c => { const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;"
            "       for (let i = 3; i < d.length; i += 4) if (d[i]) return false; return true; }")
        check("Turning off picks + crosshair clears the overlay", blank)

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
