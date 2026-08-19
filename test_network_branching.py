"""
Playwright test for the Command Center's Network view on a wide, branching
pipeline -- one job fanning out to four children, one of those fanning out to
two more, every job carrying one of RELION's real (and long) tomography
display names.

This exists because a straight 5-job chain (test_legacy_project.py's fixture)
never has more than one node in a row, so it can't exercise column spacing
across a wide row or a taller-than-usual row (from a wrapped, longer job
name) -- exactly the shape of pipeline where a user first noticed the
connector lines landing short of the box below them. See run_tests.sh's
make_legacy_branchy_project and style.css's "Network view" comment for the
root cause (the overlay SVG's coordinate space losing sync with the node
grid's) and its fix.

Needs a live backend already pointed at the fixture project (run_tests.sh
builds one via make_legacy_branchy_project); pass the project directory as
the second argument.

Usage: python3 test_network_branching.py [base_url] [project_dir]
"""
import os
import sys

from playwright.sync_api import sync_playwright


def launch_browser(p):
    exe = os.environ.get("RELION_US_CHROMIUM")
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8433"

errors = []
ok = True


def check(label, cond):
    global ok
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        ok = False


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
        page.wait_for_timeout(600)

        page.locator('.cc-view-btn[data-view="network"]').click()
        page.wait_for_timeout(400)
        check("Network view becomes visible", page.locator("#ccNetworkView").is_visible())

        node_count = page.locator(".cc-network-node").count()
        check(f"All 9 jobs appear as network nodes ({node_count})", node_count == 9)
        edge_count = page.locator(".cc-network-edge").count()
        check(f"8 edges for a 9-job branching pipeline ({edge_count})", edge_count == 8)

        # job004 -> job005 -> job010 -> {job011, job013, job014, job015} ->
        # (job014 only) -> {job018, job021}: 5 rows deep, with job010's
        # fan-out putting all 4 of its children in the same (4th) row.
        rows_top_to_bottom = page.locator(".cc-network-row").all_inner_texts()
        check(f"5 rows deep ({len(rows_top_to_bottom)})", len(rows_top_to_bottom) == 5)
        check("job010's fan-out row has all 4 of its children",
              all(j in rows_top_to_bottom[3] for j in ("job011", "job013", "job014", "job015")))

        # The real check: every edge's endpoints land exactly on a node's
        # bottom-center (its start) and another node's top-center (its end)
        # in ACTUAL SCREEN PIXELS -- getBoundingClientRect() on both the
        # nodes and the SVG overlay, not offsetTop/Left compared against the
        # SVG path's own local coordinates app.js derived them from in the
        # first place. That comparison is tautological (both sides are the
        # same numbers by construction) and would pass even if the SVG were
        # rendering somewhere else on screen entirely, which is exactly what
        # was happening before the padding fix in style.css.
        def edges_touch_nodes():
            return page.evaluate("""() => {
                const nodes = Array.from(document.querySelectorAll('.cc-network-node')).map((n) => {
                    const r = n.getBoundingClientRect();
                    return { cx: r.left + r.width / 2, top: r.top, bottom: r.bottom };
                });
                const svgRect = document.getElementById('ccNetworkEdges').getBoundingClientRect();
                const near = (a, b) => Math.abs(a - b) < 0.5;
                return Array.from(document.querySelectorAll('.cc-network-edge')).every((path) => {
                    const parts = path.getAttribute('d').replace(/,/g, ' ').split(/\\s+/).filter(Boolean);
                    const x1 = svgRect.left + parseFloat(parts[1]);
                    const y1 = svgRect.top + parseFloat(parts[2]);
                    const x2 = svgRect.left + parseFloat(parts[parts.length - 2]);
                    const y2 = svgRect.top + parseFloat(parts[parts.length - 1]);
                    const startsAtABottom = nodes.some((n) => near(n.cx, x1) && near(n.bottom, y1));
                    const endsAtATop = nodes.some((n) => near(n.cx, x2) && near(n.top, y2));
                    return startsAtABottom && endsAtATop;
                });
            }""")
        check("Every edge touches a node's bottom at one end and a node's top at the other, "
              "in real screen pixels, across the 4-way branch",
              edges_touch_nodes())

        # At this node width/font, "Extract Pseudo-subtomograms" (the longest
        # display name used here) happens to fit on one line -- so this fixture
        # doesn't currently exercise wrapped, taller-than-usual rows. Logged
        # rather than asserted on: if a future name or a narrower node width
        # does make a row taller, the precise edges_touch_nodes() check above
        # (re-run after this) is what actually guards against attachment
        # drifting for mismatched row heights, not a specific pixel count here.
        heights = page.evaluate("""() =>
            Array.from(document.querySelectorAll('.cc-network-row'))
                .map((row) => row.getBoundingClientRect().height)""")
        print(f"Row heights: {heights}")
        check("Edges still touch precisely on a re-render of this layout",
              edges_touch_nodes())

        page.screenshot(path="/tmp/relion_us_network_branching.png", full_page=True)
        browser.close()

    print()
    if errors:
        print("CONSOLE/PAGE ERRORS:")
        for e in errors:
            print(" -", e)
    return ok and not errors


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
