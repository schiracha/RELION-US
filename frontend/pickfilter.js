// Filter Picks tuner (backend/pick_filter.py): choose which picks to keep by a numeric
// column of a particles STAR file -- a column dropdown (RELION's pick score first),
// a range with a live histogram, per-image counts, and a preview of one image with its
// picks drawn kept / excluded -- then write the selection into the job.
//
// What is written is RELION's own "Select on value" (relion_star_handler --select), so the
// job is a real Select job in RELION's pipeline. This window only chooses label / min /
// max. Counts and the preview classify a pick exactly as RELION does, including its 32-bit
// bounds (Math.fround), so what is shown is what will be written.
//
// Opened from a Filter Picks job's "Open Tuner" button (app.js openJobPopup). Works for
// tomography picks (rlnTomoName; the tomograms STAR in the job's preview field says which
// volume each is) and single-particle picks (rlnMicrographName).
(function () {
  "use strict";

  const KEPT = "#39d353";
  const EXCLUDED = "#e5484d";
  const open = new Map();                 // runId -> state, one tuner per job

  const fmt = (x) => (x === null || x === undefined || Number.isNaN(x)) ? "—"
    : (Math.abs(x) >= 1e5 || (x !== 0 && Math.abs(x) < 1e-3) ? x.toExponential(2) : String(+x.toPrecision(4)));
  const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
  const num = (n) => Number(n).toLocaleString();

  function messageOf(err) {
    const m = String((err && err.message) || err);
    const detail = m.match(/"detail"\s*:\s*"([^"]*)"/);
    return detail ? detail[1] : m;
  }

  // RELION compares against 32-bit bounds (see backend/pick_filter.py _keep_mask).
  function keeps(value, lo, hi) {
    return value !== null && value >= Math.fround(lo) && value <= Math.fround(hi);
  }

  // A bound just outside the data, to a few significant figures, so "keep everything" is a
  // short number that still lies beyond the extreme.
  function roundOut(x, down) {
    if (x === 0) return 0;
    const step = Math.pow(10, Math.floor(Math.log10(Math.abs(x))) - 3);
    return (down ? Math.floor(x / step) : Math.ceil(x / step)) * step;
  }

  async function openPickFilterTuner({ runId, jobName }) {
    if (open.has(runId)) {
      const st = open.get(runId);
      try { st.win.restore(); st.win.focus(); } catch (e) { /* already normal */ }
      return st;
    }
    const body = document.createElement("div");
    body.className = "pf-popup";
    body.innerHTML = `
      <aside class="viz-side pf-side">
        <div class="viz-side-group">
          <label class="viz-field">
            <span class="viz-field-label">Filter on</span>
            <select class="viz-input-sm" data-role="pf-col"></select>
          </label>
          <div class="viz-hint" data-role="pf-colinfo"></div>
        </div>
        <div class="viz-side-group">
          <div class="viz-side-title">Keep picks between</div>
          <div class="viz-ctrl-row"><span class="viz-ctrl-key">Min</span>
            <input type="range" data-role="pf-lo-range" /><input type="number" class="viz-input-sm pf-num" data-role="pf-lo" step="any" /></div>
          <div class="viz-ctrl-row"><span class="viz-ctrl-key">Max</span>
            <input type="range" data-role="pf-hi-range" /><input type="number" class="viz-input-sm pf-num" data-role="pf-hi" step="any" /></div>
          <div class="viz-inputs-row"><button class="btn btn-sm" data-role="pf-reset" title="Keep every pick">Keep all</button>
            <button class="btn btn-sm" data-role="pf-reset-job" title="Go back to what the job currently holds">Revert</button></div>
        </div>
        <div class="viz-side-group">
          <div class="pf-count" data-role="pf-count">…</div>
          <div class="viz-hint" data-role="pf-count-sub"></div>
          <button class="btn primary" data-role="pf-apply" title="Write this selection into the job's particles.star">Apply to job</button>
          <div class="status-line viz-status" data-role="pf-status"></div>
        </div>
        <div class="viz-side-group pf-images-group">
          <div class="viz-side-title">Images <span class="viz-hint" data-role="pf-images-meta"></span></div>
          <input type="text" class="viz-input-sm" data-role="pf-image-filter" placeholder="filter names…" />
          <div class="pf-images" data-role="pf-images"></div>
        </div>
      </aside>
      <div class="pf-main">
        <div class="pf-histbox">
          <canvas data-role="pf-hist"></canvas>
          <label class="vol3d-check pf-log"><input type="checkbox" data-role="pf-log" /> log counts</label>
        </div>
        <div class="pf-previewbox">
          <div class="pf-previewbar">
            <span class="pf-imgname" data-role="pf-imgname">no image selected</span>
            <label class="pf-ctl" data-role="pf-z-wrap" hidden>z <input type="range" data-role="pf-z" /> <span data-role="pf-z-val"></span></label>
            <label class="pf-ctl" data-role="pf-slab-wrap" hidden>± <input type="number" class="viz-input-sm pf-num" data-role="pf-slab" value="12" min="0" title="Draw picks within this many voxels of the slice" /></label>
            <label class="pf-ctl">size <input type="range" data-role="pf-size" min="2" max="24" value="7" /></label>
            <label class="pf-ctl"><input type="checkbox" data-role="pf-show-ex" checked /> excluded</label>
            <span class="pf-legend"><i style="background:${KEPT}"></i> kept <i style="background:${EXCLUDED}"></i> excluded</span>
          </div>
          <div class="pf-stage" data-role="pf-stage">
            <img data-role="pf-img" alt="" hidden />
            <canvas data-role="pf-overlay"></canvas>
            <div class="pf-tip" data-role="pf-tip" hidden></div>
            <div class="pf-empty" data-role="pf-empty">Pick an image from the list to see its picks.</div>
          </div>
          <div class="viz-hint" data-role="pf-previewinfo"></div>
        </div>
      </div>`;

    const q = (role) => body.querySelector(`[data-role="${role}"]`);
    const st = {
      q, runId, win: null, body, info: null, column: null, domain: [0, 1], lo: 0, hi: 1, integer: false,
      applied: null, dirty: false, image: null, imageData: null, hist: null, counts: null,
      zIndex: 0, view: null, picksInView: [], timers: {},
    };
    st.win = new WinBox({
      title: `Filter Picks — ${jobName || runId}`, width: "94%", height: "92%", x: "center", y: "center",
      mount: body, class: ["no-full", "pf-winbox"],
      onresize: () => { drawHist(st); layoutOverlay(st); },
      onclose: (forced) => {
        if (st.dirty && forced !== true) {
          confirmDialog("This selection has not been applied to the job. Close the tuner and discard it?",
            { confirmLabel: "Discard", danger: true }).then((ok) => { if (ok) { st.dirty = false; st.win.close(true); } });
          return true;                      // keep it open until the user says
        }
        open.delete(runId);
        return false;
      },
    });
    open.set(runId, st);

    // Wired before anything is awaited, so an edit made while the first data is still loading is not lost.
    const sel = q("pf-col");
    sel.addEventListener("change", () => { if (st.info) selectColumn(st, sel.value, false); });
    q("pf-lo").addEventListener("input", () => setRange(st, Number(q("pf-lo").value), st.hi, "lo"));
    q("pf-hi").addEventListener("input", () => setRange(st, st.lo, Number(q("pf-hi").value), "hi"));
    q("pf-lo-range").addEventListener("input", (e) => setRange(st, Number(e.target.value), st.hi, "lo"));
    q("pf-hi-range").addEventListener("input", (e) => setRange(st, st.lo, Number(e.target.value), "hi"));
    q("pf-reset").addEventListener("click", () => setRange(st, st.domain[0], st.domain[1]));
    q("pf-reset-job").addEventListener("click", () => {
      if (st.applied && st.applied.column === st.column) setRange(st, st.applied.lo, st.applied.hi);
      else setRange(st, st.domain[0], st.domain[1]);
    });
    q("pf-apply").addEventListener("click", () => apply(st));
    q("pf-log").addEventListener("change", () => drawHist(st));
    q("pf-image-filter").addEventListener("input", () => renderImages(st));
    ["pf-z", "pf-slab", "pf-size", "pf-show-ex"].forEach((r) => q(r).addEventListener("input", () => {
      if (r === "pf-z") loadSlice(st); else drawOverlay(st);
    }));
    wireHistDrag(st);
    wireTip(st);

    try {
      st.info = await api(`/api/pick-filter/${runId}/info`);
    } catch (err) {
      q("pf-count").textContent = "Could not read the picks file";
      q("pf-status").textContent = messageOf(err);
      q("pf-status").classList.add("error");
      return st;
    }
    const groups = {};
    st.info.columns.forEach((c) => { (groups[c.group] = groups[c.group] || []).push(c); });
    sel.innerHTML = Object.entries(groups).map(([g, cols]) => `<optgroup label="${escapeHtml(g)}">${
      cols.map((c) => `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}  (${fmt(c.min)} – ${fmt(c.max)})</option>`).join("")}</optgroup>`).join("");
    const current = st.info.current || {};
    const startCol = st.info.columns.some((c) => c.name === current.column) ? current.column : st.info.default;
    sel.value = startCol;
    st.applied = null;
    await selectColumn(st, startCol, true);
    q("pf-images-meta").textContent = `${num(st.info.n_images)} ${st.info.kind === "tomo" ? "tomograms" : "micrographs"} · ${num(st.info.n_rows)} picks`;

    return st;
  }

  // ---- column and range --------------------------------------------------

  async function selectColumn(st, column, first) {
    const q = st.q;
    st.column = column;
    const info = st.info.columns.find((c) => c.name === column);
    st.integer = info.integer;
    const span = (info.max - info.min) || 1;
    const pad = st.integer ? 0.5 : 0.01 * span;
    st.domain = [roundOut(info.min - pad, true), roundOut(info.max + pad, false)];
    q("pf-colinfo").textContent = `${num(info.n_unique)} distinct values · mean ${fmt(info.mean)} · σ ${fmt(info.std)}`;
    const step = st.integer ? 1 : (st.domain[1] - st.domain[0]) / 1000;
    ["pf-lo-range", "pf-hi-range"].forEach((r) => { q(r).min = st.domain[0]; q(r).max = st.domain[1]; q(r).step = step; });
    const cur = st.info.current || {};
    if (first && cur.column === column && cur.min !== undefined && cur.min !== null && cur.min !== ""
        && Number.isFinite(Number(cur.min)) && Number.isFinite(Number(cur.max))) {
      // A bound beyond the data (RELION's -9999 / 9999 defaults) is "keep everything" on this side:
      // shown as the end of the scale rather than a huge number.
      st.lo = Number(cur.min) <= st.domain[0] ? st.domain[0] : Number(cur.min);
      st.hi = Number(cur.max) >= st.domain[1] ? st.domain[1] : Number(cur.max);
      st.applied = { column, lo: st.lo, hi: st.hi, keepAll: st.lo <= st.domain[0] && st.hi >= st.domain[1] };
    } else {
      st.lo = st.domain[0]; st.hi = st.domain[1];
    }
    st.dirty = isDirty(st);
    st.hist = await api(`/api/pick-filter/${st.runId}/histogram?column=${encodeURIComponent(column)}&bins=60`);
    syncInputs(st);
    if (!first) st.q("pf-status").textContent = st.dirty ? "Not applied yet." : "";
    await refreshCounts(st, true);
    drawHist(st);
    if (st.image) await loadImage(st, st.image);
  }

  function syncInputs(st) {
    const q = st.q;
    q("pf-lo").value = String(+st.lo.toPrecision(6)); q("pf-hi").value = String(+st.hi.toPrecision(6));
    q("pf-lo-range").value = st.lo; q("pf-hi-range").value = st.hi;
  }

  // Does the selection on screen differ from what the job holds? Keeping everything counts as
  // the same whatever the column: it selects nothing out.
  function isDirty(st) {
    const a = st.applied;
    if (!a) return false;
    const keepAll = st.lo <= st.domain[0] && st.hi >= st.domain[1];
    if (keepAll && a.keepAll) return false;
    return !(a.column === st.column && a.lo === st.lo && a.hi === st.hi);
  }

  function setRange(st, lo, hi, moved) {
    if (!st.hist || !Number.isFinite(lo) || !Number.isFinite(hi)) return;      // not loaded yet
    if (lo > hi) { if (moved === "lo") lo = hi; else hi = lo; }
    st.lo = lo; st.hi = hi;
    st.dirty = isDirty(st);
    syncInputs(st);
    drawHist(st);
    drawOverlay(st);
    clearTimeout(st.timers.counts);
    st.timers.counts = setTimeout(() => refreshCounts(st), 120);
    st.q("pf-status").textContent = st.dirty ? "Not applied yet." : "";
    st.q("pf-status").classList.remove("error");
  }

  async function refreshCounts(st, selectFirstImage) {
    const col = st.column, lo = st.lo, hi = st.hi;
    try {
      const c = await api(`/api/pick-filter/${st.runId}/counts?column=${encodeURIComponent(col)}&lo=${lo}&hi=${hi}`);
      if (col !== st.column || lo !== st.lo || hi !== st.hi) return;        // superseded while waiting
      st.counts = c;
    } catch (err) {
      st.q("pf-count").textContent = messageOf(err);
      return;
    }
    const { kept, total } = st.counts;
    st.q("pf-count").textContent = `${num(kept)} of ${num(total)} kept (${pct(kept, total)}%)`;
    st.q("pf-count-sub").textContent = `${num(total - kept)} excluded`;
    renderImages(st);
    if (selectFirstImage && !st.image && st.counts.images.length) {
      // the biggest image that can be drawn, else the biggest
      const pool = st.info.previewable
        ? st.counts.images.filter((i) => st.info.previewable.includes(i.name)) : st.counts.images;
      const from = pool.length ? pool : st.counts.images;
      loadImage(st, from.reduce((a, b) => (b.total > a.total ? b : a)).name);
    }
  }

  function renderImages(st) {
    const host = st.q("pf-images");
    if (!st.counts) return;
    const needle = st.q("pf-image-filter").value.trim().toLowerCase();
    const rows = st.counts.images.filter((i) => !needle || i.name.toLowerCase().includes(needle));
    const drawable = (name) => !st.info.previewable || st.info.previewable.includes(name);
    host.innerHTML = rows.map((i) => `
      <div class="pf-image-row${i.name === st.image ? " selected" : ""}${drawable(i.name) ? "" : " pf-nopreview"}" data-name="${escapeHtml(i.name)}"
           title="${escapeHtml(i.name)}${drawable(i.name) ? "" : " — no volume to draw on (picks are shown as a scatter)"}">
        <span class="pf-image-name">${escapeHtml(i.name.split("/").pop())}</span>
        <span class="pf-image-count">${num(i.kept)} / ${num(i.total)}</span>
        <span class="pf-image-bar"><i style="width:${pct(i.kept, i.total)}%"></i></span>
      </div>`).join("") || '<div class="viz-hint">No images.</div>';
    host.querySelectorAll(".pf-image-row").forEach((el) => el.addEventListener("click", () => loadImage(st, el.dataset.name)));
  }

  async function apply(st) {
    if (!st.column) return;                                  // nothing loaded yet
    const btn = st.q("pf-apply");
    btn.disabled = true;
    const status = st.q("pf-status");
    status.classList.remove("error");
    status.textContent = "Writing…";
    try {
      const r = await api(`/api/pick-filter/${st.runId}/apply`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ column: st.column, lo: st.lo, hi: st.hi }),
      });
      st.applied = { column: st.column, lo: st.lo, hi: st.hi, keepAll: st.lo <= st.domain[0] && st.hi >= st.domain[1] };
      st.dirty = false;
      st.info.current = { column: st.column, min: String(st.lo), max: String(st.hi) };
      status.textContent = `Applied: ${num(r.kept)} of ${num(r.total)} picks written to the job.`;
      if (typeof refreshCommandCenter === "function") refreshCommandCenter();
    } catch (err) {
      status.textContent = messageOf(err);
      status.classList.add("error");
    } finally {
      btn.disabled = false;
    }
  }

  // ---- histogram -----------------------------------------------------------

  function sizeCanvas(canvas) {
    const r = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    const w = Math.max(10, Math.round(r.width * ratio)), h = Math.max(10, Math.round(r.height * ratio));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    return { w, h, ratio };
  }

  function drawHist(st) {
    const canvas = st.q("pf-hist");
    if (!canvas || !st.hist) return;
    const { w, h, ratio } = sizeCanvas(canvas);
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, w, h);
    const css = getComputedStyle(document.documentElement);
    const dim = css.getPropertyValue("--text-dim").trim() || "#9aa4b2";
    const text = css.getPropertyValue("--text").trim() || "#e6e9ee";
    const padL = 40 * ratio, padR = 12 * ratio, padT = 10 * ratio, padB = 24 * ratio;
    const pw = w - padL - padR, ph = h - padT - padB;
    const log = st.q("pf-log").checked;
    const f = (c) => (log ? Math.log10(c + 1) : c);
    const maxC = Math.max(1, ...st.hist.counts.map(f));
    const [d0, d1] = st.domain;
    const X = (v) => padL + ((v - d0) / (d1 - d0 || 1)) * pw;
    st.histMap = { padL, pw, d0, d1, ratio };
    const edges = st.hist.edges;
    st.hist.counts.forEach((c, i) => {
      const x0 = X(edges[i]), x1 = X(edges[i + 1]);
      const bh = (f(c) / maxC) * ph;
      const mid = (edges[i] + edges[i + 1]) / 2;
      ctx.fillStyle = keeps(mid, st.lo, st.hi) ? KEPT : "#59606b";
      ctx.globalAlpha = keeps(mid, st.lo, st.hi) ? 0.9 : 0.55;
      ctx.fillRect(x0, padT + ph - bh, Math.max(1, x1 - x0 - 1), bh);
    });
    ctx.globalAlpha = 1;
    ctx.strokeStyle = dim; ctx.lineWidth = ratio; ctx.beginPath();
    ctx.moveTo(padL, padT + ph); ctx.lineTo(padL + pw, padT + ph); ctx.stroke();
    ctx.fillStyle = dim; ctx.font = `${11 * ratio}px sans-serif`; ctx.textBaseline = "top";
    [0, 0.25, 0.5, 0.75, 1].forEach((t) => {
      const v = d0 + t * (d1 - d0);
      ctx.textAlign = t === 0 ? "left" : t === 1 ? "right" : "center";
      ctx.fillText(fmt(v), X(v), padT + ph + 4 * ratio);
    });
    ctx.textAlign = "right"; ctx.textBaseline = "top";
    ctx.fillText(log ? "log10(count+1)" : `${num(Math.max(...st.hist.counts))} max`, padL - 4 * ratio, padT);
    [st.lo, st.hi].forEach((v) => {
      const x = X(Math.min(Math.max(v, d0), d1));
      ctx.strokeStyle = text; ctx.lineWidth = 2 * ratio; ctx.beginPath();
      ctx.moveTo(x, padT); ctx.lineTo(x, padT + ph); ctx.stroke();
      ctx.fillStyle = text; ctx.fillRect(x - 4 * ratio, padT, 8 * ratio, 8 * ratio);
    });
    if (st.hist.n_missing) {
      ctx.fillStyle = dim; ctx.textAlign = "right"; ctx.textBaseline = "top";
      ctx.fillText(`${num(st.hist.n_missing)} without a value (always excluded)`, w - padR, padT);
    }
  }

  // Drag either bound on the histogram itself.
  function wireHistDrag(st) {
    const canvas = st.q("pf-hist");
    let dragging = null;
    const valueAt = (e) => {
      const m = st.histMap, r = canvas.getBoundingClientRect();
      const x = (e.clientX - r.left) * (canvas.width / r.width);
      const t = Math.min(1, Math.max(0, (x - m.padL) / m.pw));
      let v = m.d0 + t * (m.d1 - m.d0);
      if (st.integer) v = Math.round(v * 2) / 2;
      return +v.toPrecision(5);
    };
    const near = (e) => {
      if (!st.histMap) return null;
      const m = st.histMap, r = canvas.getBoundingClientRect();
      const x = (e.clientX - r.left) * (canvas.width / r.width);
      const px = (v) => m.padL + ((v - m.d0) / (m.d1 - m.d0 || 1)) * m.pw;
      const dLo = Math.abs(px(st.lo) - x), dHi = Math.abs(px(st.hi) - x);
      if (Math.min(dLo, dHi) > 10 * m.ratio) return null;
      return dLo <= dHi ? "lo" : "hi";
    };
    canvas.addEventListener("pointerdown", (e) => {
      dragging = near(e);
      if (dragging) canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener("pointermove", (e) => {
      if (!dragging) { canvas.style.cursor = near(e) ? "ew-resize" : "default"; return; }
      const v = valueAt(e);
      if (dragging === "lo") setRange(st, v, st.hi, "lo"); else setRange(st, st.lo, v, "hi");
    });
    canvas.addEventListener("pointerup", () => { dragging = null; });
  }

  // ---- the image preview ---------------------------------------------------

  async function loadImage(st, name) {
    st.image = name;
    renderImages(st);
    const q = st.q;
    q("pf-imgname").textContent = name.split("/").pop();
    q("pf-previewinfo").textContent = "Loading picks…";
    try {
      st.imageData = await api(`/api/pick-filter/${st.runId}/image?column=${encodeURIComponent(st.column)}&image=${encodeURIComponent(name)}`);
    } catch (err) {
      st.imageData = null;
      q("pf-previewinfo").textContent = messageOf(err);
      return;
    }
    const d = st.imageData;
    const v = d.volume;
    const is3d = !!(v && v.nz > 1);
    const scatter3d = !v && d.z.length > 0;                     // no volume, but the picks have a depth
    q("pf-z-wrap").hidden = !(is3d || scatter3d);
    q("pf-slab-wrap").hidden = !(is3d || scatter3d);
    if (is3d || scatter3d) {
      const zs = d.z.slice().sort((a, b) => a - b);
      const zmin = is3d ? 0 : Math.floor(zs[0]), zmax = is3d ? v.nz - 1 : Math.ceil(zs[zs.length - 1]);
      q("pf-z").min = zmin; q("pf-z").max = zmax;
      st.zIndex = Math.max(zmin, Math.min(zmax, Math.round(zs.length ? zs[zs.length >> 1] : (zmin + zmax) / 2)));
      q("pf-z").value = st.zIndex;
    }
    await loadSlice(st);
  }

  async function loadSlice(st) {
    const q = st.q, d = st.imageData;
    if (!d) return;
    const img = q("pf-img");
    const v = d.volume;
    st.zIndex = Number(q("pf-z").value) || 0;
    q("pf-z-val").textContent = String(st.zIndex);
    if (d.path && v) {
      const url = `/api/viz/slice?mrc_path=${encodeURIComponent(d.path)}&axis=z&index=${v.nz > 1 ? st.zIndex : 0}&max_dim=1100`;
      await new Promise((resolve) => {
        img.onload = img.onerror = resolve;
        img.hidden = false;
        img.src = url;
      });
      st.view = { w: v.nx, h: v.ny, x0: 0, y0: 0 };
    } else {
      img.hidden = true;
      img.removeAttribute("src");
      const xs = d.x, ys = d.y;
      if (!xs.length) { st.view = null; }
      else {
        const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
        const px = (x1 - x0) * 0.04 || 1, py = (y1 - y0) * 0.04 || 1;
        st.view = { w: x1 - x0 + 2 * px, h: y1 - y0 + 2 * py, x0: x0 - px, y0: y0 - py };
      }
    }
    q("pf-empty").hidden = !!st.view;
    q("pf-empty").textContent = d.message || "Nothing to draw for this image.";
    layoutOverlay(st);
  }

  function layoutOverlay(st) {
    const stage = st.q("pf-stage"), canvas = st.q("pf-overlay"), img = st.q("pf-img");
    if (!st.view) { canvas.width = canvas.height = 0; return; }
    const box = stage.getBoundingClientRect();
    const scale = Math.min(box.width / st.view.w, box.height / st.view.h);
    const cw = Math.max(10, Math.floor(st.view.w * scale)), ch = Math.max(10, Math.floor(st.view.h * scale));
    canvas.style.width = img.style.width = `${cw}px`;
    canvas.style.height = img.style.height = `${ch}px`;
    canvas.style.left = img.style.left = `${Math.floor((box.width - cw) / 2)}px`;
    canvas.style.top = img.style.top = `${Math.floor((box.height - ch) / 2)}px`;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(cw * ratio); canvas.height = Math.round(ch * ratio);
    st.scale = (cw / st.view.w);
    st.ratio = ratio;
    drawOverlay(st);
  }

  function drawOverlay(st) {
    const canvas = st.q("pf-overlay"), d = st.imageData;
    if (!d || !st.view || !canvas.width) return;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const showEx = st.q("pf-show-ex").checked;
    const r = Number(st.q("pf-size").value) * st.ratio;
    const slab = Number(st.q("pf-slab").value) || 0;
    const is3d = !!(d.volume && d.volume.nz > 1) || (!d.volume && d.z.length > 0);
    const zc = st.zIndex;
    const sx = st.scale * st.ratio;
    let kept = 0, excluded = 0, inView = 0;
    st.picksInView = [];
    ctx.lineWidth = 1.6 * st.ratio;
    for (let i = 0; i < d.x.length; i++) {
      if (is3d && d.z.length && Math.abs(d.z[i] - zc) > slab) continue;
      const k = keeps(d.value[i], st.lo, st.hi);
      inView++;
      if (k) kept++; else excluded++;
      if (!k && !showEx) continue;
      const x = (d.x[i] - st.view.x0) * sx, y = (d.y[i] - st.view.y0) * sx;
      st.picksInView.push({ x, y, value: d.value[i], kept: k });
      ctx.strokeStyle = k ? KEPT : EXCLUDED;
      ctx.globalAlpha = k ? 0.95 : 0.7;
      ctx.beginPath();
      if (k) ctx.arc(x, y, r, 0, 2 * Math.PI);
      else { ctx.moveTo(x - r * 0.7, y - r * 0.7); ctx.lineTo(x + r * 0.7, y + r * 0.7); ctx.moveTo(x + r * 0.7, y - r * 0.7); ctx.lineTo(x - r * 0.7, y + r * 0.7); }
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    const where = is3d ? ` in slice z=${zc} (±${slab})` : "";
    st.q("pf-previewinfo").textContent =
      `${num(inView)} picks${where}: ${num(kept)} kept, ${num(excluded)} excluded · all ${num(d.n)} picks of this image: ` +
      `${num(d.value.filter((v) => keeps(v, st.lo, st.hi)).length)} kept`;
  }

  // The value of the pick under the cursor.
  function wireTip(st) {
    const canvas = st.q("pf-overlay"), tip = st.q("pf-tip");
    canvas.addEventListener("mousemove", (e) => {
      const rect = canvas.getBoundingClientRect();
      const x = (e.clientX - rect.left) * (canvas.width / rect.width), y = (e.clientY - rect.top) * (canvas.height / rect.height);
      let best = null, bd = (10 * st.ratio) ** 2;
      for (const p of st.picksInView) {
        const dd = (p.x - x) ** 2 + (p.y - y) ** 2;
        if (dd < bd) { bd = dd; best = p; }
      }
      if (!best) { tip.hidden = true; return; }
      tip.hidden = false;
      tip.textContent = `${st.column} ${fmt(best.value)} — ${best.kept ? "kept" : "excluded"}`;
      const box = st.q("pf-stage").getBoundingClientRect();
      tip.style.left = `${e.clientX - box.left + 12}px`;
      tip.style.top = `${e.clientY - box.top + 12}px`;
    });
    canvas.addEventListener("mouseleave", () => { tip.hidden = true; });
  }

  window.openPickFilterTuner = openPickFilterTuner;
})();
