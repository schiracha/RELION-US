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
builds one via make_legacy_branchy_project and starts the backend inside
it) -- this script only ever talks to it over HTTP, so it takes no project
path of its own.

Usage: python3 test_network_branching.py [base_url]
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
        # bottom edge (its start) and another node's top edge (its end)
        # in ACTUAL SCREEN PIXELS -- getBoundingClientRect() on both the
        # nodes and the SVG overlay, not offsetTop/Left compared against the
        # SVG path's own local coordinates app.js derived them from in the
        # first place. That comparison is tautological (both sides are the
        # same numbers by construction) and would pass even if the SVG were
        # rendering somewhere else on screen entirely, which is exactly what
        # was happening before the padding fix in style.css.
        def edges_touch_nodes(tol=0.5):
            return page.evaluate("""() => {
                const nodes = Array.from(document.querySelectorAll('.cc-network-node')).map((n) => {
                    const r = n.getBoundingClientRect();
                    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
                });
                const svgEl = document.getElementById('ccNetworkEdges');
                const svgRect = svgEl.getBoundingClientRect();
                const k = svgRect.width / svgEl.clientWidth;  // the drawing's zoom
                const near = (a, b) => Math.abs(a - b) < %s;
                const within = (n, x) => x >= n.left && x <= n.right;
                return Array.from(document.querySelectorAll('.cc-network-edge')).every((path) => {
                    const parts = path.getAttribute('d').replace(/,/g, ' ').split(/\\s+/).filter(Boolean);
                    const x1 = svgRect.left + parseFloat(parts[1]) * k;
                    const y1 = svgRect.top + parseFloat(parts[2]) * k;
                    const x2 = svgRect.left + parseFloat(parts[parts.length - 2]) * k;
                    const y2 = svgRect.top + parseFloat(parts[parts.length - 1]) * k;
                    const startsAtABottom = nodes.some((n) => within(n, x1) && near(n.bottom, y1));
                    const endsAtATop = nodes.some((n) => within(n, x2) && near(n.top, y2));
                    return startsAtABottom && endsAtATop;
                });
            }""" % tol)
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

        # Hovering a job lights its lineage and dims the rest.
        page.locator(".cc-network-node", has_text="job018").first.hover()
        page.wait_for_timeout(200)
        lit = page.locator(".cc-network-node.is-related").count()
        check(f"Hovering job018 lights its own lineage only ({lit} of 9)", lit == 5
              and page.locator("#ccNetworkCanvas.cc-focus").count() == 1)
        page.mouse.move(5, 5)
        page.wait_for_timeout(200)
        check("Moving off clears the highlight",
              page.locator(".cc-network-node.is-related").count() == 0
              and page.locator("#ccNetworkCanvas.cc-focus").count() == 0)
        # ---- click to trace, find, only-lineage, zoom --------------------------
        def count(sel):
            return page.locator(sel).count()

        check("Nothing says it is tracing before a job is selected", not page.locator("#ccNetworkSelected").is_visible())
        page.locator(".cc-network-node", has_text="job014").first.click()
        page.wait_for_timeout(200)
        check("Clicking a job selects it (and does not open it)",
              count(".cc-network-node.is-sel") == 1 and count(".winbox") == 0)
        check("The tools say which job is being traced", "job014" in page.locator("#ccNetworkSelected").inner_text())
        check(f"Its ancestors are marked ({count('.cc-network-node.is-anc')} = job004, job005, job010)",
              count(".cc-network-node.is-anc") == 3)
        check(f"Its descendants are marked separately ({count('.cc-network-node.is-desc')} = job018, job021)",
              count(".cc-network-node.is-desc") == 2)
        check("Both kinds of line are coloured",
              count(".cc-network-edge.is-up") == 3 and count(".cc-network-edge.is-down") == 2)
        page.locator(".cc-network-node", has_text="job015").first.hover()
        page.wait_for_timeout(200)
        check("A selection stays put while the mouse moves over other jobs",
              count(".cc-network-node.is-sel") == 1 and "job014" in page.locator(".cc-network-node.is-sel").inner_text())
        page.mouse.move(5, 5)

        page.locator("#ccNetworkOnlyBtn").click()
        page.wait_for_timeout(300)
        check(f"Only lineage lays out just the 6 related jobs ({count('.cc-network-node')})",
              count(".cc-network-node") == 6)
        check("...and its lines still touch the boxes", edges_touch_nodes())
        page.locator("#ccNetworkOnlyBtn").click()
        page.wait_for_timeout(300)
        check("Turning it off brings all 9 back", count(".cc-network-node") == 9 and count(".cc-network-node.is-sel") == 1)

        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        check("Esc lets go of the selection",
              count(".cc-network-node.is-sel") == 0 and count("#ccNetworkCanvas.cc-focus") == 0)

        page.locator("#ccNetworkSearch").fill("job01")
        page.wait_for_timeout(200)
        check(f"Find dims what doesn't match, in place ({count('.cc-network-nomatch')} of 9)",
              count(".cc-network-nomatch") == 3 and count(".cc-network-node") == 9)
        check("...and says how many match", "6 of 9" in page.locator("#ccNetworkInfo").inner_text())
        page.locator("#ccNetworkSearch").press("Enter")
        page.wait_for_timeout(200)
        check("Enter selects the first match", count(".cc-network-node.is-sel") == 1)
        page.locator("#ccNetworkSearch").press("Escape")
        page.wait_for_timeout(200)
        check("Esc in the box clears the search but not the selection",
              count(".cc-network-nomatch") == 0 and count(".cc-network-node.is-sel") == 1)
        page.locator("#ccNetworkClearBtn").click()

        before = page.locator("#ccNetworkZoom").bounding_box()["width"]
        page.locator("#ccNetworkZoomOut").click()
        page.wait_for_timeout(200)
        after = page.locator("#ccNetworkZoom").bounding_box()["width"]
        check(f"Zoom out shrinks the drawing ({before:.0f} -> {after:.0f}px, {page.locator('#ccNetworkZoomPct').inner_text()})",
              after < before * 0.9)
        check("Lines still touch the boxes when zoomed", edges_touch_nodes(2))
        page.locator("#ccNetworkResetBtn").click()
        page.wait_for_timeout(200)
        check("Clicking the percentage returns to 100%", page.locator("#ccNetworkZoomPct").inner_text() == "100%")

        # The hover trace can be switched off; clicking still traces.
        page.locator("#ccNetworkHover").uncheck()
        page.locator(".cc-network-node", has_text="job018").first.hover()
        page.wait_for_timeout(200)
        check("With hover tracing off, pointing at a job traces nothing",
              page.locator(".cc-network-node.is-related").count() == 0)
        check("...and the choice is remembered",
              page.evaluate("localStorage.getItem('relion_us_cc_network_hover')") == "off")
        page.locator(".cc-network-node", has_text="job018").first.click()
        page.wait_for_timeout(200)
        check("...but a click still does", page.locator(".cc-network-node.is-related").count() == 5)
        page.locator("#ccNetworkClearBtn").click()
        page.locator("#ccNetworkHover").check()
        page.mouse.move(5, 5)

        check("Edges carry an arrowhead", page.locator(".cc-network-edge[marker-end]").count() == 8)

        # ---- a big, messy graph: lines must stay clear of the boxes ----------
        # 40 jobs, most reading from one or two recent jobs, some from far back
        # (the lines that used to run behind other boxes), plus a few
        # parentless jobs used late.
        report = page.evaluate("""() => {
            let seed = 7;
            const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
            const runs = [];
            for (let i = 1; i <= 40; i++) {
                const links = [];
                const pick = (lo) => runs[lo + Math.floor(rnd() * (runs.length - lo))];
                if (i > 1) links.push({ run_id: pick(Math.max(0, runs.length - 6)).run_id });
                if (i > 6 && rnd() < 0.4) links.push({ run_id: pick(0).run_id });
                if (i > 3 && rnd() < 0.3) links.push({ run_id: pick(Math.max(0, runs.length - 4)).run_id });
                runs.push({ run_id: 'r' + i, job_number: i, job_name: 'job' + String(i).padStart(3, '0'),
                            internal_name: 'x', input_links: links });
            }
            const layout = computeLineageLayout(runs);
            const out = { crossings: layout.crossings, initial: layout.initialCrossings, edges: layout.chains.length };
            for (const direction of ['asc', 'desc']) {
                const rowsEl = document.createElement('div');
                rowsEl.style.cssText = 'position:relative;display:flex;flex-direction:column;gap:46px';
                const svgEl = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                svgEl.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;overflow:visible';
                const canvas = document.createElement('div');
                canvas.style.cssText = 'position:absolute;left:-9999px;top:0;width:max-content';
                canvas.append(rowsEl, svgEl);
                document.body.appendChild(canvas);
                renderLineageGraph(runs, { rowsEl, svgEl, emptyEl: null, direction,
                    onNodeClick: () => {}, nodeRenderer: (r) => r.job_name });
                const boxes = Array.from(rowsEl.querySelectorAll('.cc-network-node')).map((n) => ({
                    id: n.dataset.runId, l: n.offsetLeft, r: n.offsetLeft + n.offsetWidth,
                    t: n.offsetTop, b: n.offsetTop + n.offsetHeight }));
                let through = 0;
                svgEl.querySelectorAll('.cc-network-edge').forEach((path) => {
                    const len = path.getTotalLength();
                    for (let d = 3; d < len - 3; d += 2) {
                        const pt = path.getPointAtLength(d);
                        for (const bx of boxes) {
                            if (pt.x > bx.l + 1 && pt.x < bx.r - 1 && pt.y > bx.t + 1 && pt.y < bx.b - 1) {
                                through++; break;
                            }
                        }
                    }
                });
                // sideways distance the lines travel in total: long connections
                // that wander from column to column make this large
                let travel = 0, longJogs = 0, longEdges = 0;
                svgEl.querySelectorAll('.cc-network-edge').forEach((e) => {
                    const nums = e.getAttribute('d').replace(/[MLQC,]/g, ' ').split(/\s+/).filter(Boolean).map(Number);
                    for (let i = 2; i + 1 < nums.length; i += 2) travel += Math.abs(nums[i] - nums[i - 2]);
                    if (Math.abs(nums[nums.length - 1] - nums[1]) > 230) {   // spans 3+ rows
                        longEdges++;
                        longJogs += (e.getAttribute('d').match(/Q/g) || []).length / 2;
                    }
                });
                out[direction] = { paths: svgEl.querySelectorAll('.cc-network-edge').length, samplesInsideBoxes: through,
                                   travelPerEdge: Math.round(travel / svgEl.querySelectorAll('.cc-network-edge').length),
                                   jogsPerLongEdge: longEdges ? +(longJogs / longEdges).toFixed(2) : 0, longEdges };
                canvas.remove();
            }
            return out;
        }""")
        print(f"Messy graph: {report}")
        check(f"40-job graph: reordering does not add crossings ({report['initial']} -> {report['crossings']})",
              report["crossings"] <= report["initial"])
        for direction in ("asc", "desc"):
            check(f"40-job graph ({direction}): one line per edge ({report[direction]['paths']} of {report['edges']})",
                  report[direction]["paths"] == report["edges"])
            check(f"40-job graph ({direction}): no line runs through a job box",
                  report[direction]["samplesInsideBoxes"] == 0)

        # ---- rows of different widths: a long line must stay in one column ----
        # A backbone n1..n8 with a different number of side jobs hanging off
        # each step (so every row is a different width), and n8 also reading
        # from n1. Rows used to be centred one by one, which put that line's
        # lane in a different column in every row and made it zigzag.
        jogs = page.evaluate("""() => {
            const runs = [];
            const add = (id, parents) => runs.push({ run_id: id, job_number: runs.length + 1, job_name: id,
                internal_name: 'x', input_links: parents.map((p) => ({ run_id: p })) });
            add('n1', []);
            for (let i = 2; i <= 8; i++) {
                add('n' + i, ['n' + (i - 1)].concat(i === 8 ? ['n1'] : []));
                for (let k = 0; k < i % 4; k++) add(`s${i}_${k}`, ['n' + (i - 1)]);
            }
            const rowsEl = document.createElement('div');
            rowsEl.style.cssText = 'position:relative;display:flex;flex-direction:column;gap:46px';
            const svgEl = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svgEl.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;overflow:visible';
            const canvas = document.createElement('div');
            canvas.style.cssText = 'position:absolute;left:-9999px;top:0;width:max-content';
            canvas.append(rowsEl, svgEl);
            document.body.appendChild(canvas);
            renderLineageGraph(runs, { rowsEl, svgEl, emptyEl: null, direction: 'asc',
                onNodeClick: () => {}, nodeRenderer: (r) => r.job_name });
            const long = Array.from(svgEl.querySelectorAll('.cc-network-edge'))
                .find((e) => e.dataset.parent === 'n1' && e.dataset.child === 'n8');
            const corners = (long.getAttribute('d').match(/Q/g) || []).length;
            canvas.remove();
            return corners / 2;
        }""")
        check(f"A line spanning 7 rows of different widths makes at most 2 sideways jogs ({jogs})", jogs <= 2)

        # ---- unconnected tracks sit side by side, not stranded apart --------------
        # A short chain (a1-a3) beside a wide pipeline (b1..). The chain used to
        # end up past the pipeline's widest row instead of touching it.
        gap = page.evaluate("""() => {
            const runs = [];
            const add = (id, parents) => runs.push({ run_id: id, job_number: runs.length + 1, job_name: id,
                internal_name: 'x', input_links: parents.map((p) => ({ run_id: p })) });
            add('a1', []); add('a2', ['a1']); add('a3', ['a2']);
            add('b1', []); add('b2', ['b1']);
            ['c1', 'c2', 'c3', 'c4', 'c5'].forEach((c) => add(c, ['b2']));
            ['d1', 'd2', 'd3', 'd4', 'd5'].forEach((d, i) => add(d, ['c' + (i + 1)]));
            const rowsEl = document.createElement('div');
            rowsEl.style.cssText = 'position:relative;display:flex;flex-direction:column;gap:46px';
            const svgEl = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            const canvas = document.createElement('div');
            canvas.style.cssText = 'position:absolute;left:-9999px;top:0;width:max-content';
            canvas.append(rowsEl, svgEl);
            document.body.appendChild(canvas);
            renderLineageGraph(runs, { rowsEl, svgEl, emptyEl: null, direction: 'asc',
                onNodeClick: () => {}, nodeRenderer: (r) => r.job_name });
            const box = (id) => rowsEl.querySelector(`[data-run-id="${id}"]`);
            // the tightest gap, over the rows both tracks occupy, from the
            // chain's right edge to whatever sits next to it in that row
            const nextTo = [['a1', 'b1'], ['a2', 'b2'], ['a3', 'c1']];
            const g = Math.min(...nextTo.map(([a, b]) => box(b).offsetLeft - (box(a).offsetLeft + box(a).offsetWidth)));
            canvas.remove();
            return g;
        }""")
        check(f"An unconnected chain tucks in beside the next track ({gap}px at the closest row)", 20 <= gap <= 60)

        # ---- no staircases: linked boxes are in the same column or clearly not ----
        # A pipeline shaped like a real tomography project (four tilt-series
        # tracks feeding one processing pipeline with side branches and long
        # connections). Boxes a few pixels off the one above or below read as a
        # staircase, so any two linked boxes in neighbouring rows must either
        # share a column exactly or be at least a column-width-ish apart.
        stairs = page.evaluate("""() => {
            const E = {
              j001: [], j005: ['j001'], j009: ['j005'], j014: ['j009'], j017: ['j014'], j019: ['j017'],
              j000: [], j004: ['j000'], j008: ['j004'], j011: ['j008'], j016: ['j011'], j018: ['j016'], j027m: ['j000'],
              j003: [], j007: ['j003'], j010: ['j007'], j020: ['j010'], j024: ['j020'], j021: ['j024'],
              j002: [], j012: ['j002'], j006: ['j012'], j013: ['j006'], j015: ['j013'], j022: ['j015'], j023: ['j022'],
              j025: ['j018'], j026: ['j018'], j033: ['j018'], j027: ['j026'], j034: ['j033'],
              j028: ['j025', 'j027'], j035: ['j034'], j036: ['j035'], j037: ['j036'], j038: ['j037'],
              j030: ['j028'], j039: ['j028'], j031: ['j030', 'j028'], j040: ['j039'],
              j032: ['j031', 'j027'], j046: ['j040', 'j039'], j041: ['j046', 'j018', 'j032'], j029: ['j032'],
              j042: ['j041'], j047: ['j041', 'j029', 'j018'],
            };
            const runs = Object.keys(E).map((k, i) => ({ run_id: k, job_number: i + 1, job_name: k, internal_name: 'x',
                input_links: E[k].map((p) => ({ run_id: p })) }));
            const rowsEl = document.createElement('div');
            rowsEl.style.cssText = 'position:relative;display:flex;flex-direction:column;gap:46px';
            const svgEl = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            const canvas = document.createElement('div');
            canvas.style.cssText = 'position:absolute;left:-9999px;top:0;width:max-content';
            canvas.append(rowsEl, svgEl);
            document.body.appendChild(canvas);
            renderLineageGraph(runs, { rowsEl, svgEl, emptyEl: null, direction: 'asc',
                onNodeClick: () => {}, nodeRenderer: (r) => r.job_name });
            const box = (id) => rowsEl.querySelector(`[data-run-id="${id}"]`);
            const top = (id) => box(id).offsetTop;
            let links = 0, stepped = [];
            runs.forEach((r) => r.input_links.forEach((l) => {
                if (Math.abs(top(r.run_id) - top(l.run_id)) > 130) return;   // neighbouring rows only
                links++;
                const dx = Math.abs(box(r.run_id).offsetLeft - box(l.run_id).offsetLeft);
                if (dx > 1.5 && dx < 40) stepped.push(l.run_id + '>' + r.run_id + ':' + dx);
            }));
            canvas.remove();
            return { links, stepped };
        }""")
        check(f"No stair-stepping across {stairs['links']} neighbouring-row links ({stairs['stepped']})",
              not stairs["stepped"])

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
