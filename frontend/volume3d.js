// 3D volume viewer -- Mol* (vendor/molstar.js) in its own window, separate from
// the tomogram / pick viewer (openVisualizer in app.js), which stays the place
// for slices, contrast and picking.
//
// The browser can only render what it downloads, so maps come from the backend
// as a block-averaged copy (backend/volume3d.py: at most `detail` voxels along
// the longest side, voxel size scaled to match, cached). A refined map of
// 100-300 voxels is served unchanged; a gigabyte tomogram is shown coarsely.
//
// Every map is a Mol* isosurface that this window drives itself (contour,
// colour, opacity, mesh/solid, show/hide, remove) through the plugin's state
// tree, so the controls are the few a cryo-EM user wants. Mol*'s own panels
// (measurements, structure tools, ...) are hidden but one tick away.
//
// Loaded lazily: vendor/molstar.js is ~5 MB and most sessions never need it.
// Everything is served from this app (no CDN), so it works offline on a cluster.
(function () {
  "use strict";

  const MOLSTAR_JS = "vendor/molstar.js";
  const MOLSTAR_CSS = "vendor/molstar.css";
  const PALETTE = [0x4aa3ff, 0xf5a623, 0x39d353, 0xe5484d, 0xb07cff, 0x2fd6d0];   // = the picker's class colours
  const DETAILS = [[128, "Fast (128)"], [192, "Balanced (192)"], [256, "Detailed (256)"], [384, "Fine (384, slow)"]];
  const DEFAULT_DETAIL = 192;
  const DEFAULT_SIGMA = 1.5;            // first contour for a density map, in standard deviations above the mean
  const BG_DARK = 0x14161a;
  const BG_LIGHT = 0xfcfcfb;
  const PANELS_HIDDEN = { left: "hidden", right: "hidden", top: "hidden", bottom: "hidden" };
  const PANELS_SHOWN = { left: "collapsed", right: "full", top: "hidden", bottom: "hidden" };

  let molstarLoading = null;
  let current = null;                   // the one open window: { win, viewer, maps, ... }

  function loadMolstar() {
    if (window.molstar && window.molstar.Viewer) return Promise.resolve();
    if (molstarLoading) return molstarLoading;
    molstarLoading = new Promise((resolve, reject) => {
      const css = document.createElement("link");
      css.rel = "stylesheet";
      css.href = MOLSTAR_CSS;
      document.head.appendChild(css);
      const script = document.createElement("script");
      script.src = MOLSTAR_JS;
      script.onload = () => resolve();
      script.onerror = () => {
        molstarLoading = null;
        script.remove();
        css.remove();
        reject(new Error("Could not load the Mol* viewer files (" + MOLSTAR_JS + ")."));
      };
      document.head.appendChild(script);
    });
    return molstarLoading;
  }

  function messageOf(err) {
    const m = String((err && err.message) || err);
    const detail = m.match(/"detail"\s*:\s*"([^"]*)"/);
    return detail ? detail[1] : m;
  }

  const hex = (n) => "#" + n.toString(16).padStart(6, "0");
  const fmt = (x) => (Math.abs(x) >= 1e4 || (x !== 0 && Math.abs(x) < 1e-3) ? x.toExponential(2) : String(+x.toPrecision(3)));

  // ---- window ----------------------------------------------------------

  async function openVolumeViewer(path) {
    if (current) {
      try { current.win.restore(); current.win.focus(); } catch (e) { /* already normal */ }
      if (path) await addMap(current, path);
      return current;
    }
    const body = document.createElement("div");
    body.className = "vol3d-popup";
    body.innerHTML = `
      <aside class="viz-side vol3d-side">
        <div class="viz-side-group">
          <label class="viz-field">
            <span class="viz-field-label">Volume (.mrc)</span>
            <span class="viz-input-row">
              <input type="text" class="viz-input-sm" data-role="v3-path" placeholder="Refine3D/job010/run_class001.mrc" />
              <button type="button" class="btn btn-icon" data-role="v3-browse" title="Browse for an MRC file on the machine running the backend">…</button>
            </span>
          </label>
          <div class="viz-inputs-row">
            <button class="btn primary btn-sm" data-role="v3-add">Add</button>
            <select class="viz-input-sm" data-role="v3-detail" title="Largest side of the volume sent to the browser, in voxels. Bigger volumes are averaged down to this.">
              ${DETAILS.map(([n, label]) => `<option value="${n}"${n === DEFAULT_DETAIL ? " selected" : ""}>${label}</option>`).join("")}
            </select>
          </div>
          <div class="status-line viz-status" data-role="v3-status"></div>
        </div>
        <div class="vol3d-maps" data-role="v3-maps"></div>
        <div class="viz-side-group vol3d-global" data-role="v3-global" hidden>
          <div class="viz-side-title">View</div>
          <div class="viz-inputs-row">
            <button class="btn btn-sm" data-role="v3-reset" title="Fit all maps in the view">Reset view</button>
            <button class="btn btn-sm" data-role="v3-shot" title="Save the current view as a PNG">Screenshot</button>
          </div>
          <label class="vol3d-check"><input type="checkbox" data-role="v3-light" /> Light background</label>
          <label class="vol3d-check" title="Mol*'s own tools: measurements, extra representations, clipping, state tree"><input type="checkbox" data-role="v3-advanced" /> Show Mol* panels</label>
        </div>
        <div class="viz-hint vol3d-hint">
          Drag to rotate, scroll to zoom, right-drag to pan. Large volumes are averaged down to the detail level above, so
          fine features can disappear; use the slice viewer for full resolution.
        </div>
      </aside>
      <div class="vol3d-stage" data-role="v3-stage"></div>`;

    const state = { win: null, viewer: null, maps: [], nextId: 1, body, detail: DEFAULT_DETAIL, busy: false };
    state.win = new WinBox({
      title: "3D volume viewer", width: "86%", height: "86%", x: "center", y: "center",
      mount: body, class: ["no-full", "vol3d-winbox"],
      onresize: () => { if (state.viewer) state.viewer.handleResize(); },
      onclose: () => {
        try { if (state.viewer) state.viewer.dispose(); } catch (e) { /* tearing down */ }
        state.viewer = null;
        current = null;
        return false;
      },
    });
    current = state;

    const q = (role) => body.querySelector(`[data-role="${role}"]`);
    q("v3-add").addEventListener("click", () => addFromInput(state));
    q("v3-path").addEventListener("keydown", (e) => { if (e.key === "Enter") addFromInput(state); });
    q("v3-browse").addEventListener("click", async () => {
      const picked = await pickFileDialog({
        title: "Select a volume (MRC)", extensions: [".mrc", ".rec", ".map", ".ali"],
        startPath: typeof currentDirOf === "function" ? currentDirOf(q("v3-path").value) : null,
      });
      if (picked) { q("v3-path").value = picked; addFromInput(state); }
    });
    q("v3-detail").addEventListener("change", (e) => { state.detail = Number(e.target.value); });
    q("v3-reset").addEventListener("click", () => state.viewer && state.viewer.plugin.managers.camera.reset());
    q("v3-shot").addEventListener("click", () => screenshot(state));
    q("v3-light").addEventListener("change", (e) => setBackground(state, e.target.checked));
    q("v3-advanced").addEventListener("change", (e) => setPanels(state, e.target.checked ? PANELS_SHOWN : PANELS_HIDDEN));

    status(state, "Loading the 3D viewer…");
    try {
      await loadMolstar();
      state.viewer = await window.molstar.Viewer.create(q("v3-stage"), {
        layoutIsExpanded: false, layoutShowControls: true, layoutShowRemoteState: false, layoutShowSequence: false,
        layoutShowLog: false, layoutShowLeftPanel: true, viewportShowExpand: false, viewportShowSelectionMode: false,
        viewportShowAnimation: false,
      });
      await setPanels(state, PANELS_HIDDEN);
      setBackground(state, document.documentElement.getAttribute("data-theme") === "light");
      status(state, "");
    } catch (err) {
      status(state, "");
      const webgl = /webgl/i.test(String(err && err.message));
      body.querySelector('[data-role="v3-stage"]').innerHTML = `<div class="vol3d-fail">${
        escapeHtml(webgl ? "This browser could not start WebGL, which the 3D viewer needs." : messageOf(err))}</div>`;
      return state;
    }
    if (path) await addMap(state, path);
    return state;
  }

  function status(state, text, isError) {
    const el = state.body.querySelector('[data-role="v3-status"]');
    el.textContent = text || "";
    el.classList.toggle("error", !!isError);
  }

  // Mol*'s panels only redraw when their state actually CHANGES, and a fresh
  // viewer reports "hidden" while still drawing them, so go through "collapsed".
  async function setPanels(state, target) {
    const layout = state.viewer.plugin.layout;
    layout.setProps({ regionState: { left: "collapsed", right: "collapsed", top: "collapsed", bottom: "collapsed" } });
    await new Promise((r) => setTimeout(r, 60));
    layout.setProps({ regionState: target });
    state.viewer.handleResize();
  }

  function setBackground(state, light) {
    state.viewer.plugin.canvas3d.setProps({ renderer: { backgroundColor: light ? BG_LIGHT : BG_DARK } });
  }

  async function screenshot(state) {
    try {
      const helper = state.viewer.plugin.helpers.viewportScreenshot;
      if (helper && helper.download) { helper.download(); return; }
    } catch (e) { /* fall through to the canvas */ }
    const canvas = state.body.querySelector("canvas");
    if (!canvas) return;
    state.viewer.plugin.canvas3d.commit(true);
    const a = document.createElement("a");
    a.href = canvas.toDataURL("image/png");
    a.download = "volume.png";
    a.click();
  }

  // ---- maps --------------------------------------------------------------

  function addFromInput(state) {
    const input = state.body.querySelector('[data-role="v3-path"]');
    const value = input.value.trim();
    if (!value) { status(state, "Enter or browse for a volume first.", true); return; }
    return addMap(state, value);
  }

  async function addMap(state, path) {
    if (!state.viewer) return;
    if (state.busy) { status(state, "Still loading the previous volume…"); return; }
    state.busy = true;
    const detail = state.detail;
    status(state, `Preparing ${path.split("/").pop()} …`);
    try {
      const enc = encodeURIComponent(path);
      const info = await api(`/api/viz/volume3d/info?mrc_path=${enc}&max_dim=${detail}`);
      const color = PALETTE[(state.nextId - 1) % PALETTE.length];
      const plugin = state.viewer.plugin;
      const before = new Set(plugin.state.data.cells.keys());
      await state.viewer.loadVolumeFromUrl(
        { url: `/api/viz/volume3d/file?mrc_path=${enc}&max_dim=${detail}`, format: "ccp4", isBinary: true },
        [info.binary
          ? { type: "absolute", value: 0.5, color, alpha: 0.8 }
          : { type: "relative", value: DEFAULT_SIGMA, color, alpha: 0.8 }]);
      const refs = {};
      plugin.state.data.cells.forEach((cell, key) => {
        if (before.has(key)) return;
        const id = cell.transform.transformer.id;
        if (id === "ms-plugin.download") refs.download = key;
        else if (id === "ms-plugin.volume-from-ccp4") refs.volume = key;
        else if (id === "ms-plugin.volume-representation-3d") refs.rep = key;
      });
      if (!refs.download || !refs.volume || !refs.rep) throw new Error("The volume loaded but could not be found in the viewer.");
      const stats = plugin.state.data.cells.get(refs.volume).obj.data.grid.stats;
      const map = {
        id: state.nextId++, path, info, refs, color, alpha: 0.8, style: "solid", visible: true,
        stats: { min: stats.min, max: stats.max, mean: stats.mean, sigma: stats.sigma },
        mode: info.binary ? "abs" : "sigma", value: info.binary ? 0.5 : DEFAULT_SIGMA,
        pending: null, running: false,
      };
      state.maps.push(map);
      renderCard(state, map);
      state.body.querySelector('[data-role="v3-global"]').hidden = false;
      if (state.maps.length === 1) plugin.managers.camera.reset();
      status(state, "");
    } catch (err) {
      status(state, messageOf(err), true);
    } finally {
      state.busy = false;
    }
  }

  // Updates are serialised and coalesced: dragging a slider fires many changes,
  // and each one re-extracts the surface, so only the latest value is applied
  // once the one in flight is done.
  function apply(state, map, change) {
    map.pending = Object.assign(map.pending || {}, change);
    if (map.running) return;
    map.running = true;
    (async () => {
      try {
        while (map.pending) {
          const c = map.pending;
          map.pending = null;
          if (c.visible !== undefined) {
            state.viewer.plugin.state.data.updateCellState(map.refs.rep, { isHidden: !c.visible });
          }
          const touchesRep = ["mode", "value", "color", "alpha", "style"].some((k) => c[k] !== undefined);
          if (touchesRep) {
            await state.viewer.plugin.build().to(map.refs.rep).update((p) => {
              if (c.mode !== undefined || c.value !== undefined) {
                p.type.params.isoValue = map.mode === "sigma"
                  ? { kind: "relative", relativeValue: map.value }
                  : { kind: "absolute", absoluteValue: map.value };
              }
              if (c.alpha !== undefined) p.type.params.alpha = map.alpha;
              if (c.style !== undefined) p.type.params.visuals = [map.style];
              if (c.color !== undefined) p.colorTheme = { name: "uniform", params: { value: map.color, lightness: 0, saturation: 0 } };
            }).commit();
          }
        }
      } catch (err) {
        status(state, messageOf(err), true);
      } finally {
        map.running = false;
      }
    })();
  }

  function contourRange(map) {
    const { min, max, mean, sigma } = map.stats;
    if (map.mode === "sigma") return { lo: 0, hi: Math.max(1, (max - mean) / (sigma || 1)) * 1.02, step: 0.01 };
    return { lo: min, hi: max, step: (max - min) / 500 || 0.001 };
  }

  function contourLabel(map) {
    const { mean, sigma } = map.stats;
    return map.mode === "sigma"
      ? `${map.value.toFixed(2)} σ  (${fmt(mean + map.value * sigma)})`
      : `${fmt(map.value)}  (${((map.value - mean) / (sigma || 1)).toFixed(2)} σ)`;
  }

  function renderCard(state, map) {
    const holder = state.body.querySelector('[data-role="v3-maps"]');
    const card = document.createElement("div");
    card.className = "viz-side-group vol3d-card";
    const i = map.info;
    const reduced = i.factor > 1
      ? `averaged ×${i.factor} → ${i.shape.join("×")} at ${fmt(i.viewer_voxel_size)} Å/voxel (original ${i.original_shape.join("×")})`
      : `${i.shape.join("×")} voxels at ${fmt(i.voxel_size)} Å/voxel`;
    card.innerHTML = `
      <div class="vol3d-card-head">
        <input type="checkbox" data-role="c-visible" checked title="Show / hide" />
        <input type="color" data-role="c-color" value="${hex(map.color)}" title="Colour" />
        <span class="vol3d-name" title="${escapeHtml(map.path)}">${escapeHtml(i.name)}</span>
        <button class="btn btn-icon" data-role="c-remove" title="Remove this volume from the view">✕</button>
      </div>
      <div class="viz-hint">${escapeHtml(reduced)}${i.binary ? " · mask" : ""}</div>
      <div class="viz-ctrl-row">
        <span class="viz-ctrl-key">Level</span>
        <input type="range" data-role="c-level" />
        <select class="viz-input-sm" data-role="c-mode" title="Contour in standard deviations above the mean, or as an absolute density value">
          <option value="sigma">σ</option><option value="abs">value</option>
        </select>
      </div>
      <div class="vol3d-readout" data-role="c-readout"></div>
      <div class="viz-ctrl-row">
        <span class="viz-ctrl-key">Opacity</span>
        <input type="range" data-role="c-alpha" min="0.1" max="1" step="0.05" value="${map.alpha}" />
        <select class="viz-input-sm" data-role="c-style"><option value="solid">Solid</option><option value="wireframe">Mesh</option></select>
      </div>`;
    holder.appendChild(card);

    const q = (role) => card.querySelector(`[data-role="${role}"]`);
    const level = q("c-level"), readout = q("c-readout"), mode = q("c-mode");
    mode.value = map.mode;
    const syncSlider = () => {
      const r = contourRange(map);
      level.min = r.lo; level.max = r.hi; level.step = r.step;
      level.value = map.value;
      readout.textContent = contourLabel(map);
    };
    syncSlider();

    level.addEventListener("input", () => {
      map.value = Number(level.value);
      readout.textContent = contourLabel(map);
      apply(state, map, { value: map.value });
    });
    mode.addEventListener("change", () => {
      const { mean, sigma } = map.stats;
      map.value = mode.value === "abs" ? mean + map.value * sigma : (map.value - mean) / (sigma || 1);
      map.mode = mode.value;
      syncSlider();
      apply(state, map, { mode: map.mode, value: map.value });
    });
    q("c-alpha").addEventListener("input", (e) => { map.alpha = Number(e.target.value); apply(state, map, { alpha: map.alpha }); });
    q("c-style").addEventListener("change", (e) => { map.style = e.target.value; apply(state, map, { style: map.style }); });
    q("c-color").addEventListener("input", (e) => { map.color = parseInt(e.target.value.slice(1), 16); apply(state, map, { color: map.color }); });
    q("c-visible").addEventListener("change", (e) => { map.visible = e.target.checked; apply(state, map, { visible: map.visible }); });
    q("c-remove").addEventListener("click", async () => {
      try {
        await state.viewer.plugin.build().delete(map.refs.download).commit();
      } catch (err) {
        status(state, messageOf(err), true);
        return;
      }
      state.maps = state.maps.filter((m) => m !== map);
      card.remove();
      if (!state.maps.length) state.body.querySelector('[data-role="v3-global"]').hidden = true;
    });
  }

  window.openVolumeViewer = openVolumeViewer;
})();
