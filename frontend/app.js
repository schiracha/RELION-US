// app.js — RELION-US frontend. Vanilla JS, no build step.
// Each job opened from the sidebar (or reopened from the Command Center)
// becomes an independent WinBox popup — draggable, resizable, minimizable,
// nearly window-filling with rounded corners — mounted with a form built
// from the job definition the backend serves. See style.css for the popup's
// internal layout (standard fields on top, tabs, editable command box, live
// output at the bottom). Only one job popup is open at a time (see
// currentJobWinbox below); opening a new one closes whichever was open.


async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function formatBytes(n) {
  if (n === undefined || n === null) return "";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(1)} ${units[i]}`;
}

function formatTimestamp(t) {
  if (!t) return "—";
  return new Date(t * 1000).toLocaleString();
}

function formatDuration(startedAt, endedAt) {
  if (!startedAt) return "—";
  const end = endedAt || Date.now() / 1000;
  const secs = Math.max(0, Math.round(end - startedAt));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const remSecs = secs % 60;
  if (mins < 60) return `${mins}m ${remSecs}s`;
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  return `${hrs}h ${remMins}m`;
}

// --- Lightweight custom confirm/prompt/error dialogs -----------------------
// Never the browser's native confirm()/prompt()/alert(): those are modal at
// the OS level and block the whole page, including anything driving it
// programmatically (e.g. Playwright). These build a throwaway overlay,
// resolve a promise, and remove themselves.

function statusLineClass(status) {
  if (status === "completed") return "status-line ok";
  if (status === "failed" || status === "aborted") return "status-line failed";
  return "status-line";
}

function errorDialog(message) {
  return confirmDialog(message, { confirmLabel: "OK", danger: false });
}

function confirmDialog(message, { confirmLabel = "OK", danger = false } = {}) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "mini-dialog-overlay";
    overlay.innerHTML = `
      <div class="mini-dialog">
        <p></p>
        <div class="mini-dialog-actions">
          <button class="btn" data-role="cancel">Cancel</button>
          <button class="btn ${danger ? "danger" : "primary"}" data-role="confirm"></button>
        </div>
      </div>`;
    overlay.querySelector("p").textContent = message;
    overlay.querySelector('[data-role="confirm"]').textContent = confirmLabel;
    function done(result) {
      overlay.remove();
      resolve(result);
    }
    overlay.querySelector('[data-role="cancel"]').addEventListener("click", () => done(false));
    overlay.querySelector('[data-role="confirm"]').addEventListener("click", () => done(true));
    document.body.appendChild(overlay);
  });
}

function promptDialog(message, defaultValue = "") {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "mini-dialog-overlay";
    overlay.innerHTML = `
      <div class="mini-dialog">
        <p></p>
        <input type="text" data-role="value" />
        <div class="mini-dialog-actions">
          <button class="btn" data-role="cancel">Cancel</button>
          <button class="btn primary" data-role="confirm">OK</button>
        </div>
      </div>`;
    overlay.querySelector("p").textContent = message;
    const input = overlay.querySelector('[data-role="value"]');
    input.value = defaultValue;
    function done(result) {
      overlay.remove();
      resolve(result);
    }
    overlay.querySelector('[data-role="cancel"]').addEventListener("click", () => done(null));
    overlay.querySelector('[data-role="confirm"]').addEventListener("click", () => done(input.value));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") done(input.value);
      if (e.key === "Escape") done(null);
    });
    document.body.appendChild(overlay);
    input.focus();
    input.select();
  });
}

// --- Sidebar -------------------------------------------------------------

async function loadCatalog() {
  const data = await api("/api/catalog");
  const container = document.getElementById("jobCategories");
  container.innerHTML = "";

  for (const category of data.categories) {
    const jobsInCategory = data.jobs.filter((j) => j.category === category);
    if (jobsInCategory.length === 0) continue;

    const block = document.createElement("div");
    block.className = "category-block";

    const title = document.createElement("div");
    title.className = "category-title";
    title.textContent = category;
    block.appendChild(title);

    for (const job of jobsInCategory) {
      const item = document.createElement("div");
      item.className = "job-item" + (job.is_custom ? " custom" : "");
      item.dataset.search = (job.display_name + " " + job.description).toLowerCase();
      // 'spa' | 'tomo' | 'shared' — see backend/job_catalog.py's
      // pipeline_type() for how this is assigned. Used by applyJobFilters()
      // below for the SPA/Tomo/All toggle; never affects clickability.
      item.dataset.pipeline = job.pipeline_type || "shared";
      item.innerHTML = `<span class="job-name">${escapeHtml(job.display_name)}</span>
                         <span class="job-desc">${escapeHtml(job.description)}</span>`;
      item.addEventListener("click", () => openJobPopup(job.internal_name, job.display_name));
      block.appendChild(item);
    }
    container.appendChild(block);
  }

  applyJobFilters();
}

// --- SPA / Tomo / All toggle ----------------------------------------------
// Pure display filter — it never restricts which jobs can be opened. Every
// job stays one search away no matter what's selected: a non-empty search
// bypasses the pipeline filter entirely (see matchesPipeline below), so
// there's always a way to reach every job type, exactly as requested.

const PIPELINE_STORAGE_KEY = "relion_us_pipeline_filter";
let pipelineFilter = "all";
try {
  const saved = localStorage.getItem(PIPELINE_STORAGE_KEY);
  if (saved === "all" || saved === "spa" || saved === "tomo") pipelineFilter = saved;
} catch (e) {
  // Storage unavailable (private browsing, locked-down profile, etc.) —
  // just fall back to "all" for this session, nothing to recover from.
}

function applyJobFilters() {
  const q = document.getElementById("jobSearch").value.trim().toLowerCase();
  document.querySelectorAll(".job-item").forEach((item) => {
    const matchesSearch = !q || item.dataset.search.includes(q);
    const matchesPipeline =
      !!q || // a search in progress always searches the full catalog
      pipelineFilter === "all" ||
      item.dataset.pipeline === "shared" ||
      item.dataset.pipeline === pipelineFilter;
    item.style.display = matchesSearch && matchesPipeline ? "" : "none";
  });
  document.querySelectorAll(".category-block").forEach((block) => {
    const anyVisible = Array.from(block.querySelectorAll(".job-item")).some(
      (i) => i.style.display !== "none"
    );
    block.style.display = anyVisible ? "" : "none";
  });
}

function setPipelineFilter(value, { persist = true } = {}) {
  // persist=false is used for the project's auto-detected pipeline hint --
  // a convenience, not a preference, so it must never overwrite the user's
  // own deliberate choice in localStorage (they share the same key).
  pipelineFilter = value;
  if (persist) {
    try {
      localStorage.setItem(PIPELINE_STORAGE_KEY, value);
    } catch (e) {
      // Non-fatal — the toggle just won't be remembered across reloads.
    }
  }
  document.querySelectorAll(".pipeline-toggle-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.pipeline === value);
  });
  applyJobFilters();
}

document.querySelectorAll(".pipeline-toggle-btn").forEach((btn) => {
  btn.addEventListener("click", () => setPipelineFilter(btn.dataset.pipeline));
});
setPipelineFilter(pipelineFilter); // reflect initial/restored state on the buttons

document.getElementById("jobSearch").addEventListener("input", applyJobFilters);

// --- Topbar controls -------------------------------------------------------

document.getElementById("toggleSidebarBtn").addEventListener("click", () => {
  document.getElementById("sidebar").classList.toggle("hidden");
  // Belt-and-suspenders alongside the ResizeObserver in ensureNetworkResizeObserver():
  // that observer covers this already (the network canvas resizes as the
  // sidebar's own CSS transition plays out), but re-rendering right away too
  // means the boxes and lines never visibly disagree, even for a frame.
  if (ccView === "network") renderNetwork();
});

// Password protection (backend/auth.py) is opt-in and managed from the
// terminal only (Run-RelionUS --set-password / --enable-auth /
// --disable-auth) -- there is no in-browser way to turn it on, set it, or
// change it, just this one button to end the current session. If we got far
// enough to run this script at all while it's enabled, the auth gate
// middleware already required a valid session cookie to serve this page, so
// "enabled" here just means "show the button", not a second auth check.
fetch("/api/auth/status")
  .then((r) => r.json())
  .then((data) => {
    if (!data.enabled) return;
    const btn = document.getElementById("logoutBtn");
    btn.classList.remove("hidden");
    btn.addEventListener("click", async () => {
      await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
      window.location.replace("/login.html");
    });
  })
  .catch(() => { /* status check is best-effort; no button if it fails */ });

// --- Field rendering -------------------------------------------------------

// RELION's field `pattern` (e.g. "Input micrographs (*.{star,mrc})",
// "Optimisation set STAR file (*optimisation_set.star)") names the
// extensions Qt's file dialog would filter to in RELION's own GUI --
// extracted here instead of hardcoding ".star", so a field that also takes
// e.g. an .mrc reference isn't limited to STAR files in the picker. Falls
// back to [".star"] if the pattern doesn't parse into anything (this is
// only called on patterns that already matched /star/i).
function extensionsFromPattern(pattern) {
  const exts = new Set();
  // Most patterns are "Label (glob)", e.g. "STAR Files (*.star)" or
  // "Image Files (*.{spi,vol,mrc})" -- but some of RELION's own patterns are
  // a bare glob with no label/parens at all (e.g. "*.{mrc,gain}", "*.*",
  // "ResMap*"), so fall back to treating the whole pattern as the glob.
  const groups = (pattern.match(/\(([^)]*)\)/g) || []).map((g) => g.slice(1, -1));
  (groups.length ? groups : [pattern]).forEach((g) => {
    const braceMatch = g.match(/\{([^}]*)\}/);
    if (braceMatch) {
      braceMatch[1].split(",").forEach((t) => {
        const ext = t.trim().toLowerCase().replace(/[^a-z0-9]/g, "");
        if (ext) exts.add("." + ext);
      });
    } else {
      const dot = g.lastIndexOf(".");
      if (dot >= 0 && dot < g.length - 1) {
        const ext = g.slice(dot + 1).toLowerCase().replace(/[^a-z0-9]/g, "");
        if (ext) exts.add("." + ext);
      }
    }
  });
  // Empty means "no extension filter" -- pickFileDialog already treats an
  // empty extensions list as "match everything", which is the right browse
  // behaviour for a pattern like "*" or "ResMap*" that names no extension.
  return Array.from(exts);
}

// A pattern is a real file pattern (as opposed to job_definitions_raw.json's
// occasional mis-extracted numeric "pattern" on slider-type options that got
// tagged filename/inputnode, e.g. Manualpick's blue_value with pattern
// "0.1") if it's blank (browse with no filter, e.g. External's fn_exe),
// names a wildcard glob (e.g. "*.mrc"), or names a specific file in
// parentheses even with no wildcard (e.g. "STAR files (postprocess.star)"
// -- a fixed filename produced by an earlier job, not a glob, but still one
// real file to browse for). The numeric artifacts have neither parens nor a
// wildcard, so this excludes them without needing to special-case them.
function isBrowsableFilePattern(pattern) {
  const p = pattern || "";
  return p === "" || p.includes("*") || p.includes("(");
}

function renderField(key, option, value) {
  const wrap = document.createElement("div");
  wrap.className = "field-input";
  wrap.dataset.fieldKey = key;

  switch (option.field_type) {
    case "boolean": {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = !!value;
      wrap.appendChild(input);
      break;
    }
    case "slider": {
      const input = document.createElement("input");
      input.type = "number";
      input.value = value;
      if (option.min !== undefined) input.min = option.min;
      if (option.max !== undefined) input.max = option.max;
      if (option.step !== undefined) input.step = option.step;
      wrap.appendChild(input);
      break;
    }
    case "radio": {
      const select = document.createElement("select");
      (option.options || []).forEach((opt) => {
        const o = document.createElement("option");
        o.value = opt;
        o.textContent = opt;
        if (opt === value) o.selected = true;
        select.appendChild(o);
      });
      wrap.appendChild(select);
      break;
    }
    case "filename":
    case "inputnode": {
      const input = document.createElement("input");
      input.type = "text";
      input.value = value || "";
      input.placeholder = option.pattern || "";
      // A filename/inputnode field is how RELION's own GUI marks "this is a
      // single file, offer a Browse button" -- job_definitions_raw.json's
      // pattern (e.g. "Particle STAR file (*.star)", "MRC map files
      // (*half1*.mrc)", extracted straight from RELION's JobOption
      // definitions) is only used here to filter the picker to the right
      // extension(s). The one exception is a handful of mis-extracted
      // slider options (e.g. Manualpick's blue_value) whose "pattern" is
      // actually just a numeric default -- isBrowsableFilePattern excludes
      // those since a real file pattern always names a glob or is blank.
      if (isBrowsableFilePattern(option.pattern)) {
        const row = document.createElement("span");
        row.className = "field-browse-row";
        input.classList.add("field-browse-input");
        row.appendChild(input);
        const browseBtn = document.createElement("button");
        browseBtn.type = "button";
        browseBtn.className = "btn btn-icon";
        browseBtn.title = option.pattern
          ? `Browse for a file on the machine running the backend (${option.pattern})`
          : "Browse for a file on the machine running the backend";
        browseBtn.textContent = "…";
        browseBtn.addEventListener("click", async () => {
          const picked = await pickFileDialog({
            title: option.label || "Select a file",
            extensions: extensionsFromPattern(option.pattern || ""),
            startPath: currentDirOf(input.value),
          });
          if (picked) {
            input.value = picked;
            input.dispatchEvent(new Event("input", { bubbles: true }));
          }
        });
        row.appendChild(browseBtn);
        wrap.appendChild(row);
        break;
      }
      wrap.appendChild(input);
      break;
    }
    default: { // text
      const input = document.createElement("input");
      input.type = "text";
      input.value = value || "";
      wrap.appendChild(input);
    }
  }
  return wrap;
}

function getFieldValue(fieldWrap, option) {
  const el = fieldWrap.querySelector("input, select, textarea");
  if (option.field_type === "boolean") return el.checked;
  if (option.field_type === "slider") return parseFloat(el.value);
  return el.value;
}

function buildFieldRow(key, option, value) {
  const row = document.createDocumentFragment();
  const label = document.createElement("div");
  label.className = "field-label";
  label.innerHTML = `${escapeHtml(option.label || key)}` +
    (option.help ? `<span class="req-help" title="${escapeHtml(option.help)}">ⓘ</span>` : "");
  row.appendChild(label);
  row.appendChild(renderField(key, option, value));
  return row;
}

// --- Job popup ---------------------------------------------------------
// One popup implementation for both cases:
//   - launching a NEW job from the sidebar (existingRun is null)
//   - reopening a job from the Command Center table/timeline (existingRun
//     is that run's summary dict from /api/runs)
// existingRun's own field_values (if the run recorded them — see
// backend/main.py's start_run, which now forwards field_values for RELION
// jobs too, not just custom ones) prefill the form instead of the job
// type's defaults, and the command box shows the command that was ACTUALLY
// run rather than a fresh draft, so reopening history shows history.

// Tracks the one job popup allowed open at a time. Closed (not just
// covered) right before a new one mounts, so its websocket/progress polling
// tears down properly rather than streaming into a hidden window.
let currentJobWinbox = null;

async function openJobPopup(internalName, displayName, existingRun) {
  const isReopen = !!existingRun;

  let def;
  try {
    def = await api(`/api/jobs/${internalName}`);
  } catch (err) {
    errorDialog(`Could not load job definition for ${internalName}: ${err.message}`);
    return;
  }

  const optionsByKey = {};
  (def.options || []).forEach((o) => (optionsByKey[o.key] = o));
  // A job imported from RELION's pipeline carries the values RELION saved in
  // its job.star; merge them over the defaults so options RELION's job.star
  // doesn't mention (a newer RELION-US field, say) still get sane values.
  const prefillValues = isReopen && existingRun.field_values
    ? (existingRun.source === "relion"
        ? { ...(def.default_values || {}), ...existingRun.field_values }
        : existingRun.field_values)
    : (def.default_values || {});

  // The RELION-style output dir (<JobDir>/jobNNN) this popup targets. For a
  // fresh job it's the prospective next dir from the job definition; passed
  // to /api/runs so the backend (running from the project root, like RELION)
  // creates and tracks that dir. The backend finalizes/renumbers at Run time.
  let popupOutputSubdir = def.output_subdir || "";

  // currentRun tracks whichever run this popup is currently showing —
  // starts as existingRun when reopening, or null until Run/Overwrite
  // gives it one. Actions (abort/rename/note/clean/delete/etc.) all key
  // off currentRun.run_id.
  let currentRun = existingRun ? { ...existingRun } : null;

  const body = document.createElement("div");
  body.className = "job-popup";
  body.innerHTML = `
    <div class="job-desc-bar">${escapeHtml(def.description || "")}</div>
    ${(isReopen && existingRun.source === "relion") ? `<div class="job-relion-bar">Run in RELION itself — read-only here. ${
      escapeHtml(existingRun.import_note || "Settings below are the ones this job ran with, from its job.star.")
    }</div>` : ""}
    <div class="job-actions-toolbar" data-role="actions-toolbar">
      <span class="job-name-display" data-role="job-name-display" title="Click to rename (RELION's 'Alias' job action)">${escapeHtml((currentRun && currentRun.job_name) || displayName)}</span>
      <button class="btn" data-action="collapse" title="Minimize this window">− Collapse</button>
      <button class="btn" data-action="close" title="Close this window">✕ Close</button>
      <button class="btn" data-action="note" title="Edit note">📝 Note</button>
      <button class="btn" data-action="overwrite" hidden title="Re-run into this SAME output directory, overwriting its files (RELION's 'Overwrite' job action)">⟳ Overwrite</button>
      <button class="btn" data-action="abort" hidden title="Stop this running job">⏹ Abort</button>
      <button class="btn" data-action="mark-finished" hidden title="Manually mark as finished">✓ Mark Finished</button>
      <button class="btn" data-action="mark-failed" hidden title="Manually mark as failed">✗ Mark Failed</button>
      <button class="btn" data-action="delete" hidden title="Delete this job (and optionally its output files)">🗑 Delete</button>
    </div>
    <div class="job-note-row hidden" data-role="note-row"></div>
    <div class="tab-bar" data-role="tab-bar">
      <button class="tab-btn active" data-tab="inputs">Inputs</button>
      <button class="tab-btn" data-tab="progress" hidden>Progress</button>
      <button class="tab-btn" data-tab="ctfqc" hidden>CTF QC</button>
      <button class="tab-btn" data-tab="outputs" hidden>Outputs</button>
      <button class="tab-btn" data-tab="errors">Errors<span class="badge" data-role="error-badge" style="display:none">0</span></button>
      ${def.is_custom ? "" : '<button class="tab-btn" data-tab="source">RELION Source</button>'}
    </div>
    <div class="tab-content-area" data-role="tab-content-area">
      <div class="tab-content active" data-tab-content="inputs">
        <div class="job-standard-form" data-role="standard-form"></div>
      </div>
      <div class="tab-content" data-tab-content="progress"></div>
      <div class="tab-content" data-tab-content="ctfqc"></div>
      <div class="tab-content" data-tab-content="outputs"></div>
      <div class="tab-content" data-tab-content="errors"><pre class="errors-pre" data-role="errors-pre">(no errors yet)</pre></div>
      ${def.is_custom ? "" : `<div class="tab-content" data-tab-content="source"><pre class="source-pre">${escapeHtml(def.commands_source || "(source unavailable)")}</pre></div>`}
    </div>
    ${def.is_custom ? `
    <div class="command-row active" data-role="command-row">
      <div class="command-actions">
        <button class="btn primary" data-role="run-btn">Run</button>
        <span class="status-line" data-role="status-line"></span>
      </div>
    </div>` : `
    <div class="command-row active" data-role="command-row">
      <label>Command (edit freely — this exact string runs, nothing added or removed under the hood)
        <button class="btn" data-role="recompute-btn" style="padding:2px 8px;">Recompute draft</button>
      </label>
      <textarea class="command-box" data-role="command-box"></textarea>
      <div class="command-actions">
        <button class="btn primary" data-role="run-btn">Run</button>
        <span class="status-line" data-role="status-line"></span>
      </div>
    </div>`}
  `;

  // ---- Inputs tab: every option RELION's own GUI shows -------------------
  // Grouped and ordered by RELION's own tab names (I/O, CTF, ..., Running) as
  // collapsible sections, so a long job (Class3D has ~60 fields) stays
  // navigable without hiding anything behind a second tab. The Advanced
  // section (appended below, after this loop) is NOT for these -- it lists
  // command-line options the GUI never exposes, and sits last so it reads as
  // the "everything else" option past RELION's own Running/Other groups.
  const standardForm = body.querySelector('[data-role="standard-form"]');
  const groups = def.standard_groups || [];
  groups.forEach((group, index) => {
    const fields = (group.fields || []).filter((k) => optionsByKey[k]);
    if (!fields.length) return;

    const section = document.createElement("details");
    section.className = "opt-section";
    // The first group (RELION's I/O tab, for every job that has one) is the
    // one you always need; the rest open on click.
    section.open = index === 0 || !group.name;
    if (group.name) {
      const summary = document.createElement("summary");
      summary.className = "opt-section-head";
      summary.innerHTML =
        `<span class="opt-section-name">${escapeHtml(group.name)}</span>` +
        `<span class="opt-section-count">${fields.length}</span>`;
      section.appendChild(summary);
    }
    const grid = document.createElement("div");
    grid.className = "opt-section-grid";
    fields.forEach((key) => {
      grid.appendChild(buildFieldRow(key, optionsByKey[key], prefillValues[key]));
    });
    section.appendChild(grid);
    standardForm.appendChild(section);
  });

  // ---- Advanced: options the GUI does not expose -------------------------
  // Read from the installed binary's own --help output (GET .../cli-options),
  // not from the extracted definitions: the program accepts more than the GUI
  // offers, and those extras are exactly what you would otherwise dig out of
  // --help or the source. A collapsible section inside the Inputs tab, past
  // every one of RELION's own groups (I/O, ..., Running, Other) rather than
  // its own tab -- these are the options you reach for less often, so they
  // sit last, collapsed by default, and load lazily the first time this
  // section is actually opened (the "toggle" listener below) rather than on
  // every popup.
  const advancedSection = document.createElement("details");
  advancedSection.className = "opt-section";
  advancedSection.dataset.role = "advanced-section";
  const advancedSummary = document.createElement("summary");
  advancedSummary.className = "opt-section-head";
  advancedSummary.innerHTML = `<span class="opt-section-name">Advanced</span>`;
  advancedSection.appendChild(advancedSummary);
  const advancedContent = document.createElement("div");
  advancedContent.className = "cli-advanced-body";
  advancedSection.appendChild(advancedContent);
  standardForm.appendChild(advancedSection);
  let advancedLoaded = false;

  function renderAdvancedRows(host, options) {
    const list = document.createElement("div");
    list.className = "cli-option-list";
    options.forEach((opt) => {
      const row = document.createElement("div");
      row.className = "cli-option";
      row.dataset.flag = opt.flag;
      row.dataset.search = (opt.flag + " " + (opt.help || "")).toLowerCase();
      const value = opt.takes_value
        ? `<input type="text" class="cli-option-value" placeholder="${escapeHtml(opt.default || "value")}" />`
        : "";
      row.innerHTML = `
        <div class="cli-option-main">
          <code class="cli-option-flag">${escapeHtml(opt.flag)}</code>
          ${opt.default ? `<span class="cli-option-default">(${escapeHtml(opt.default)})</span>` : ""}
          ${opt.section ? `<span class="cli-option-section">${escapeHtml(opt.section)}</span>` : ""}
        </div>
        <div class="cli-option-help">${escapeHtml(opt.help || "")}</div>
        <div class="cli-option-actions">${value}
          <button type="button" class="btn btn-sm" data-role="cli-add">Add</button>
        </div>`;
      row.querySelector('[data-role="cli-add"]').addEventListener("click", () => {
        const box = body.querySelector('[data-role="command-box"]');
        if (!box) return;
        const input = row.querySelector(".cli-option-value");
        const val = input ? input.value.trim() : "";
        // Appended to the command box rather than applied invisibly: the box
        // is what runs, and every other field in this app works the same way.
        box.value = box.value.trimEnd() + " " + opt.flag + (val ? " " + val : "");
        box.focus();
        box.setSelectionRange(box.value.length, box.value.length);
      });
      list.appendChild(row);
    });
    host.appendChild(list);
  }

  async function loadAdvancedTab({ force = false } = {}) {
    if (advancedLoaded && !force) return;
    advancedLoaded = true;
    advancedContent.innerHTML = '<div class="cli-note">Asking the program for its options…</div>';
    const nrMpi = parseInt(collectValues().nr_mpi, 10) || 1;
    let data;
    try {
      data = await api(
        `/api/jobs/${encodeURIComponent(internalName)}/cli-options?nr_mpi=${nrMpi}`
      );
    } catch (err) {
      advancedContent.innerHTML =
        `<div class="cli-note">Could not list options: ${escapeHtml(err.message)}</div>`;
      return;
    }

    advancedContent.innerHTML = "";
    const intro = document.createElement("div");
    intro.className = "cli-note";
    if (!data.available) {
      intro.innerHTML =
        `${escapeHtml(data.message || "No extra options available.")}` +
        `<br />Anything you need can still be typed straight into the command box, ` +
        `or into <em>Additional arguments</em> in the Running section above.`;
      advancedContent.appendChild(intro);
      return;
    }
    intro.innerHTML =
      `Options <code>${escapeHtml(data.path || data.program || "")}</code> accepts that ` +
      `RELION's own form does not show — the ones you would otherwise find by running it ` +
      `with <code>--help</code>. ` +
      `<strong>${data.options.length}</strong> of ${data.total_program_options} ` +
      `(${data.hidden_by_gui} are already fields above). ` +
      `Adding one appends it to the command box, where you can still edit it.`;
    advancedContent.appendChild(intro);
    // Matches the count badge every other section shows.
    advancedSummary.innerHTML =
      `<span class="opt-section-name">Advanced</span>` +
      `<span class="opt-section-count">${data.options.length}</span>`;

    if (!data.parsed) {
      const pre = document.createElement("pre");
      pre.className = "source-pre";
      pre.textContent = data.raw || "";
      advancedContent.appendChild(
        Object.assign(document.createElement("div"), {
          className: "cli-note",
          textContent:
            "This program's help isn't in RELION's own format, so it isn't broken " +
            "into individual options here — the raw output follows.",
        })
      );
      advancedContent.appendChild(pre);
      return;
    }

    const search = document.createElement("input");
    search.type = "text";
    search.className = "cli-search";
    search.placeholder = "Filter options…";
    search.addEventListener("input", () => {
      const q = search.value.trim().toLowerCase();
      advancedContent.querySelectorAll(".cli-option").forEach((row) => {
        row.hidden = q && !row.dataset.search.includes(q);
      });
    });
    advancedContent.appendChild(search);

    if (!data.options.length) {
      advancedContent.appendChild(
        Object.assign(document.createElement("div"), {
          className: "cli-note",
          textContent: "Every option this program accepts is already a field above.",
        })
      );
      return;
    }
    renderAdvancedRows(advancedContent, data.options);
  }

  // Tab switching. The command box lives outside the tab-content area (a
  // fixed strip pinned to the bottom of the popup, see the command-row CSS)
  // rather than inside the Inputs tab-content, so it needs its own
  // active-toggle here instead of just riding along with .tab-content.
  const commandRowEl = body.querySelector('[data-role="command-row"]');
  body.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.hidden) return;
      body.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      body.querySelectorAll(".tab-content").forEach((c) => c.classList.remove("active"));
      btn.classList.add("active");
      body.querySelector(`[data-tab-content="${btn.dataset.tab}"]`).classList.add("active");
      if (commandRowEl) commandRowEl.classList.toggle("active", btn.dataset.tab === "inputs");
      if (btn.dataset.tab === "outputs") loadOutputsTab();
      if (btn.dataset.tab === "progress") refreshProgress();
    });
  });

  // The Advanced section loads the first time it's actually opened, not on
  // every popup -- most jobs, most of the time, never need it. The backend
  // caches each binary's --help on (path, mtime, size) regardless, so this
  // costs at most one subprocess per program per backend lifetime.
  advancedSection.addEventListener("toggle", () => {
    if (advancedSection.open) loadAdvancedTab();
  });

  function collectValues() {
    const values = {};
    body.querySelectorAll("[data-field-key]").forEach((wrap) => {
      const key = wrap.dataset.fieldKey;
      const opt = optionsByKey[key];
      if (opt) values[key] = getFieldValue(wrap, opt);
    });
    return values;
  }

  // Draft command recompute (RELION jobs only)
  const commandBox = body.querySelector('[data-role="command-box"]');
  if (commandBox) {
    commandBox.value = isReopen ? (currentRun.command || "") : (def.draft_command || "");
    const recomputeBtn = body.querySelector('[data-role="recompute-btn"]');
 recomputeBtn.addEventListener("click", async () => {
  try {
    const resp = await api(`/api/jobs/${internalName}/draft`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        isReopen
          // Reopened job (e.g. re-editing a FAILED run before hitting
          // Overwrite): target THIS job's own existing output directory,
          // not a fresh number -- otherwise Recompute silently drifts the
          // command's --o onto job006 while the user meant to fix job005,
          // and Overwrite ends up creating a new job next to it instead of
          // overwriting it.
          ? { field_values: collectValues(), overwrite_run_id: currentRun.run_id }
          : { field_values: collectValues(), output_subdir: popupOutputSubdir }
      ),
    });
    commandBox.value = resp.draft_command;
    if (resp.output_subdir) popupOutputSubdir = resp.output_subdir;
    if (resp.unmapped_fields && resp.unmapped_fields.length) {
      commandBox.title = "Not auto-mapped to a flag (add manually if needed): " +
        resp.unmapped_fields.join(", ");
    }
  } catch (err) {
    errorDialog("Could not recompute draft: " + err.message);
  }
});
  }

  const statusLine = body.querySelector('[data-role="status-line"]');
  const errorsPre = body.querySelector('[data-role="errors-pre"]');
  const errorBadge = body.querySelector('[data-role="error-badge"]');
  let errorLines = [];
  let ws = null;

  // stderr goes to the dedicated Errors tab (errorsPre/errorBadge) only --
  // no separate always-visible output block duplicating it underneath
  // every tab. stdout isn't discarded, just not streamed live inline: it's
  // still written to run.out and browsable/downloadable from the Outputs
  // tab like every other output file.
  function appendOutputLine(text, isStderr) {
    if (!isStderr) return;
    errorLines.push(text);
    errorsPre.textContent = errorLines.join("\n");
    errorsPre.classList.add("has-errors");
    errorBadge.style.display = "";
    errorBadge.textContent = String(errorLines.length);
  }

  // --- Job actions toolbar --------------------------------------------
  const toolbar = body.querySelector('[data-role="actions-toolbar"]');
  const jobNameDisplay = toolbar.querySelector('[data-role="job-name-display"]');
  const noteRow = body.querySelector('[data-role="note-row"]');
  const overwriteBtn = toolbar.querySelector('[data-action="overwrite"]');
  const abortBtn = toolbar.querySelector('[data-action="abort"]');
  const markFinishedBtn = toolbar.querySelector('[data-action="mark-finished"]');
  const markFailedBtn = toolbar.querySelector('[data-action="mark-failed"]');
  const deleteBtn = toolbar.querySelector('[data-action="delete"]');
  const noteBtn = toolbar.querySelector('[data-action="note"]');
  const outputsTabBtn = body.querySelector('[data-tab="outputs"]');
  const progressContent = body.querySelector('[data-tab-content="progress"]');
  const progressTabBtn = body.querySelector('[data-tab="progress"]');
  const ctfQcContent = body.querySelector('[data-tab-content="ctfqc"]');
  const ctfQcTabBtn = body.querySelector('[data-tab="ctfqc"]');
  let ctfQcState = { data: null, worstN: 12 };
  let progressTimer = null;
  let progressState = {
    enabled: true,          // "Live progress" — on by default for supported jobs
    everyN: 1,               // spacing of the iterations offered in the picker (1 = every)
    data: null,               // latest GET /progress response (iteration summaries + latest)
    selectedIteration: "latest",   // "latest" (auto-follows new polls) or a specific number
    iterationCache: {},      // iteration number -> its full {iteration, resolution_A, classes}
    orientationData: null,   // cached response from the on-demand viewing-direction button
  };


  function refreshNoteRow() {
    const note = (currentRun && currentRun.note) || "";
    if (note) {
      noteRow.textContent = "Note: " + note;
      noteRow.classList.remove("hidden");
    } else {
      noteRow.classList.add("hidden");
    }
  }

  function refreshToolbarState() {
    const hasRun = !!currentRun;
    const status = hasRun ? currentRun.status : null;
    // A job imported from RELION's own pipeline: this app didn't run it and
    // doesn't write RELION's pipeline file, so it can't abort, re-run into,
    // rename or delete it without leaving RELION's record describing
    // something untrue. Browsing its outputs and settings is fine.
    const fromRelion = hasRun && currentRun.source === "relion";
    jobNameDisplay.textContent = hasRun ? currentRun.job_name : displayName;
    overwriteBtn.hidden = !hasRun || fromRelion || status === "running";
    abortBtn.hidden = !hasRun || fromRelion || status !== "running";
    markFinishedBtn.hidden = !hasRun || fromRelion || status === "running" || status === "completed";
    markFailedBtn.hidden = !hasRun || fromRelion || status === "running" || status === "failed";
    deleteBtn.hidden = !hasRun || fromRelion || status === "running";
    noteBtn.hidden = fromRelion;
    jobNameDisplay.style.cursor = fromRelion ? "default" : "";
    jobNameDisplay.title = fromRelion
      ? "Run in RELION itself — rename it there"
      : "Click to rename (RELION's 'Alias' job action)";
    outputsTabBtn.hidden = !hasRun;
    refreshProgressTabVisibility();
    refreshCtfQcTabVisibility();
    refreshNoteRow();
  }
  refreshToolbarState();

  async function renameJob() {
    if (!currentRun) return;
    const newAlias = await promptDialog("Rename this job (leave blank to clear and revert to the plain job number):", currentRun.job_name);
    if (newAlias === null) return;
    try {
      const updated = await api(`/api/runs/${currentRun.run_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alias: newAlias }),
      });
      currentRun = { ...currentRun, ...updated };
      refreshToolbarState();
      refreshCommandCenter();
    } catch (err) {
      errorDialog("Could not rename job: " + err.message);
    }
  }
  jobNameDisplay.addEventListener("click", renameJob);

  noteBtn.addEventListener("click", async () => {
    if (!currentRun) {
      errorDialog("Run this job first, then add a note.");
      return;
    }
    const newNote = await promptDialog("Note for this job:", currentRun.note || "");
    if (newNote === null) return;
    try {
      const updated = await api(`/api/runs/${currentRun.run_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: newNote }),
      });
      currentRun = { ...currentRun, ...updated };
      refreshNoteRow();
      refreshCommandCenter();
    } catch (err) {
      errorDialog("Could not save note: " + err.message);
    }
  });

  // Mark Finished / Mark Failed differ only in the status they set.
  async function markStatus(status) {
    if (!currentRun) return;
    try {
      const updated = await api(`/api/runs/${currentRun.run_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      currentRun = { ...currentRun, ...updated };
      statusLine.textContent = `Status: ${status} (manually marked)`;
      statusLine.className = statusLineClass(status);
      refreshToolbarState();
      refreshCommandCenter();
    } catch (err) {
      errorDialog("Could not update status: " + err.message);
    }
  }
  markFinishedBtn.addEventListener("click", () => markStatus("completed"));
  markFailedBtn.addEventListener("click", () => markStatus("failed"));

  abortBtn.addEventListener("click", async () => {
    if (!currentRun) return;
    const ok = await confirmDialog(`Abort "${currentRun.job_name}"? The running process will be stopped.`, { confirmLabel: "Abort", danger: true });
    if (!ok) return;
    try {
      await api(`/api/runs/${currentRun.run_id}/abort`, { method: "POST" });
    } catch (err) {
      errorDialog("Could not abort: " + err.message);
    }
  });

  deleteBtn.addEventListener("click", async () => {
    if (!currentRun) return;
    const removeFiles = await confirmDialog(
      `Delete "${currentRun.job_name}"?\n\nThis removes it from the job history. Also delete its output directory (${currentRun.cwd || "unknown path"})? This cannot be undone.`,
      { confirmLabel: "Delete + remove files", danger: true }
    );
    // A single confirm covers both "delete the history entry" (always) and
    // "also remove files" (what the danger-styled confirm button means
    // here) -- Cancel aborts the whole action rather than offering a third
    // "delete history only" click, keeping this to one dialog.
    if (!removeFiles) return;
    try {
      await api(`/api/runs/${currentRun.run_id}?remove_files=true`, { method: "DELETE" });
      if (ws) try { ws.close(); } catch (e) { /* noop */ }
      win.close();
      refreshCommandCenter();
    } catch (err) {
      errorDialog("Could not delete job: " + err.message);
    }
  });

  overwriteBtn.addEventListener("click", async () => {
    if (!currentRun) return;
    const cmdToRun = commandBox ? commandBox.value : "";
    const ok = await confirmDialog(
      `Overwrite "${currentRun.job_name}"? This re-runs into the SAME output directory (${currentRun.cwd}), overwriting its files:\n\n${cmdToRun || "(custom job — reruns with the current field values)"}`,
      { confirmLabel: "Run (overwrite)", danger: true }
    );
    if (!ok) return;
    try {
      const payload = { internal_name: internalName, overwrite_run_id: currentRun.run_id };
      if (def.is_custom) payload.field_values = collectValues();
      else {
        payload.command = cmdToRun;
        payload.field_values = collectValues();
        // Lets the backend's own defensive rewrite (start_subprocess_job)
        // catch and fix a stale output dir in the command box -- e.g. left
        // over from before this popup's Recompute was fixed to track the
        // job being overwritten, or from a hand-edit -- rather than
        // silently running with whatever --o the text happens to say.
        payload.subdir = popupOutputSubdir;
      }
      const run = await api("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      // Close only after the run starts, so a failed overwrite leaves the
      // user's edited command and field values intact instead of destroyed.
      win.close();
      openJobPopup(internalName, displayName, run);
      refreshCommandCenter();
    } catch (err) {
      errorDialog("Could not start overwrite run: " + err.message);
    }
  });

  // --- Outputs tab: file listing, download, Clean / Harsh Clean --------
  const outputsContent = body.querySelector('[data-tab-content="outputs"]');

  function renderOutputsList(files) {
    if (!files.length) {
      return '<div class="outputs-empty">No output files yet.</div>';
    }
    return `<div class="outputs-list">${files.map((f) => `
      <div class="outputs-row" data-path="${escapeHtml(f.path)}">
        <input type="checkbox" data-role="file-check" ${f.suggested ? "checked" : ""} />
        <span class="out-path">${escapeHtml(f.path)}</span>
        <span class="out-size">${formatBytes(f.size)}</span>
        <span class="out-download" data-role="download" title="Download">⬇</span>
      </div>`).join("")}</div>`;
  }

  function wireDownloadClicks(container) {
    container.querySelectorAll('[data-role="download"]').forEach((el) => {
      el.addEventListener("click", () => {
        const row = el.closest(".outputs-row");
        const url = `/api/runs/${currentRun.run_id}/files/download?path=${encodeURIComponent(row.dataset.path)}`;
        window.open(url, "_blank");
      });
    });
  }

  // ---- Progress tab (iterative jobs only) --------------------------------
  // Charts + class thumbnails from the run_it###_model.star files RELION
  // writes each iteration. Nothing is stored server-side; thumbnails are
  // rendered on demand from the MRCs RELION already wrote. Per the user's
  // request this is per-job togglable, with a user-defined thumbnail interval
  // and an off-by-default "keep all iterations".
  const PROGRESS_POLL_MS = 4000;

  function progressSupported() {
    return currentRun && PROGRESS_JOB_TYPES.has(internalName);
  }

  function renderProgressShell() {
    progressContent.innerHTML = `
      <div class="progress-controls">
        <label class="progress-check" title="Turn off to stop polling this job entirely — no charts, no thumbnails, no extra work.">
          <input type="checkbox" data-role="prog-enabled" ${progressState.enabled ? "checked" : ""} /> Live progress
        </label>
        <label class="progress-num" title="Spacing of the iterations offered below, e.g. 5 lists iteration 1, 5, 10, 15… (the latest iteration is always included too).">
          Every
          <input type="number" data-role="prog-every" min="1" max="99" value="${progressState.everyN}" /> it
        </label>
        <label class="progress-select" title="Which iteration's class images to show below. Pick an earlier one to compare against the latest — while a run keeps going, your pick stays put unless you switch it back to Latest.">
          Iteration
          <select data-role="prog-iter-select"></select>
        </label>
        <span class="progress-status" data-role="prog-status"></span>
      </div>
      <div data-role="prog-body"></div>
    `;
    progressContent.querySelector('[data-role="prog-enabled"]').addEventListener("change", (e) => {
      progressState.enabled = e.target.checked;
      if (progressState.enabled) refreshProgress();
      else { stopProgressPolling(); renderProgressBody(); }
    });
    progressContent.querySelector('[data-role="prog-every"]').addEventListener("change", (e) => {
      progressState.everyN = Math.max(1, parseInt(e.target.value, 10) || 1);
      e.target.value = progressState.everyN;
      renderProgressBody();
    });
    progressContent.querySelector('[data-role="prog-iter-select"]').addEventListener("change", (e) => {
      progressState.selectedIteration = e.target.value === "latest" ? "latest" : parseInt(e.target.value, 10);
      renderProgressBody();
    });
  }

  // Populates the iteration <select> from the current d.iterations (already
  // fetched with the summary poll — no extra request just to list them),
  // spaced by "Every N it" but always keeping the true latest as an option
  // regardless of N, so a fresh iteration is never filtered out of reach.
  function renderIterationOptions(d) {
    const select = progressContent.querySelector('[data-role="prog-iter-select"]');
    if (!select) return;
    const latestIt = d.latest.iteration;
    const shown = d.iterations
      .map((p) => p.iteration)
      .filter((it) => it === latestIt || it % progressState.everyN === 0)
      .sort((a, b) => b - a);
    select.innerHTML = [`<option value="latest">Latest (iteration ${latestIt})</option>`]
      .concat(shown.filter((it) => it !== latestIt).map((it) => `<option value="${it}">iteration ${it}</option>`))
      .join("");
    const wanted = progressState.selectedIteration === "latest" ? "latest" : String(progressState.selectedIteration);
    // The previously picked iteration may have fallen out of the (re-spaced)
    // list -- fall back to Latest rather than leave the picker showing
    // nothing selected.
    select.value = wanted;
    if (select.value !== wanted) {
      progressState.selectedIteration = "latest";
      select.value = "latest";
    }
  }

  function thumbGridHtml(iteration, classes) {
    return `<div class="thumb-grid">` + classes.map((k) => `
      <figure class="thumb">
        <img loading="lazy" alt="Class ${k.index}, iteration ${iteration}"
             src="/api/runs/${encodeURIComponent(currentRun.run_id)}/progress/thumbnail?reference=${encodeURIComponent(k.reference)}" />
        <figcaption>#${k.index} · ${(k.distribution * 100).toFixed(0)}%${
          k.resolution_A != null ? ` · ${k.resolution_A.toFixed(1)} Å` : ""}</figcaption>
      </figure>`).join("") + `</div>`;
  }

  // Fetches (and caches) one specific iteration's full class breakdown --
  // the latest iteration never needs this, its classes already came along
  // with the summary poll (d.latest).
  async function fetchIterationClasses(iteration) {
    if (progressState.iterationCache[iteration]) return progressState.iterationCache[iteration];
    const data = await api(`/api/runs/${currentRun.run_id}/progress/iteration/${iteration}`);
    progressState.iterationCache[iteration] = data;
    return data;
  }

  async function renderProgressBody() {
    const host = progressContent.querySelector('[data-role="prog-body"]');
    const statusEl = progressContent.querySelector('[data-role="prog-status"]');
    if (!host) return;
    if (!progressState.enabled) {
      host.innerHTML = '<div class="progress-empty">Live progress is off for this job.</div>';
      if (statusEl) statusEl.textContent = "";
      return;
    }
    const d = progressState.data;
    if (!d || !d.available) {
      host.innerHTML = '<div class="progress-empty">Waiting for the first iteration…</div>';
      return;
    }
    renderIterationOptions(d);

    const targetIteration = progressState.selectedIteration === "latest"
      ? d.latest.iteration : progressState.selectedIteration;
    let shown;
    if (targetIteration === d.latest.iteration) {
      shown = d.latest;
    } else {
      host.innerHTML = '<div class="progress-empty">Loading iteration…</div>';
      try {
        shown = await fetchIterationClasses(targetIteration);
      } catch (err) {
        host.innerHTML = `<div class="progress-empty">Could not load iteration ${targetIteration}: ${escapeHtml(err.message)}</div>`;
        return;
      }
    }

    if (statusEl) {
      statusEl.textContent = `iteration ${shown.iteration}` +
        (d.dimensionality ? ` · ${d.dimensionality}D` : "") +
        (d.nr_classes ? ` · ${d.nr_classes} class${d.nr_classes === 1 ? "" : "es"}` : "");
    }
    const is3D = d.dimensionality === 3;
    const showOrientation = ORIENTATION_JOB_TYPES.has(internalName);
    host.innerHTML = `
      <div class="progress-section"><h4>Resolution by iteration</h4><div data-role="chart-res"></div></div>
      <div class="progress-section"><h4>Angular sampling accuracy by iteration</h4><div data-role="chart-acc-rot"></div></div>
      <div class="progress-section"><h4>Translational sampling accuracy by iteration</h4><div data-role="chart-acc-trans"></div></div>
      <div class="progress-section"><h4>Particles per class (iteration ${shown.iteration})</h4><div data-role="chart-dist"></div></div>
      <div class="progress-section"><h4>${is3D ? "Class volumes (central slice)" : "Class averages"}</h4><div data-role="thumbs"></div></div>
      ${showOrientation ? `
      <div class="progress-section">
        <h4>Viewing-direction distribution</h4>
        <div class="progress-controls" style="border-bottom:none;margin-bottom:4px;padding:0 0 6px;">
          <button class="btn" data-role="orient-btn">Generate from most recent completed iteration</button>
          <span class="progress-status" data-role="orient-status"></span>
        </div>
        <div data-role="orient-body"></div>
      </div>` : ""}
    `;
    drawResolutionChart(host.querySelector('[data-role="chart-res"]'), d.iterations);
    drawAccuracyChart(host.querySelector('[data-role="chart-acc-rot"]'), d.iterations, {
      key: "accuracy_rotation_deg", label: "Rotational accuracy", unit: "°", color: "s1",
    });
    drawAccuracyChart(host.querySelector('[data-role="chart-acc-trans"]'), d.iterations, {
      key: "accuracy_translation_A", label: "Translational accuracy", unit: " Å", color: "s2",
    });
    drawClassDistributionChart(host.querySelector('[data-role="chart-dist"]'), shown.classes);
    host.querySelector('[data-role="thumbs"]').innerHTML = thumbGridHtml(shown.iteration, shown.classes);

    if (showOrientation) {
      const orientBody = host.querySelector('[data-role="orient-body"]');
      const orientStatus = host.querySelector('[data-role="orient-status"]');
      const orientBtn = host.querySelector('[data-role="orient-btn"]');
      // Already fetched once this popup session (e.g. switching tabs and
      // back) -- show it again without another expensive parse; the button
      // still works to re-fetch (a still-running job may have moved on to
      // a newer completed iteration since).
      if (progressState.orientationData) {
        renderOrientationPlot(orientBody, orientStatus, progressState.orientationData);
      }
      orientBtn.addEventListener("click", async () => {
        orientBtn.disabled = true;
        orientStatus.textContent = "Reading particle orientations…";
        orientBody.innerHTML = "";
        try {
          const data = await api(`/api/runs/${currentRun.run_id}/orientation-distribution`);
          progressState.orientationData = data;
          renderOrientationPlot(orientBody, orientStatus, data);
        } catch (err) {
          orientStatus.textContent = "";
          orientBody.innerHTML = `<div class="progress-empty">Could not read orientations: ${escapeHtml(err.message)}</div>`;
        } finally {
          orientBtn.disabled = false;
        }
      });
    }
  }

  function renderOrientationPlot(body, statusEl, data) {
    if (!data.available) {
      statusEl.textContent = "";
      body.innerHTML = data.iteration != null
        ? `<div class="progress-empty">Iteration ${data.iteration}'s data.star has no orientation columns yet.</div>`
        : '<div class="progress-empty">No completed iteration to read yet.</div>';
      return;
    }
    statusEl.textContent = `iteration ${data.iteration} · ${data.n_particles.toLocaleString()} particles`;
    drawOrientationHeatmap(body, data);
  }

  function stopProgressPolling() {
    if (progressTimer) { clearTimeout(progressTimer); progressTimer = null; }
  }

  async function refreshProgress() {
    stopProgressPolling();
    if (!progressSupported() || !progressState.enabled) { await renderProgressBody(); return; }
    try {
      const d = await api(`/api/runs/${currentRun.run_id}/progress`);
      progressState.data = d;
      if (d.supported === false) {
        progressTabBtn.hidden = true;
        return;
      }
      await renderProgressBody();
    } catch (err) {
      const host = progressContent.querySelector('[data-role="prog-body"]');
      if (host) host.innerHTML = `<div class="progress-empty">Could not read progress: ${escapeHtml(err.message)}</div>`;
    }
    // Keep polling only while the job is actually running.
    if (currentRun && currentRun.status === "running" && progressState.enabled) {
      progressTimer = setTimeout(refreshProgress, PROGRESS_POLL_MS);
    }
  }

  function refreshProgressTabVisibility() {
    const supported = progressSupported();
    progressTabBtn.hidden = !supported;
    if (supported && !progressContent.dataset.built) {
      progressContent.dataset.built = "1";
      renderProgressShell();
      refreshProgress();
    }
  }

  // Charts are drawn with resolved theme colours, so repaint them on a switch.
  const onThemeChange = () => {
    if (progressContent.dataset.built) renderProgressBody();
    if (ctfQcContent.dataset.built) renderCtfQcBody();
  };
  document.addEventListener("relion-us-theme-changed", onThemeChange);

  // --- CTF QC tab (Ctffind only) ------------------------------------------
  // End-of-job only, unlike the Progress tab's live polling: RELION itself
  // only writes micrographs_ctf.star/power_spectra_fits.star once, when the
  // whole job finishes (see backend/ctf_qc.py's module docstring) -- so this
  // is a single fetch, not a poll loop.
  function ctfQcSupported() {
    return currentRun && CTF_QC_JOB_TYPES.has(internalName);
  }

  function renderCtfQcShell() {
    ctfQcContent.innerHTML = `
      <div class="progress-controls">
        <label class="progress-num" title="How many of the worst-fitting micrographs to show thumbnails for. Keeps the grid usable on a project with thousands of micrographs.">
          Show worst
          <input type="number" data-role="ctfqc-worst-n" min="1" max="200" value="${ctfQcState.worstN}" /> of them
        </label>
        <span class="progress-status" data-role="ctfqc-status"></span>
      </div>
      <div data-role="ctfqc-body"><div class="progress-empty">Loading…</div></div>
    `;
    ctfQcContent.querySelector('[data-role="ctfqc-worst-n"]').addEventListener("change", (e) => {
      ctfQcState.worstN = Math.max(1, parseInt(e.target.value, 10) || 12);
      e.target.value = ctfQcState.worstN;
      renderCtfQcBody();
    });
  }

  function ctfThumbGridHtml(micrographs) {
    return `<div class="thumb-grid">` + micrographs.map((m) => `
      <figure class="thumb">
        <img loading="lazy" alt="${escapeHtml(m.name)}"
             src="/api/runs/${encodeURIComponent(currentRun.run_id)}/ctf-qc/thumbnail?reference=${encodeURIComponent(m.ctf_image)}" />
        <figcaption title="${escapeHtml(m.name)}">${
          m.max_resolution_A != null ? `${m.max_resolution_A.toFixed(1)} Å` : "?"}${
          m.defocus_u != null ? ` · ${(m.defocus_u / 10000).toFixed(2)} µm` : ""}</figcaption>
      </figure>`).join("") + `</div>`;
  }

  function renderCtfQcBody() {
    const host = ctfQcContent.querySelector('[data-role="ctfqc-body"]');
    const statusEl = ctfQcContent.querySelector('[data-role="ctfqc-status"]');
    if (!host) return;
    const d = ctfQcState.data;
    if (!d || !d.available) {
      host.innerHTML = '<div class="progress-empty">Not available until the job finishes — RELION only writes its CTF summary once, at the end of the run.</div>';
      if (statusEl) statusEl.textContent = "";
      return;
    }
    if (statusEl) statusEl.textContent = `${d.count} micrograph${d.count === 1 ? "" : "s"}`;

    host.innerHTML = `
      <div class="progress-section"><h4>Defocus by micrograph</h4><div data-role="chart-defocus"></div></div>
      <div class="progress-section"><h4>Max resolution (CTF fit)</h4><div data-role="chart-maxres"></div></div>
      <div class="progress-section"><h4>Astigmatism</h4><div data-role="chart-astig"></div></div>
      <div class="progress-section"><h4>Figure of merit</h4><div data-role="chart-fom"></div></div>
      <div class="progress-section"><h4 data-role="worst-title"></h4><div data-role="ctfqc-thumbs"></div></div>
    `;
    const mics = d.micrographs;
    drawDefocusTrendChart(host.querySelector('[data-role="chart-defocus"]'), mics);
    drawHistogramChart(host.querySelector('[data-role="chart-maxres"]'), mics.map((m) => m.max_resolution_A), { unit: "Å", color: "s1" });
    drawHistogramChart(host.querySelector('[data-role="chart-astig"]'), mics.map((m) => m.astigmatism), { unit: "Å", color: "s2" });
    drawHistogramChart(host.querySelector('[data-role="chart-fom"]'), mics.map((m) => m.fom), { unit: "", color: "s1" });

    const n = Math.min(ctfQcState.worstN, mics.length);
    host.querySelector('[data-role="worst-title"]').textContent =
      `Worst ${n} of ${mics.length} by CTF fit resolution`;
    const worst = mics
      .filter((m) => m.max_resolution_A != null)
      .slice()
      .sort((a, b) => b.max_resolution_A - a.max_resolution_A)
      .slice(0, n);
    host.querySelector('[data-role="ctfqc-thumbs"]').innerHTML = worst.length
      ? ctfThumbGridHtml(worst)
      : '<div class="progress-empty">No CTF fit resolution reported.</div>';
  }

  async function refreshCtfQc() {
    if (!ctfQcSupported()) { renderCtfQcBody(); return; }
    try {
      ctfQcState.data = await api(`/api/runs/${currentRun.run_id}/ctf-qc`);
      if (ctfQcState.data.supported === false) { ctfQcTabBtn.hidden = true; return; }
      renderCtfQcBody();
    } catch (err) {
      const host = ctfQcContent.querySelector('[data-role="ctfqc-body"]');
      if (host) host.innerHTML = `<div class="progress-empty">Could not read CTF QC data: ${escapeHtml(err.message)}</div>`;
    }
  }

  function refreshCtfQcTabVisibility() {
    const supported = ctfQcSupported();
    ctfQcTabBtn.hidden = !supported;
    if (supported && !ctfQcContent.dataset.built) {
      ctfQcContent.dataset.built = "1";
      renderCtfQcShell();
      refreshCtfQc();
    }
  }

  async function loadOutputsTab() {
    if (!currentRun) return;
    outputsContent.innerHTML = '<div class="outputs-empty">Loading…</div>';
    try {
      const { files } = await api(`/api/runs/${currentRun.run_id}/files`);
      outputsContent.innerHTML = `
        <div class="outputs-toolbar">
          <button class="btn" data-role="clean-btn">🧹 Clean</button>
          <button class="btn danger" data-role="harsh-clean-btn">🔥 Harsh Clean</button>
          <button class="btn" data-role="download-selected-btn">⬇ Download selected as .zip</button>
        </div>
        ${renderOutputsList(files)}
      `;
      wireDownloadClicks(outputsContent);

      outputsContent.querySelector('[data-role="download-selected-btn"]').addEventListener("click", () => {
        const paths = Array.from(outputsContent.querySelectorAll('[data-role="file-check"]:checked'))
          .map((cb) => cb.closest(".outputs-row").dataset.path);
        if (!paths.length) { errorDialog("Check at least one file first."); return; }
        const qs = paths.map((p) => `path=${encodeURIComponent(p)}`).join("&");
        window.open(`/api/runs/${currentRun.run_id}/files/zip?${qs}`, "_blank");
      });

      async function runClean(harsh) {
        const { files: candidates } = await api(`/api/runs/${currentRun.run_id}/files?harsh=${harsh}`);
        outputsContent.innerHTML = `
          <p style="color:var(--text-dim);font-size:11px;">
            ${harsh ? "Harsh" : "Gentle"} clean review — a suggested selection is pre-checked
            (generic housekeeping patterns${harsh ? " + files over 100 MB" : ""}; see the Outputs
            tab docs). Nothing is deleted until you review and confirm below.
          </p>
          ${renderOutputsList(candidates)}
          <div class="outputs-toolbar" style="margin-top:8px;">
            <button class="btn danger" data-role="confirm-delete-btn">Delete checked files</button>
            <button class="btn" data-role="cancel-clean-btn">Cancel</button>
          </div>
        `;
        wireDownloadClicks(outputsContent);   // the review list has ⬇ links too
        outputsContent.querySelector('[data-role="cancel-clean-btn"]').addEventListener("click", loadOutputsTab);
        outputsContent.querySelector('[data-role="confirm-delete-btn"]').addEventListener("click", async () => {
          const paths = Array.from(outputsContent.querySelectorAll('[data-role="file-check"]:checked'))
            .map((cb) => cb.closest(".outputs-row").dataset.path);
          if (!paths.length) { errorDialog("Nothing checked — nothing to delete."); return; }
          const ok = await confirmDialog(`Delete ${paths.length} file(s)? This cannot be undone.`, { confirmLabel: "Delete", danger: true });
          if (!ok) return;
          try {
            await api(`/api/runs/${currentRun.run_id}/files/delete`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ relative_paths: paths }),
            });
          } catch (err) {
            errorDialog("Could not delete files: " + err.message);
          }
          loadOutputsTab();
        });
      }

      outputsContent.querySelector('[data-role="clean-btn"]').addEventListener("click", () => runClean(false));
      outputsContent.querySelector('[data-role="harsh-clean-btn"]').addEventListener("click", () => runClean(true));
    } catch (err) {
      outputsContent.innerHTML = `<div class="outputs-empty">Could not load output files: ${escapeHtml(err.message)}</div>`;
    }
  }

  // --- Collapse / Close ------------------------------------------------
  toolbar.querySelector('[data-action="collapse"]').addEventListener("click", () => win.minimize());
  toolbar.querySelector('[data-action="close"]').addEventListener("click", () => win.close());

  // --- Run / live status -------------------------------------------------

  function connectWebSocket(runId) {
    // Close any socket already attached to this popup before opening another.
    if (ws) { try { ws.close(); } catch (e) { /* already closed */ } ws = null; }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/runs/${runId}`);
    ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data);
      if (msg.type === "stdout") appendOutputLine(msg.line, false);
      else if (msg.type === "stderr") appendOutputLine(msg.line, true);
      else if (msg.type === "status") {
        statusLine.textContent = `Status: ${msg.status}` +
          (msg.exit_code !== undefined && msg.exit_code !== null ? ` (exit ${msg.exit_code})` : "");
        statusLine.className = statusLineClass(msg.status);
        if (currentRun) currentRun.status = msg.status;
        refreshToolbarState();
        refreshCommandCenter();
      } else if (msg.type === "error") {
        appendOutputLine(msg.line, true);
      }
    };
    ws.onerror = () => appendOutputLine("[websocket error]", true);
  }

  const runBtn = body.querySelector('[data-role="run-btn"]');
  if (isReopen) {
    // Reopening history: don't offer a bare "Run" (that's what Overwrite,
    // in the toolbar, is for — re-running into the SAME job explicitly).
    // Show status/output immediately instead.
    const commandRow = body.querySelector('[data-role="command-row"]');
    if (commandRow) commandRow.querySelector('[data-role="run-btn"]').style.display = "none";
    statusLine.textContent = `Status: ${currentRun.status}`;
    statusLine.className = statusLineClass(currentRun.status);
    connectWebSocket(currentRun.run_id);
  } else {
    runBtn.addEventListener("click", async () => {
      if (currentRun) return;   // already started; re-running is Overwrite's job
      runBtn.disabled = true;
      statusLine.textContent = "Starting…";
      statusLine.className = "status-line";
      try {
        const payload = { internal_name: internalName };
        if (def.is_custom) {
          payload.field_values = collectValues();
        } else {
          payload.command = commandBox.value;
          payload.field_values = collectValues();
          // Tell the backend which <JobDir>/jobNNN this command's --o targets,
          // so it creates/tracks that dir and can renumber if it was taken.
          payload.subdir = popupOutputSubdir;
        }
        const run = await api("/api/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        currentRun = run;
        refreshToolbarState();
        connectWebSocket(run.run_id);
        refreshCommandCenter();
      } catch (err) {
        appendOutputLine("Failed to start run: " + err.message, true);
        // Re-enable only on failure, so a successful start can't be double
        // clicked into launching a second job and orphaning the first
        // job's websocket.
        runBtn.disabled = false;
      }
    });
  }

  // Only one job popup at a time: close whichever was open right before
  // mounting this one (not merely re-focusing it), so its websocket and
  // progress polling stop cleanly instead of streaming into a hidden window.
  if (currentJobWinbox) {
    try { currentJobWinbox.close(); } catch (e) { /* noop */ }
    currentJobWinbox = null;
  }

  const win = new WinBox({
    title: displayName,
    width: "94%",
    height: "92%",
    x: "center",
    y: "center",
    mount: body,
    class: ["no-full", "job-popup-window"],
    onclose: () => {
      stopProgressPolling();
      document.removeEventListener("relion-us-theme-changed", onThemeChange);
      if (ws) try { ws.close(); } catch (e) { /* noop */ }
      if (currentJobWinbox === win) currentJobWinbox = null;
      return false;
    },
  });
  currentJobWinbox = win;
}

loadCatalog().catch((err) => {
  document.getElementById("ccHint").textContent = `Failed to load job catalog: ${err.message}`;
});

// --- Command Center (job history: table / timeline) -----------------------

const CC_VIEW_KEY = "relion_us_cc_view";
const CC_DIRECTION_KEY = "relion_us_cc_direction";
const CC_NETWORK_DIRECTION_KEY = "relion_us_cc_network_direction";
let ccRuns = [];
let ccView = "table";
let ccSort = { key: "started_at", dir: "desc" };
let ccDirection = "desc"; // timeline: 'desc' = newest first, 'asc' = oldest first
// Network's own direction, kept separate from the timeline's: the network
// view's whole point was oldest-at-top, branching down to what used it (see
// the "network view of the job history" feature), so it defaults the other
// way round from the timeline above -- 'asc' (oldest first, at the top).
let ccNetworkDirection = "asc";
try {
  const savedView = localStorage.getItem(CC_VIEW_KEY);
  if (savedView === "table" || savedView === "timeline" || savedView === "network") ccView = savedView;
  const savedDir = localStorage.getItem(CC_DIRECTION_KEY);
  if (savedDir === "asc" || savedDir === "desc") ccDirection = savedDir;
  const savedNetDir = localStorage.getItem(CC_NETWORK_DIRECTION_KEY);
  if (savedNetDir === "asc" || savedNetDir === "desc") ccNetworkDirection = savedNetDir;
} catch (e) { /* fall back to defaults */ }

function statusBadge(status) {
  return `<span class="cc-status-badge status-${escapeHtml(status || "pending")}">${escapeHtml(status || "pending")}</span>`;
}

function sortedRuns() {
  const rows = ccRuns.slice();
  const { key, dir } = ccSort;
  rows.sort((a, b) => {
    let av = a[key];
    let bv = b[key];
    if (key === "job_name") { av = (av || "").toLowerCase(); bv = (bv || "").toLowerCase(); }
    if (av === undefined || av === null) av = "";
    if (bv === undefined || bv === null) bv = "";
    if (av < bv) return dir === "asc" ? -1 : 1;
    if (av > bv) return dir === "asc" ? 1 : -1;
    return 0;
  });
  return rows;
}

function renderTable() {
  const tbody = document.getElementById("ccTableBody");
  const empty = document.getElementById("ccTableEmpty");
  tbody.innerHTML = "";
  const rows = sortedRuns();
  empty.classList.toggle("hidden", rows.length > 0);
  for (const run of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(run.job_name || "job???")}${run.note ? '<span class="cc-job-note-icon" title="' + escapeHtml(run.note) + '">📝</span>' : ""}${
        run.source === "relion"
          ? '<span class="cc-relion-tag" title="Run in RELION itself, read from this project\'s default_pipeline.star. Read-only here.">RELION</span>'
          : ""}</td>
      <td>${escapeHtml(run.display_name || run.internal_name)}</td>
      <td>${statusBadge(run.status)}</td>
      <td>${formatTimestamp(run.started_at)}</td>
      <td>${formatDuration(run.started_at, run.ended_at)}</td>
    `;
    tr.addEventListener("click", () => reopenRun(run));
    tbody.appendChild(tr);
  }
  document.querySelectorAll("#ccTable th[data-sort]").forEach((th) => {
    const arrow = th.querySelector(".sort-arrow");
    arrow.textContent = th.dataset.sort === ccSort.key ? (ccSort.dir === "asc" ? "▲" : "▼") : "";
  });
}

function renderTimeline() {
  const list = document.getElementById("ccTimelineList");
  const empty = document.getElementById("ccTimelineEmpty");
  list.innerHTML = "";
  let rows = ccRuns.slice().sort((a, b) => (a.started_at || 0) - (b.started_at || 0));
  if (ccDirection === "desc") rows = rows.reverse();
  empty.classList.toggle("hidden", rows.length > 0);
  for (const run of rows) {
    const card = document.createElement("div");
    card.className = "cc-card";
    // Lineage: inputs that came from another job's output are shown as a
    // clickable "from: jobNNN" chip (see backend list_runs input_links);
    // any remaining detected input files are listed plainly.
    const links = run.input_links || [];
    const linkedPaths = new Set(links.map((l) => l.path));
    let inputsLine = "";
    if (links.length) {
      const uniqueJobs = {};
      links.forEach((l) => { uniqueJobs[l.run_id] = l.job_name; });
      const chips = Object.entries(uniqueJobs)
        .map(([rid, name]) => `<span class="cc-input-job" data-run-id="${escapeHtml(rid)}" title="Open ${escapeHtml(name)}">↳ from ${escapeHtml(name)}</span>`)
        .join(" ");
      inputsLine += `<div class="cc-card-inputs"><span class="cc-inputs-label">Inputs from:</span> ${chips}</div>`;
    }
    const looseInputs = (run.detected_inputs || []).filter((p) => !linkedPaths.has(p));
    if (looseInputs.length) {
      inputsLine += `<div class="cc-card-inputs"><span class="cc-inputs-label">Detected inputs:</span> ${looseInputs.map(escapeHtml).join(", ")}</div>`;
    }
    const noteLine = run.note ? `<div class="cc-card-note">📝 ${escapeHtml(run.note)}</div>` : "";
    card.innerHTML = `
      <div class="cc-card-top">
        <span class="cc-card-name">${escapeHtml(run.job_name || "job???")}</span>
        <span class="cc-card-type">${escapeHtml(run.display_name || run.internal_name)}</span>
        ${statusBadge(run.status)}
        ${run.source === "relion" ? '<span class="cc-relion-tag" title="Run in RELION itself. Read-only here.">RELION</span>' : ""}
      </div>
      <div class="cc-card-meta">${
        run.source === "relion"
          // RELION's pipeline file records no timestamps -- saying so beats
          // showing a made-up one.
          ? "from RELION's pipeline" + (run.exists_on_disk === false ? " · directory missing" : "")
          : formatTimestamp(run.started_at) + " · " + formatDuration(run.started_at, run.ended_at)
      }</div>
      ${inputsLine}
      ${noteLine}
    `;
    card.addEventListener("click", () => reopenRun(run));
    // Clicking a lineage chip opens the PRODUCING job, not this card's job.
    card.querySelectorAll(".cc-input-job").forEach((chip) => {
      chip.addEventListener("click", (e) => {
        e.stopPropagation();
        const producer = ccRuns.find((r) => r.run_id === chip.dataset.runId);
        if (producer) reopenRun(producer);
      });
    });
    list.appendChild(card);
  }
}

// --- Command Center: Network view ------------------------------------
//
// A lineage graph built from run.input_links (see backend job_runner.py's
// _attach_input_lineage for this app's own runs, and project_manager.py's
// read_relion_pipeline "producers" for jobs RELION itself ran — both land in
// the same {path, run_id, job_name} shape). Oldest jobs sit at the top;
// every job that used another job's output hangs directly below it,
// side-by-side with any siblings that used the same input, connected by a
// curved branch line — e.g. job010 above, job011 and job012 side by side
// beneath it, each with their own line back up to job010.
//
// Layout is two passes: (1) each run's row = 1 + the deepest row among its
// parents (roots at row 0), so a branch's row always sits directly under
// every one of its parents; (2) within a row, runs are ordered by the
// average column of their already-placed parents, so branches stay visually
// near their source instead of jumping around. Pixel positions for the SVG
// connectors are read back from the DOM after layout (offsetLeft/Top
// relative to #ccNetworkRows) rather than computed by hand, so it stays
// correct regardless of how wide any job's name/type text renders.

function buildLineageGraph(runs) {
  const byId = new Map(runs.map((r) => [r.run_id, r]));
  const parentsOf = new Map();
  runs.forEach((r) => parentsOf.set(r.run_id, new Set()));
  runs.forEach((r) => {
    (r.input_links || []).forEach((link) => {
      if (byId.has(link.run_id) && link.run_id !== r.run_id) {
        parentsOf.get(r.run_id).add(link.run_id);
      }
    });
  });
  return { byId, parentsOf };
}

function computeLineageRows(runs, parentsOf) {
  const row = new Map();
  const visiting = new Set();
  function rowOf(id) {
    if (row.has(id)) return row.get(id);
    if (visiting.has(id)) return 0; // shouldn't happen (no cycles in a real pipeline)
    visiting.add(id);
    const parents = Array.from(parentsOf.get(id) || []);
    const r = parents.length ? 1 + Math.max(...parents.map(rowOf)) : 0;
    visiting.delete(id);
    row.set(id, r);
    return r;
  }
  runs.forEach((r) => rowOf(r.run_id));
  return row;
}

function renderNetwork() {
  const rowsEl = document.getElementById("ccNetworkRows");
  const svg = document.getElementById("ccNetworkEdges");
  const empty = document.getElementById("ccNetworkEmpty");
  rowsEl.innerHTML = "";
  svg.innerHTML = "";
  empty.classList.toggle("hidden", ccRuns.length > 0);
  if (!ccRuns.length) return;

  const { parentsOf } = buildLineageGraph(ccRuns);
  const rowIndex = computeLineageRows(ccRuns, parentsOf);
  const byId = new Map(ccRuns.map((r) => [r.run_id, r]));

  const maxRow = Math.max(...Array.from(rowIndex.values()));
  const rows = [];
  for (let i = 0; i <= maxRow; i++) rows.push([]);
  ccRuns.forEach((r) => rows[rowIndex.get(r.run_id)].push(r));

  // Column position (a plain array index) per run, filled in row order so
  // later rows can average their parents' already-known columns.
  const colOf = new Map();
  rows.forEach((rowRuns, r) => {
    rowRuns.sort((a, b) => {
      if (r > 0) {
        const avg = (run) => {
          const parents = Array.from(parentsOf.get(run.run_id) || []);
          const cols = parents.map((pid) => colOf.get(pid)).filter((c) => c !== undefined);
          return cols.length ? cols.reduce((s, c) => s + c, 0) / cols.length : Infinity;
        };
        const d = avg(a) - avg(b);
        if (d) return d;
      }
      return (a.job_number || 0) - (b.job_number || 0);
    });
    rowRuns.forEach((run, i) => colOf.set(run.run_id, i));
  });

  // Row 0 (the roots) is oldest, by construction (computeLineageRows). That's
  // also this view's default top-to-bottom order; ccNetworkDirection flips
  // which end sits on top, same as the Timeline direction button, without
  // touching the row/column math above -- only the DOM order rows are
  // appended in.
  const visualRows = ccNetworkDirection === "desc" ? rows.slice().reverse() : rows;

  visualRows.forEach((rowRuns) => {
    const rowEl = document.createElement("div");
    rowEl.className = "cc-network-row";
    rowRuns.forEach((run) => {
      const node = document.createElement("div");
      node.className = "cc-network-node";
      node.dataset.runId = run.run_id;
      node.innerHTML = `
        <div class="cc-network-node-top">
          <span class="cc-network-node-name">${escapeHtml(run.job_name || "job???")}</span>
          ${run.source === "relion" ? '<span class="cc-relion-tag" title="Run in RELION itself. Read-only here.">RELION</span>' : ""}
        </div>
        <div class="cc-network-node-type">${escapeHtml(run.display_name || run.internal_name)}</div>
        ${statusBadge(run.status)}
      `;
      node.addEventListener("click", () => reopenRun(run));
      rowEl.appendChild(node);
    });
    rowsEl.appendChild(rowEl);
  });

  // Edges, read back from the laid-out DOM. #ccNetworkRows is the SVG's
  // positioned ancestor (see style.css), so offsetLeft/Top on a node give
  // coordinates directly in the SVG's own coordinate space.
  //
  // Attachment is by which node is visually higher, not by which is the
  // "parent" -- ccNetworkDirection can put the newest job on top, and the
  // line should still run from the upper node's bottom edge to the lower
  // node's top edge either way, rather than from a "parent" edge that might
  // now be pointing the wrong way.
  const svgNS = "http://www.w3.org/2000/svg";
  const nodeEls = new Map();
  rowsEl.querySelectorAll(".cc-network-node").forEach((el) => nodeEls.set(el.dataset.runId, el));
  const centerX = (el) => el.offsetLeft + el.offsetWidth / 2;
  const top = (el) => ({ x: centerX(el), y: el.offsetTop });
  const bottom = (el) => ({ x: centerX(el), y: el.offsetTop + el.offsetHeight });
  ccRuns.forEach((run) => {
    const childEl = nodeEls.get(run.run_id);
    if (!childEl) return;
    (parentsOf.get(run.run_id) || new Set()).forEach((parentId) => {
      const parentEl = nodeEls.get(parentId);
      if (!parentEl) return;
      const parentIsHigher = parentEl.offsetTop <= childEl.offsetTop;
      const upperEl = parentIsHigher ? parentEl : childEl;
      const lowerEl = parentIsHigher ? childEl : parentEl;
      const p = bottom(upperEl);
      const c = top(lowerEl);
      const midY = (p.y + c.y) / 2;
      const path = document.createElementNS(svgNS, "path");
      path.setAttribute("d", `M ${p.x} ${p.y} C ${p.x} ${midY}, ${c.x} ${midY}, ${c.x} ${c.y}`);
      path.setAttribute("class", "cc-network-edge");
      svg.appendChild(path);
    });
  });
}

// Edges are computed from live DOM positions, so anything that moves the
// job boxes without changing what data is shown (opening/closing the Jobs
// sidebar, a browser window resize, the sidebar's own CSS transition still
// settling) has to trigger a recompute, or the lines stay drawn at their old
// coordinates while the boxes move out from under them. A ResizeObserver on
// the canvas (which stretches to fill the view -- see style.css's min-width:
// 100% -- so it resizes whenever the view's available width does) covers all
// of those causes in one place, including the transition: it keeps firing
// as the width animates and lands on the correct layout once it settles,
// rather than reading a mid-transition snapshot.
let networkResizeObserver = null;
function ensureNetworkResizeObserver() {
  if (networkResizeObserver || typeof ResizeObserver === "undefined") return;
  const canvas = document.getElementById("ccNetworkCanvas");
  if (!canvas) return;
  let raf = null;
  networkResizeObserver = new ResizeObserver(() => {
    if (ccView !== "network") return;
    if (raf) cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => renderNetwork());
  });
  networkResizeObserver.observe(canvas);
}

function renderCommandCenterViews() {
  document.getElementById("ccTableView").classList.toggle("hidden", ccView !== "table");
  document.getElementById("ccTimelineView").classList.toggle("hidden", ccView !== "timeline");
  document.getElementById("ccNetworkView").classList.toggle("hidden", ccView !== "network");
  // The direction button is shared between Timeline and Network (each keeps
  // its own direction state -- see ccDirection vs. ccNetworkDirection above)
  // rather than adding a second button, since only one of the two views is
  // ever visible at a time.
  // Label always states the CURRENTLY active setting ("Sort: ..." makes that
  // a status, not an instruction); the title says what clicking does, the
  // same state-vs-action split themeBtn uses.
  const dirBtn = document.getElementById("ccDirectionBtn");
  dirBtn.style.display = (ccView === "timeline" || ccView === "network") ? "inline-block" : "none";
  // No "Sort:" prefix -- a bare verb reads as an action to take, exactly the
  // ambiguity this label exists to avoid; the label IS the current setting.
  // Arrow follows the flow of time, not screen position: newest first is
  // "moving further back as you go" (up, against time) and oldest first is
  // "moving forward as you go" (down, with time) -- so newest points up and
  // oldest points down, regardless of which view puts which end on top.
  const newestFirst = ccView === "network" ? ccNetworkDirection === "desc" : ccDirection === "desc";
  dirBtn.textContent = newestFirst ? "Newest first ↑" : "Oldest first ↓";
  dirBtn.title = newestFirst
    ? "Currently showing newest jobs first — click to show oldest first"
    : "Currently showing oldest jobs first — click to show newest first";
  if (ccView === "table") renderTable();
  else if (ccView === "timeline") renderTimeline();
  else { ensureNetworkResizeObserver(); renderNetwork(); }
}

async function refreshCommandCenter() {
  try {
    const proj = await api("/api/project");
    ccRuns = proj.history || [];
  } catch (err) {
    ccRuns = [];
  }
  renderCommandCenterViews();
}

async function reopenRun(run) {
  // Note: openJobPopup fetches the job definition fresh from
  // /api/jobs/{internal_name} and uses ITS is_custom flag throughout, so we
  // don't need (and shouldn't try) to infer custom-vs-RELION from the run
  // summary here.
  if (run.source === "relion") {
    if (!run.internal_name) {
      errorDialog(
        `This job's type ("${run.relion_type_label || "unknown"}") isn't one ` +
        `RELION-US knows, so its form can't be opened. Its output directory is ` +
        `${run.cwd || "unknown"}.`
      );
      return;
    }
    // Fetch the detail so the form opens with the values RELION actually ran
    // with (from the job's own job.star), not this job type's defaults.
    try {
      run = await api(`/api/runs/${encodeURIComponent(run.run_id)}`);
    } catch (err) {
      errorDialog("Could not read this job from RELION's pipeline: " + err.message);
      return;
    }
  }
  openJobPopup(run.internal_name, run.display_name || run.internal_name, run);
}

document.querySelectorAll(".cc-view-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    ccView = btn.dataset.view;
    try { localStorage.setItem(CC_VIEW_KEY, ccView); } catch (e) { /* noop */ }
    document.querySelectorAll(".cc-view-btn").forEach((b) => b.classList.toggle("active", b === btn));
    renderCommandCenterViews();
  });
});
document.querySelectorAll(".cc-view-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === ccView));

document.getElementById("ccDirectionBtn").addEventListener("click", () => {
  if (ccView === "network") {
    ccNetworkDirection = ccNetworkDirection === "asc" ? "desc" : "asc";
    try { localStorage.setItem(CC_NETWORK_DIRECTION_KEY, ccNetworkDirection); } catch (e) { /* noop */ }
  } else {
    ccDirection = ccDirection === "desc" ? "asc" : "desc";
    try { localStorage.setItem(CC_DIRECTION_KEY, ccDirection); } catch (e) { /* noop */ }
  }
  renderCommandCenterViews();
});

document.querySelectorAll("#ccTable th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (ccSort.key === key) {
      ccSort.dir = ccSort.dir === "asc" ? "desc" : "asc";
    } else {
      ccSort = { key, dir: key === "started_at" ? "desc" : "asc" };
    }
    renderTable();
  });
});

// --- Two-way sync with RELION's own pipeline ---------------------------------
// When on, a job run here is registered in the project's default_pipeline.star
// (through RELION's own relion_pipeliner, never by writing that file directly),
// so RELION's GUI lists it too and you can move between the two.

const pipelineSyncBtn = document.getElementById("pipelineSyncBtn");
let pipelineSyncState = { enabled: false, available: false, locked: false };

function renderPipelineSyncButton() {
  const { enabled, available, locked } = pipelineSyncState;
  // Hidden entirely when relion_pipeliner isn't installed: a control that
  // can't do anything is worse than no control.
  pipelineSyncBtn.classList.toggle("hidden", !available);
  if (!available) return;
  pipelineSyncBtn.textContent = enabled ? "⇄ RELION sync: on" : "⇄ RELION sync: off";
  pipelineSyncBtn.classList.toggle("active", enabled);
  pipelineSyncBtn.title = enabled
    ? "Jobs run here are recorded in this project's default_pipeline.star, so RELION's own GUI lists them too."
      + (locked ? " (RELION currently holds the pipeline lock — updates will wait for it.)" : "")
    : "Jobs run here are tracked only by RELION-US. Turn on to record them in RELION's own pipeline as well.";
}

async function refreshPipelineSync() {
  try {
    pipelineSyncState = await api("/api/project/pipeline-sync");
  } catch (err) {
    pipelineSyncState = { enabled: false, available: false, locked: false };
  }
  renderPipelineSyncButton();
}

pipelineSyncBtn.addEventListener("click", async () => {
  const turningOn = !pipelineSyncState.enabled;
  if (turningOn) {
    const ok = await confirmDialog(
      "Record jobs run here in this project's default_pipeline.star?\n\n" +
      "RELION's own GUI will then list them, so you can switch between the two. " +
      "RELION-US doesn't write that file itself — it asks RELION's own " +
      "relion_pipeliner to add each job, which is what computes the job number, " +
      "creates the directory and works out the input/output graph.\n\n" +
      "Jobs already run here are not added retrospectively.",
      { confirmLabel: "Turn on" }
    );
    if (!ok) return;
  }
  try {
    pipelineSyncState = await api("/api/project/pipeline-sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: turningOn }),
    });
  } catch (err) {
    errorDialog("Could not change RELION sync: " + err.message);
  }
  renderPipelineSyncButton();
});

// --- Project switching -------------------------------------------------

const projectDirLabel = document.getElementById("projectDirLabel");
const changeProjectBtn = document.getElementById("changeProjectBtn");
const projectModalOverlay = document.getElementById("projectModalOverlay");
const projectPathInput = document.getElementById("projectPathInput");
const newFolderNameInput = document.getElementById("newFolderNameInput");
const projectBrowser = document.getElementById("projectBrowser");
const projectModalError = document.getElementById("projectModalError");
const notAProjectOverlay = document.getElementById("notAProjectOverlay");
const notAProjectError = document.getElementById("notAProjectError");

let pendingProjectPath = null; // path awaiting a "start new / pick different" decision

function showModalError(el, message) {
  el.textContent = message;
  el.classList.remove("hidden");
}

function clearModalError(el) {
  el.textContent = "";
  el.classList.add("hidden");
}

async function refreshProjectLabel() {
  try {
    const proj = await api("/api/project");
    cachedProjectPath = proj.path;   // used by the file picker
    projectDirLabel.textContent = proj.path;
    projectDirLabel.title = proj.path;
    // Optional auto-switch: only act on an unambiguous hint ('tomo' or
    // 'spa'). 'mixed' (project has run both) and 'unknown' (new project, or
    // no default_pipeline.star to read yet) leave the toggle exactly where
    // it was — manual switching is the correct fallback there, per request.
    if (proj.pipeline_hint === "tomo" || proj.pipeline_hint === "spa") {
      // persist:false — an auto-detected hint must not overwrite the user's
      // own remembered choice (same localStorage key).
      setPipelineFilter(proj.pipeline_hint, { persist: false });
    }
  } catch (err) {
    projectDirLabel.textContent = "(unknown project)";
  }
}

async function browseTo(path) {
  try {
    const listing = await api("/api/project/browse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    clearModalError(projectModalError);
    projectPathInput.value = listing.path;
    renderBrowser(listing);
  } catch (err) {
    showModalError(projectModalError, err.message);
  }
}

function renderBrowser(listing) {
  projectBrowser.innerHTML = "";
  if (listing.is_relion_project) {
    const badge = document.createElement("div");
    badge.className = "project-badge ok";
    badge.textContent = "✓ Looks like a RELION project";
    projectBrowser.appendChild(badge);
  }
  if (listing.parent) {
    const up = document.createElement("div");
    up.className = "browser-entry";
    up.textContent = "⬆ ..";
    up.addEventListener("click", () => browseTo(listing.parent));
    projectBrowser.appendChild(up);
  }
  listing.entries.filter((e) => e.is_dir).forEach((entry) => {
    const row = document.createElement("div");
    row.className = "browser-entry";
    row.textContent = "📁 " + entry.name;
    const childPath = listing.path.replace(/\/+$/, "") + "/" + entry.name;
    row.addEventListener("click", () => browseTo(childPath));
    projectBrowser.appendChild(row);
  });
  if (!listing.entries.some((e) => e.is_dir)) {
    const none = document.createElement("div");
    none.className = "browser-entry";
    none.style.cursor = "default";
    none.style.color = "var(--text-dim)";
    none.textContent = "(no subfolders)";
    projectBrowser.appendChild(none);
  }
}

// --- Recent projects ------------------------------------------------------
// Server-side cache (project_manager.load_recent_projects), not localStorage:
// the paths belong to the machine running the backend, so they must survive a
// different browser, a different machine, and a page reload on a cluster.

const recentProjectsWrap = document.getElementById("recentProjectsWrap");
const recentProjectsList = document.getElementById("recentProjectsList");

function renderRecentProjects(recent) {
  recentProjectsList.innerHTML = "";
  if (!recent || !recent.length) {
    recentProjectsWrap.classList.add("hidden");
    return;
  }
  recentProjectsWrap.classList.remove("hidden");
  recent.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "recent-entry" + (entry.exists ? "" : " missing");
    row.title = entry.exists
      ? entry.path
      : entry.path + "  (folder no longer exists)";

    const label = document.createElement("span");
    label.className = "recent-entry-label";
    const name = document.createElement("span");
    name.className = "recent-entry-name";
    name.textContent = entry.name;
    const dir = document.createElement("span");
    dir.className = "recent-entry-path";
    dir.textContent = entry.path;
    label.appendChild(name);
    label.appendChild(dir);
    row.appendChild(label);

    // One click browses to it and fills the path box, rather than switching
    // outright — same two-step confirm as any other folder, so a mis-click on
    // a list of similar-looking paths can't move the app out from under a
    // running job. Double-click is the shortcut for "yes, this one".
    row.addEventListener("click", () => browseTo(entry.path));
    row.addEventListener("dblclick", () => {
      projectPathInput.value = entry.path;
      document.getElementById("projectSwitchBtn").click();
    });

    const forget = document.createElement("button");
    forget.type = "button";
    forget.className = "recent-forget";
    forget.textContent = "✕";
    forget.title = "Remove from this list (does not delete the folder)";
    forget.addEventListener("click", async (e) => {
      e.stopPropagation();          // don't also browse into it
      try {
        const resp = await api("/api/project/recent/remove", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: entry.path }),
        });
        renderRecentProjects(resp.recent);
      } catch (err) {
        showModalError(projectModalError, err.message);
      }
    });
    row.appendChild(forget);
    recentProjectsList.appendChild(row);
  });
}

async function refreshRecentProjects() {
  try {
    const resp = await api("/api/project/recent");
    renderRecentProjects(resp.recent);
  } catch (err) {
    // A missing/unreadable cache must not block the dialog — the browser
    // below it is the primary way in.
    renderRecentProjects([]);
  }
}

function openProjectModal() {
  projectModalOverlay.classList.remove("hidden");
  clearModalError(projectModalError);
  newFolderNameInput.value = "";
  refreshRecentProjects();
  api("/api/project").then((proj) => {
    projectPathInput.value = proj.path;
    browseTo(proj.path);
  }).catch(() => {
    browseTo("");
  });
}

function closeProjectModal() {
  projectModalOverlay.classList.add("hidden");
}

async function onProjectChanged() {
  await refreshProjectLabel();
  await refreshCommandCenter();
  await refreshPipelineSync();   // the setting is per project
}

changeProjectBtn.addEventListener("click", openProjectModal);
document.getElementById("projectModalCancelBtn").addEventListener("click", closeProjectModal);
document.getElementById("projectPathGoBtn").addEventListener("click", () => browseTo(projectPathInput.value.trim()));
projectPathInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") browseTo(projectPathInput.value.trim());
});
newFolderNameInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("createFolderBtn").click();
});

document.getElementById("createFolderBtn").addEventListener("click", async () => {
  const name = newFolderNameInput.value.trim();
  if (!name) {
    showModalError(projectModalError, "Enter a folder name first.");
    return;
  }
  const parent = projectPathInput.value.trim();
  if (!parent) {
    showModalError(projectModalError, "Browse to a location first.");
    return;
  }
  const target = parent.replace(/\/+$/, "") + "/" + name;
  try {
    const resp = await api("/api/project/create-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: target }),
    });
    if (!resp.ok) {
      showModalError(projectModalError, resp.message || "Could not create folder.");
      return;
    }
    clearModalError(projectModalError);
    newFolderNameInput.value = "";
    projectPathInput.value = resp.path;
    renderBrowser(resp.listing);
  } catch (err) {
    showModalError(projectModalError, err.message);
  }
});

document.getElementById("projectSwitchBtn").addEventListener("click", async () => {
  const target = projectPathInput.value.trim();
  if (!target) return;
  try {
    const resp = await api("/api/project/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: target }),
    });
    if (!resp.ok) {
      pendingProjectPath = target;
      closeProjectModal();
      clearModalError(notAProjectError);
      notAProjectOverlay.classList.remove("hidden");
      return;
    }
    closeProjectModal();
    await onProjectChanged();
  } catch (err) {
    showModalError(projectModalError, "Could not switch project: " + err.message);
  }
});

document.getElementById("startNewProjectBtn").addEventListener("click", async () => {
  if (!pendingProjectPath) return;
  try {
    const resp = await api("/api/project/init", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: pendingProjectPath }),
    });
    if (!resp.ok) {
      showModalError(notAProjectError, resp.message || "Could not start new project here.");
      return;
    }
    notAProjectOverlay.classList.add("hidden");
    clearModalError(notAProjectError);
    pendingProjectPath = null;
    await onProjectChanged();
  } catch (err) {
    showModalError(notAProjectError, "Could not start new project: " + err.message);
  }
});

document.getElementById("pickDifferentFolderBtn").addEventListener("click", () => {
  notAProjectOverlay.classList.add("hidden");
  clearModalError(notAProjectError);
  pendingProjectPath = null;
  openProjectModal();
});

// ==========================================================================
// Tomogram / particle-pick visualizer (a tool, not a job). Server slices the
// MRC (mrcfile mmap -> PNG); picks are overlaid client-side using
// DeepETPicker's model: a particle shows on every slice within +/-(diameter/2)
// of its centre, with radius sqrt(r^2 - delta^2). See backend/viz.py.
// ==========================================================================

function choiceDialog(message, choices) {
  // choices: [{key,label,danger?}] -> resolves to key (or null if dismissed)
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "mini-dialog-overlay";
    const box = document.createElement("div");
    box.className = "mini-dialog";
    const msg = document.createElement("div");
    msg.textContent = message;
    const actions = document.createElement("div");
    actions.className = "mini-dialog-actions";
    choices.forEach((c) => {
      const b = document.createElement("button");
      b.className = "btn" + (c.danger ? " danger" : "") + (c.primary ? " primary" : "");
      b.textContent = c.label;
      b.addEventListener("click", () => { overlay.remove(); resolve(c.key); });
      actions.appendChild(b);
    });
    box.appendChild(msg); box.appendChild(actions); overlay.appendChild(box);
    document.body.appendChild(overlay);
  });
}

async function openVisualizer() {
  const body = document.createElement("div");
  body.className = "viz-popup";
  // Layout: the orthogonal views take the whole left side; every control and
  // input lives in a narrow right-hand rail, so the images get the window.
  body.innerHTML = `
    <div class="viz-stage-col">
      <div class="viz-ortho" data-role="viz-ortho">
        <div class="viz-panel" data-panel="zy" title="ZY — side view. Click to move the crosshair, scroll to step through X.">
          <img data-role="img-zy" alt="ZY slice" />
          <canvas data-role="ov-zy"></canvas>
          <span class="viz-panel-tag">ZY</span>
        </div>
        <div class="viz-panel viz-panel-main" data-panel="xy" title="XY — main view. Click to move the crosshair, scroll to step through Z.">
          <img data-role="viz-img" alt="XY slice" />
          <canvas data-role="ov-xy"></canvas>
          <span class="viz-panel-tag">XY</span>
        </div>
        <div class="viz-corner" data-role="viz-corner"></div>
        <div class="viz-panel" data-panel="xz" title="XZ — bottom view. Click to move the crosshair, scroll to step through Y.">
          <img data-role="img-xz" alt="XZ slice" />
          <canvas data-role="ov-xz"></canvas>
          <span class="viz-panel-tag">XZ</span>
        </div>
      </div>
      <div class="viz-meta" data-role="viz-meta"></div>
    </div>
    <aside class="viz-side">
      <div class="viz-side-group">
        <label class="viz-field">
          <span class="viz-field-label">Tomogram / STAR</span>
          <span class="viz-input-row">
            <input type="text" class="viz-input-sm" data-role="viz-path" placeholder="Tomograms/job012/tomograms.star" />
            <button type="button" class="btn btn-icon" data-role="viz-browse-main" title="Browse for a STAR or MRC file on the machine running the backend">…</button>
          </span>
        </label>
        <label class="viz-field">
          <span class="viz-field-label">Picks STAR <span class="viz-hint">(optional)</span></span>
          <span class="viz-input-row">
            <input type="text" class="viz-input-sm" data-role="viz-particles" placeholder="particles.star" />
            <button type="button" class="btn btn-icon" data-role="viz-browse-particles" title="Browse for a particles/coordinates STAR file">…</button>
          </span>
        </label>
        <div class="viz-inputs-row">
          <button class="btn primary btn-sm" data-role="viz-load">Load</button>
          <select class="viz-input-sm" data-role="viz-tomo" style="display:none"></select>
        </div>
        <div class="status-line viz-status" data-role="viz-status"></div>
      </div>

      <div class="viz-controls" data-role="viz-controls" style="display:none">
        <div class="viz-side-group">
          <div class="viz-side-title">Position</div>
          <div class="viz-ctrl-row">
            <span class="viz-ctrl-key">X</span>
            <input type="range" data-role="pos-x" min="0" max="0" value="0" />
            <span class="viz-ctrl-val" data-role="pos-x-val">0</span>
          </div>
          <div class="viz-ctrl-row">
            <span class="viz-ctrl-key">Y</span>
            <input type="range" data-role="pos-y" min="0" max="0" value="0" />
            <span class="viz-ctrl-val" data-role="pos-y-val">0</span>
          </div>
          <div class="viz-ctrl-row">
            <span class="viz-ctrl-key">Z</span>
            <input type="range" data-role="pos-z" min="0" max="0" value="0" />
            <span class="viz-ctrl-val" data-role="pos-z-val">0</span>
          </div>
          <div class="viz-hint">Click a view to move the crosshair; scroll over one to step through its own axis.</div>
        </div>

        <div class="viz-side-group">
          <div class="viz-side-title">Contrast</div>
          <div class="viz-ctrl-row">
            <span class="viz-ctrl-key">Black</span>
            <input type="range" data-role="viz-lo" min="0" max="100" value="0" title="black point" />
          </div>
          <div class="viz-ctrl-row">
            <span class="viz-ctrl-key">White</span>
            <input type="range" data-role="viz-hi" min="0" max="100" value="100" title="white point" />
          </div>
        </div>

        <div class="viz-side-group">
          <div class="viz-side-title">Picks</div>
          <label class="viz-check"><input type="checkbox" data-role="viz-showpicks" checked /> Show picks</label>
          <label class="viz-check"><input type="checkbox" data-role="viz-crosshair" checked /> Show crosshair</label>
          <div class="viz-ctrl-row">
            <span class="viz-ctrl-key">Ø vox</span>
            <input type="range" data-role="viz-diam" min="2" max="80" value="16" />
            <span class="viz-ctrl-val" data-role="viz-diam-val">16</span>
          </div>
          <div class="viz-ctrl-row">
            <span class="viz-ctrl-key">Line</span>
            <input type="range" data-role="viz-width" min="1" max="6" value="2" />
            <span class="viz-ctrl-val" data-role="viz-width-val">2</span>
          </div>
        </div>
      </div>
    </aside>
  `;

  const q = (sel) => body.querySelector(sel);
  const statusEl = q('[data-role="viz-status"]');
  const state = {
    mrc: null, particles: null, tomo: null, vinfo: null, picks: [],
    // crosshair position in voxels — the three panels are just three cuts
    // through this one point, which is what makes them feel linked.
    x: 0, y: 0, z: 0,
    lo: null, hi: null, diameter: 16, width: 2,
    showPicks: true, showCrosshair: true,
  };

  // Per-panel geometry. `axis`/`transpose` are what the slice endpoint needs;
  // `col`/`row`/`normal` name which voxel coordinate each screen direction is,
  // so the click, crosshair and pick maths are all driven from one table
  // instead of three parallel sets of if/else branches.
  const PANELS = {
    xy: { axis: "z", transpose: false, col: "x", row: "y", normal: "z",
          img: '[data-role="viz-img"]', ov: '[data-role="ov-xy"]' },
    zy: { axis: "x", transpose: true,  col: "z", row: "y", normal: "x",
          img: '[data-role="img-zy"]',  ov: '[data-role="ov-zy"]' },
    xz: { axis: "y", transpose: false, col: "x", row: "z", normal: "y",
          img: '[data-role="img-xz"]',  ov: '[data-role="ov-xz"]' },
  };

  const dimOf = (letter) => {
    const v = state.vinfo;
    return letter === "x" ? v.nx : letter === "y" ? v.ny : v.nz;
  };

  // ---- layout ------------------------------------------------------------
  // One isotropic scale for all three panels, so a voxel is the same size
  // everywhere and the crosshair lines up across panel borders. Sizes are set
  // in pixels rather than with `fr` units because the panels must match to the
  // pixel for that alignment to hold.
  function layoutStage() {
    const v = state.vinfo;
    if (!v) return;
    const ortho = q('[data-role="viz-ortho"]');
    const rect = ortho.getBoundingClientRect();
    const GAP = 4;
    const availW = Math.max(120, rect.width - GAP);
    const availH = Math.max(120, rect.height - GAP);
    const s = Math.min(availW / (v.nx + v.nz), availH / (v.ny + v.nz));
    const leftW = Math.max(24, Math.round(v.nz * s));
    const mainW = Math.max(48, Math.round(v.nx * s));
    const mainH = Math.max(48, Math.round(v.ny * s));
    const botH = Math.max(24, Math.round(v.nz * s));
    ortho.style.gridTemplateColumns = `${leftW}px ${mainW}px`;
    ortho.style.gridTemplateRows = `${mainH}px ${botH}px`;
    drawOverlays();
  }

  // ---- drawing -----------------------------------------------------------
  function drawOverlay(key) {
    const p = PANELS[key];
    const img = q(p.img), cv = q(p.ov);
    if (!state.vinfo || !img || !cv) return;
    const cw = img.clientWidth, ch = img.clientHeight;
    if (!cw || !ch) return;
    cv.width = cw; cv.height = ch;
    const ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, cw, ch);

    const sx = cw / dimOf(p.col), sy = ch / dimOf(p.row);

    if (state.showPicks && state.picks.length) {
      const r = state.diameter / 2;
      const idx = state[p.normal];
      const palette = ["#39d353", "#ff6ac1", "#f5a623", "#4aa3ff", "#e5484d"];
      ctx.lineWidth = state.width;
      for (const pk of state.picks) {
        // DeepETPicker's rule: a particle appears on every slice within
        // +/-(diameter/2) of its centre, at the spherical cross-section
        // radius for that distance.
        const d = Math.abs(idx - pk[p.normal]);
        if (d > r) continue;
        const rr = Math.sqrt(Math.max(0, r * r - d * d));
        ctx.beginPath();
        ctx.arc(pk[p.col] * sx, pk[p.row] * sy, Math.max(1, rr * sx), 0, 2 * Math.PI);
        ctx.strokeStyle = palette[(pk.class || 0) % palette.length];
        ctx.stroke();
      }
    }

    if (state.showCrosshair) {
      const cx = state[p.col] * sx, cy = state[p.row] * sy;
      ctx.save();
      ctx.strokeStyle = "rgba(255,214,102,0.85)";
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(cx, 0); ctx.lineTo(cx, ch);
      ctx.moveTo(0, cy); ctx.lineTo(cw, cy);
      ctx.stroke();
      ctx.restore();
    }
  }

  function drawOverlays() { Object.keys(PANELS).forEach(drawOverlay); }

  // ---- slice fetching ----------------------------------------------------
  // Only panels whose own slice index actually moved are refetched: clicking
  // in XY moves x and y, which changes the ZY and XZ cuts but not XY's own.
  let pending = new Set();
  let sliceTimer = null;

  function requestPanels(keys) {
    keys.forEach((k) => pending.add(k));
    if (sliceTimer) clearTimeout(sliceTimer);
    // Coalesce: a slider drag or a click-drag fires ~60 events/sec, and each
    // one is an mmap + PNG encode on the backend.
    sliceTimer = setTimeout(() => {
      sliceTimer = null;
      const keys2 = Array.from(pending);
      pending = new Set();
      keys2.forEach(fetchPanel);
    }, 60);
  }

  function fetchPanel(key) {
    if (!state.mrc || !state.vinfo) return;
    const p = PANELS[key];
    const index = state[p.normal];
    const params = new URLSearchParams({
      mrc_path: state.mrc, axis: p.axis, index: String(index),
    });
    if (p.transpose) params.set("transpose", "true");
    if (state.lo != null) params.set("lo", String(state.lo));
    if (state.hi != null) params.set("hi", String(state.hi));
    const img = q(p.img);
    img.onload = () => drawOverlay(key);
    img.src = `/api/viz/slice?${params.toString()}`;
  }

  function refreshAllPanels() {
    Object.keys(PANELS).forEach(fetchPanel);
    updateMeta();
  }

  function updateMeta() {
    const v = state.vinfo;
    if (!v) return;
    q('[data-role="viz-meta"]').textContent =
      `${state.tomo || ""}  ·  ${v.nx}×${v.ny}×${v.nz}` +
      (v.voxel_size ? `  ·  ${v.voxel_size.toFixed(2)} Å/vox` : "") +
      `  ·  ${state.picks.length} picks  ·  x ${state.x}  y ${state.y}  z ${state.z}`;
  }

  // ---- crosshair movement ------------------------------------------------
  function setPosition(coords, { fromSlider = false } = {}) {
    const changed = [];
    for (const [letter, raw] of Object.entries(coords)) {
      const max = dimOf(letter) - 1;
      const val = Math.max(0, Math.min(max, Math.round(raw)));
      if (val !== state[letter]) { state[letter] = val; changed.push(letter); }
    }
    if (!changed.length) return;
    if (!fromSlider) syncSliders();
    changed.forEach((letter) => {
      q(`[data-role="pos-${letter}-val"]`).textContent = String(state[letter]);
    });
    // A panel is refetched when the coordinate it slices along moved.
    const keys = Object.keys(PANELS).filter((k) => changed.includes(PANELS[k].normal));
    if (keys.length) requestPanels(keys);
    drawOverlays();
    updateMeta();
  }

  function syncSliders() {
    ["x", "y", "z"].forEach((letter) => {
      q(`[data-role="pos-${letter}"]`).value = String(state[letter]);
    });
  }

  function setupPosition() {
    const v = state.vinfo;
    state.x = Math.floor(v.nx / 2);
    state.y = Math.floor(v.ny / 2);
    state.z = Math.floor(v.nz / 2);
    ["x", "y", "z"].forEach((letter) => {
      const sl = q(`[data-role="pos-${letter}"]`);
      sl.max = String(dimOf(letter) - 1);
      sl.value = String(state[letter]);
      q(`[data-role="pos-${letter}-val"]`).textContent = String(state[letter]);
    });
  }

  // ---- panel interaction -------------------------------------------------
  function panelCoordsFromEvent(key, ev) {
    const p = PANELS[key];
    const img = q(p.img);
    const rect = img.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const fx = (ev.clientX - rect.left) / rect.width;
    const fy = (ev.clientY - rect.top) / rect.height;
    return {
      [p.col]: fx * (dimOf(p.col) - 1),
      [p.row]: fy * (dimOf(p.row) - 1),
    };
  }

  body.querySelectorAll(".viz-panel").forEach((panel) => {
    const key = panel.dataset.panel;
    let dragging = false;

    const move = (ev) => {
      if (!state.vinfo) return;
      const coords = panelCoordsFromEvent(key, ev);
      if (coords) setPosition(coords);
    };
    panel.addEventListener("mousedown", (ev) => {
      if (ev.button !== 0) return;
      dragging = true; move(ev); ev.preventDefault();
    });
    panel.addEventListener("mousemove", (ev) => { if (dragging) move(ev); });
    // Listening on window, not the panel: a drag that leaves the panel (easy
    // to do near an edge) would otherwise never see its mouseup and the view
    // would keep following the cursor after the button was released.
    window.addEventListener("mouseup", () => { dragging = false; });

    panel.addEventListener("wheel", (ev) => {
      if (!state.vinfo) return;
      ev.preventDefault();          // don't scroll the popup body
      const normal = PANELS[key].normal;
      const step = ev.shiftKey ? 10 : 1;   // shift = coarse scrub
      setPosition({ [normal]: state[normal] + (ev.deltaY > 0 ? step : -step) });
    }, { passive: false });
  });

  // ---- loading -----------------------------------------------------------
  async function loadVolume(mrcPath) {
    statusEl.textContent = "Loading volume…";
    try {
      // Fetch first, commit state after -- a failed load must not leave
      // state.mrc/state.vinfo describing two different volumes.
      const info = await api(`/api/viz/volume-info?mrc_path=${encodeURIComponent(mrcPath)}`);
      state.mrc = mrcPath;
      state.vinfo = info;
      state.lo = info.contrast_lo;
      state.hi = info.contrast_hi;
      const vmin = info.sample_min, span = (info.sample_max - info.sample_min) || 1;
      q('[data-role="viz-lo"]').value = String(Math.round(((state.lo - vmin) / span) * 100));
      q('[data-role="viz-hi"]').value = String(Math.round(((state.hi - vmin) / span) * 100));
      q('[data-role="viz-controls"]').style.display = "";
      q('[data-role="viz-ortho"]').classList.add("loaded");
      setupPosition();
      layoutStage();
      refreshAllPanels();
      statusEl.textContent = "";
    } catch (err) {
      statusEl.textContent = "Could not load volume: " + err.message;
    }
  }

  async function loadPicks(mrcPathForMatch) {
    if (!state.particles) { state.picks = []; return; }
    try {
      const resp = await api(`/api/viz/picks`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          particles_path: state.particles,
          tomo_name: state.tomo || mrcPathForMatch,
          volume: state.vinfo,
        }),
      });
      if (resp.matched === false) {
        const choice = await choiceDialog(
          "⚠ " + resp.message + "\n\nLoad these picks anyway, pick different files, or cancel?",
          [
            { key: "anyway", label: "Load anyway", primary: true },
            { key: "reload", label: "Reload files" },
            { key: "cancel", label: "Cancel", danger: true },
          ]
        );
        if (choice !== "anyway") {
          state.picks = [];
          if (choice === "reload") { q('[data-role="viz-particles"]').focus(); }
          return;
        }
      }
      state.picks = resp.picks || [];
    } catch (err) {
      statusEl.textContent = "Could not load picks: " + err.message;
      state.picks = [];
    }
  }

  // --- Browse buttons ---------------------------------------------------
  // Extension lists mirror what viz.py accepts (VOLUME_SUFFIXES/STAR_SUFFIXES).
  q('[data-role="viz-browse-main"]').addEventListener("click", async () => {
    const picked = await pickFileDialog({
      title: "Select a tomogram or STAR file",
      extensions: [".star", ".mrc", ".mrcs", ".rec", ".st", ".ali"],
      startPath: currentDirOf(q('[data-role="viz-path"]').value),
    });
    if (picked) q('[data-role="viz-path"]').value = picked;
  });
  q('[data-role="viz-browse-particles"]').addEventListener("click", async () => {
    const picked = await pickFileDialog({
      title: "Select a particles / coordinates STAR file",
      extensions: [".star"],
      startPath: currentDirOf(q('[data-role="viz-particles"]').value)
        || currentDirOf(q('[data-role="viz-path"]').value),
    });
    if (picked) q('[data-role="viz-particles"]').value = picked;
  });

  // --- Load button: inspect -> populate tomograms -> load volume+picks ---
  q('[data-role="viz-load"]').addEventListener("click", async () => {
    const path = q('[data-role="viz-path"]').value.trim();
    state.particles = q('[data-role="viz-particles"]').value.trim() || null;
    if (!path) { statusEl.textContent = "Enter a STAR or MRC path."; return; }
    statusEl.textContent = "Inspecting…";
    let info;
    try {
      info = await api(`/api/viz/inspect`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, particles_path: state.particles }),
      });
    } catch (err) { statusEl.textContent = "Error: " + err.message; return; }

    if (info.particles_path) state.particles = info.particles_path;
    if (info.needs_mrc) {
      statusEl.textContent = (info.warnings || []).join(" ") || "Provide the MRC tomogram as the main path.";
      return;
    }
    const tomos = info.tomograms || [];
    const sel = q('[data-role="viz-tomo"]');
    if (tomos.length > 1) {
      sel.innerHTML = tomos.map((t, i) => `<option value="${i}">${escapeHtml(t.name)}</option>`).join("");
      sel.style.display = "";
      sel.onchange = async () => {
        const t = tomos[sel.value];
        state.tomo = t.name;
        await loadVolume(t.mrc_path);
        await loadPicks(t.mrc_path);
        refreshAllPanels();
      };
    } else {
      sel.style.display = "none";
    }
    if (!tomos.length) { statusEl.textContent = "No tomogram volume found to display."; return; }
    const t = tomos[0];
    state.tomo = t.name;
    await loadVolume(t.mrc_path);
    await loadPicks(t.mrc_path);
    refreshAllPanels();
  });

  // --- Controls ---
  ["x", "y", "z"].forEach((letter) => {
    q(`[data-role="pos-${letter}"]`).addEventListener("input", (e) => {
      setPosition({ [letter]: parseInt(e.target.value, 10) }, { fromSlider: true });
    });
  });

  function contrastFromSliders() {
    const v = state.vinfo; if (!v) return;
    const vmin = v.sample_min, span = (v.sample_max - v.sample_min) || 1;
    let lo = vmin + (parseInt(q('[data-role="viz-lo"]').value, 10) / 100) * span;
    let hi = vmin + (parseInt(q('[data-role="viz-hi"]').value, 10) / 100) * span;
    if (hi <= lo) hi = lo + span * 0.01;
    state.lo = lo; state.hi = hi;
    requestPanels(Object.keys(PANELS));   // contrast affects all three
  }
  q('[data-role="viz-lo"]').addEventListener("input", contrastFromSliders);
  q('[data-role="viz-hi"]').addEventListener("input", contrastFromSliders);

  q('[data-role="viz-diam"]').addEventListener("input", (e) => {
    state.diameter = parseInt(e.target.value, 10);
    q('[data-role="viz-diam-val"]').textContent = String(state.diameter);
    drawOverlays();
  });
  q('[data-role="viz-width"]').addEventListener("input", (e) => {
    state.width = parseInt(e.target.value, 10);
    q('[data-role="viz-width-val"]').textContent = String(state.width);
    drawOverlays();
  });
  q('[data-role="viz-showpicks"]').addEventListener("change", (e) => {
    state.showPicks = e.target.checked; drawOverlays();
  });
  q('[data-role="viz-crosshair"]').addEventListener("change", (e) => {
    state.showCrosshair = e.target.checked; drawOverlays();
  });

  new WinBox({
    title: "Tomogram Viewer",
    width: "1040px", height: "800px",
    x: "center", y: "center",
    mount: body,
    class: ["viz-winbox"],
    onresize: () => layoutStage(),
  });
  // WinBox mounts asynchronously; the first layout needs the real box size.
  setTimeout(layoutStage, 0);
}

document.getElementById("visualizeBtn").addEventListener("click", openVisualizer);

// ==========================================================================
// Server-side FILE picker.
// Reuses POST /api/project/browse (which already returns files alongside
// folders) rather than an <input type="file">: the backend may be on a
// different machine than the browser — an HPC login node, typically — so the
// browser's own filesystem is not the one holding the data. Returns a path
// relative to the project when the pick is inside it (what the viewer's API
// expects), or an absolute path otherwise.
// ==========================================================================

let cachedProjectPath = null;

// Where a Browse button should open: the folder holding whatever is already
// typed in that field, so re-browsing resumes where you left off. Returns null
// (meaning "start at the project directory") when the field is empty or has no
// folder part.
function currentDirOf(value) {
  const v = (value || "").trim();
  if (!v) return null;
  const idx = v.lastIndexOf("/");
  if (idx < 0) return null;                    // a bare filename -> project dir
  const dir = v.slice(0, idx);
  if (!dir) return "/";
  if (dir.startsWith("/")) return dir;         // already absolute
  return cachedProjectPath ? `${cachedProjectPath.replace(/\/+$/, "")}/${dir}` : dir;
}

function pickFileDialog({ title = "Select a file", extensions = [], startPath = null } = {}) {
  const exts = extensions.map((e) => e.toLowerCase());
  const matches = (name) => !exts.length || exts.some((e) => name.toLowerCase().endsWith(e));

  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    // Own class so this is unambiguous: the Change Project modal is always in
    // the DOM (hidden) and also carries .modal-overlay/.project-browser.
    overlay.className = "modal-overlay file-picker";
    overlay.innerHTML = `
      <div class="modal">
        <h3>${escapeHtml(title)}</h3>
        <p class="modal-hint">
          Listing files on the machine running the backend${
            exts.length ? ` — showing ${exts.map(escapeHtml).join(", ")}` : ""
          }. Click a folder to open it, or a file to choose it.
        </p>
        <div class="modal-row">
          <input type="text" data-role="pick-path" placeholder="/path/to/folder" />
          <button class="btn" data-role="pick-go">Go</button>
        </div>
        <div class="picker-current" data-role="pick-current"></div>
        <div class="project-browser" data-role="pick-list"></div>
        <div class="modal-actions">
          <button class="btn" data-role="pick-cancel">Cancel</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const listEl = overlay.querySelector('[data-role="pick-list"]');
    const currentEl = overlay.querySelector('[data-role="pick-current"]');
    const pathInput = overlay.querySelector('[data-role="pick-path"]');

    function finish(value) {
      overlay.remove();
      resolve(value);
    }

    // Prefer a project-relative path: the viewer resolves relative paths
    // against the project directory, and that's also what RELION itself
    // stores, so it keeps typed and picked paths in the same idiom.
    function toProjectRelative(fullPath) {
      if (!cachedProjectPath) return fullPath;
      const root = cachedProjectPath.replace(/\/+$/, "");
      if (fullPath === root) return fullPath;
      if (fullPath.startsWith(root + "/")) return fullPath.slice(root.length + 1);
      return fullPath;
    }

    async function show(path) {
      // Show where we're going before the round trip finishes: the dialog is
      // in the DOM the moment it opens, and leaving the location line blank
      // until the listing arrives reads as "no folder" on a slow filesystem
      // (a cold cluster mount can take a second).
      if (path) currentEl.textContent = path;
      listEl.setAttribute("aria-busy", "true");
      let listing;
      try {
        listing = await api("/api/project/browse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: path || "" }),
        });
      } catch (err) {
        // A field's own value can point to a directory that doesn't exist
        // yet -- e.g. Import's fn_in_raw defaults to RELION's own example
        // "Micrographs/*.tif", and a fresh project has no Micrographs/
        // folder -- so fall back to the project root once rather than
        // stranding the user on a bare error with nowhere to click.
        if (path && cachedProjectPath && path !== cachedProjectPath) {
          listEl.removeAttribute("aria-busy");
          show(cachedProjectPath);
          return;
        }
        listEl.innerHTML = `<div class="browser-entry picker-note">Could not open: ${escapeHtml(err.message)}</div>`;
        listEl.removeAttribute("aria-busy");
        return;
      }
      listEl.removeAttribute("aria-busy");
      currentEl.textContent = listing.path;
      pathInput.value = listing.path;
      listEl.innerHTML = "";

      if (listing.parent) {
        const up = document.createElement("div");
        up.className = "browser-entry";
        up.textContent = "⬆ ..";
        up.addEventListener("click", () => show(listing.parent));
        listEl.appendChild(up);
      }
      const base = listing.path.replace(/\/+$/, "");
      const dirs = listing.entries.filter((e) => e.is_dir);
      const files = listing.entries.filter((e) => !e.is_dir && matches(e.name));

      dirs.forEach((entry) => {
        const row = document.createElement("div");
        row.className = "browser-entry";
        row.textContent = "📁 " + entry.name;
        row.addEventListener("click", () => show(`${base}/${entry.name}`));
        listEl.appendChild(row);
      });
      files.forEach((entry) => {
        const row = document.createElement("div");
        row.className = "browser-entry picker-file";
        row.textContent = "📄 " + entry.name;
        row.addEventListener("click", () => finish(toProjectRelative(`${base}/${entry.name}`)));
        listEl.appendChild(row);
      });
      if (!dirs.length && !files.length) {
        const none = document.createElement("div");
        none.className = "browser-entry picker-note";
        none.textContent = exts.length
          ? `(no subfolders, and no ${exts.join(" / ")} files here)`
          : "(empty)";
        listEl.appendChild(none);
      }
    }

    overlay.querySelector('[data-role="pick-cancel"]').addEventListener("click", () => finish(null));
    overlay.querySelector('[data-role="pick-go"]').addEventListener("click", () => show(pathInput.value.trim()));
    pathInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); show(pathInput.value.trim()); }
    });
    overlay.addEventListener("click", (e) => { if (e.target === overlay) finish(null); });
    document.addEventListener("keydown", function onEsc(e) {
      if (e.key === "Escape" && document.body.contains(overlay)) {
        document.removeEventListener("keydown", onEsc);
        finish(null);
      }
    });

    show(startPath || cachedProjectPath || "");
  });
}

// Job types with a live Progress tab. Must match backend progress.PROGRESS_JOBS
// (the backend is authoritative — it returns supported:false and the tab hides
// itself if these ever drift).
const PROGRESS_JOB_TYPES = new Set([
  "Class2D", "Class3D", "Autorefine", "Inimodel", "MultiBody", "TomoReconPart",
]);

// Job types with an end-of-job CTF QC tab. Must match backend
// ctf_qc.supports_ctf_qc (the backend is authoritative — see PROGRESS_JOB_TYPES
// above for why).
const CTF_QC_JOB_TYPES = new Set(["Ctffind"]);

// Job types with a meaningful 3D viewing-direction plot -- PROGRESS_JOB_TYPES
// minus Class2D, whose particles have no rlnAngleRot/rlnAngleTilt at all
// (only an in-plane rlnAnglePsi). Must match backend
// progress.ORIENTATION_DISTRIBUTION_JOBS.
const ORIENTATION_JOB_TYPES = new Set(
  [...PROGRESS_JOB_TYPES].filter((t) => t !== "Class2D")
);

// ==========================================================================
// Small SVG charts for the job Progress tab.
// Hand-rolled rather than pulling in a charting library: the whole frontend is
// dependency-free and offline-capable (HPC login nodes often have no outbound
// internet), and these are two simple forms. Colours are read from the CSS
// theme variables so both charts follow the dark/light switch, and each is
// redrawn on `relion-us-theme-changed`.
// ==========================================================================

function themeColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (cs.getPropertyValue(name) || fallback).trim();
  return {
    s1: v("--series-1", "#3987e5"),
    s2: v("--series-2", "#d95926"),
    text: v("--text", "#e6e9ee"),
    dim: v("--text-dim", "#9aa4b2"),
    grid: v("--grid", "#384049"),
    surface: v("--panel", "#23272e"),
  };
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(name, attrs = {}) {
  const el = document.createElementNS(SVG_NS, name);
  for (const [k, val] of Object.entries(attrs)) el.setAttribute(k, String(val));
  return el;
}

// Line chart: resolution (Å) against iteration. Two series max, one shared
// y-axis (never a second scale — two measures of different scale would be two
// charts). Lower Å is better, stated in the axis label rather than inverting
// the axis, which reads as a trick.
function drawResolutionChart(host, iterations) {
  host.innerHTML = "";
  const series = [
    { key: "resolution_A", label: "Current", color: "s1" },
    { key: "best_class_resolution_A", label: "Best class", color: "s2" },
  ].filter((sr) => iterations.some((p) => p[sr.key] != null));
  if (!series.length) {
    host.innerHTML = '<div class="progress-empty">No resolution numbers reported yet.</div>';
    return;
  }
  const c = themeColors();
  const W = 460, H = 180, ML = 46, MR = 58, MT = 22, MB = 26;
  const plotW = W - ML - MR, plotH = H - MT - MB;
  const its = iterations.map((p) => p.iteration);
  const xMin = Math.min(...its), xMax = Math.max(...its);
  const vals = [];
  series.forEach((sr) => iterations.forEach((p) => { if (p[sr.key] != null) vals.push(p[sr.key]); }));
  let yMin = Math.min(...vals), yMax = Math.max(...vals);
  if (yMax - yMin < 1e-9) { yMin -= 1; yMax += 1; }
  const pad = (yMax - yMin) * 0.1;
  yMin -= pad; yMax += pad;
  const X = (i) => ML + (xMax === xMin ? plotW / 2 : ((i - xMin) / (xMax - xMin)) * plotW);
  const Y = (v) => MT + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "progress-chart", role: "img" });
  svg.appendChild(svgEl("title", {})).textContent = "Resolution by iteration (lower Å is better)";

  // recessive gridlines + y labels
  for (let t = 0; t <= 3; t++) {
    const v = yMin + ((yMax - yMin) * t) / 3, y = Y(v);
    svg.appendChild(svgEl("line", { x1: ML, y1: y, x2: ML + plotW, y2: y, stroke: c.grid, "stroke-width": 1, opacity: 0.5 }));
    const lab = svgEl("text", { x: ML - 6, y: y + 3, "text-anchor": "end", fill: c.dim, "font-size": 9 });
    lab.textContent = v.toFixed(1);
    svg.appendChild(lab);
  }
  const yTitle = svgEl("text", { x: 0, y: 9, fill: c.dim, "font-size": 9 });
  yTitle.textContent = "Resolution, Å (lower = better)";
  svg.appendChild(yTitle);

  // x labels: first and last iteration only, to stay uncluttered
  [xMin, xMax].forEach((i, idx) => {
    const t = svgEl("text", { x: X(i), y: H - 8, "text-anchor": idx ? "end" : "start", fill: c.dim, "font-size": 9 });
    t.textContent = `it ${i}`;
    svg.appendChild(t);
  });

  series.forEach((sr) => {
    const pts = iterations.filter((p) => p[sr.key] != null);
    if (!pts.length) return;
    const d = pts.map((p, i) => `${i ? "L" : "M"}${X(p.iteration).toFixed(1)},${Y(p[sr.key]).toFixed(1)}`).join(" ");
    svg.appendChild(svgEl("path", {
      d, fill: "none", stroke: c[sr.color], "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
    const last = pts[pts.length - 1];
    // 2px surface ring so overlapping end markers stay separable
    svg.appendChild(svgEl("circle", {
      cx: X(last.iteration), cy: Y(last[sr.key]), r: 4,
      fill: c[sr.color], stroke: c.surface, "stroke-width": 2,
    }));
    // direct label on the final point (≤4 series, so both get one)
    const lab = svgEl("text", {
      x: X(last.iteration) + 8, y: Y(last[sr.key]) + 3, fill: c.text, "font-size": 10,
    });
    lab.textContent = `${last[sr.key].toFixed(1)} Å`;
    svg.appendChild(lab);
  });

  // hover: nearest iteration, crosshair + tooltip
  const hoverLine = svgEl("line", { y1: MT, y2: MT + plotH, stroke: c.dim, "stroke-width": 1, opacity: 0 });
  svg.appendChild(hoverLine);
  const hit = svgEl("rect", { x: ML, y: MT, width: plotW, height: plotH, fill: "transparent" });
  svg.appendChild(hit);
  const tip = document.createElement("div");
  tip.className = "progress-tooltip hidden";
  host.appendChild(svg);
  host.appendChild(tip);
  hit.addEventListener("mousemove", (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * W;
    let nearest = iterations[0];
    iterations.forEach((p) => {
      if (Math.abs(X(p.iteration) - px) < Math.abs(X(nearest.iteration) - px)) nearest = p;
    });
    hoverLine.setAttribute("x1", X(nearest.iteration));
    hoverLine.setAttribute("x2", X(nearest.iteration));
    hoverLine.setAttribute("opacity", "0.6");
    tip.classList.remove("hidden");
    tip.style.left = `${(X(nearest.iteration) / W) * 100}%`;
    tip.innerHTML = `<b>Iteration ${nearest.iteration}</b>` +
      series.map((sr) => nearest[sr.key] == null ? "" :
        `<br><span class="tip-swatch" style="background:${c[sr.color]}"></span>${escapeHtml(sr.label)}: ${nearest[sr.key].toFixed(2)} Å`).join("");
  });
  hit.addEventListener("mouseleave", () => {
    hoverLine.setAttribute("opacity", "0");
    tip.classList.add("hidden");
  });

  if (series.length >= 2) {
    const legend = document.createElement("div");
    legend.className = "progress-legend";
    legend.innerHTML = series.map((sr) =>
      `<span><span class="tip-swatch" style="background:${c[sr.color]}"></span>${escapeHtml(sr.label)}</span>`).join("");
    host.appendChild(legend);
  }
}

// Line chart: one arbitrary numeric field against iteration -- a single-
// series sibling of drawResolutionChart, for values with their OWN unit
// (degrees, Å) that must not share an axis with resolution or with each
// other ("never a second scale" -- see drawResolutionChart's own comment).
// Used for the Progress tab's rotational/translational sampling-accuracy
// charts, whose numbers were already being parsed out of every iteration's
// model.star for the class list (see progress.py's accuracy_rotation_deg/
// accuracy_translation_A) but never plotted before.
function drawAccuracyChart(host, iterations, { key, label, unit = "", color = "s1" }) {
  host.innerHTML = "";
  const pts = iterations.filter((p) => p[key] != null);
  if (!pts.length) {
    host.innerHTML = '<div class="progress-empty">Not reported for this job.</div>';
    return;
  }
  const c = themeColors();
  const W = 460, H = 150, ML = 46, MR = 20, MT = 18, MB = 26;
  const plotW = W - ML - MR, plotH = H - MT - MB;
  const xMin = Math.min(...iterations.map((p) => p.iteration));
  const xMax = Math.max(...iterations.map((p) => p.iteration));
  let yMin = Math.min(...pts.map((p) => p[key])), yMax = Math.max(...pts.map((p) => p[key]));
  if (yMax - yMin < 1e-9) { yMin -= 1; yMax += 1; }
  const pad = (yMax - yMin) * 0.1;
  yMin -= pad; yMax += pad;
  const X = (i) => ML + (xMax === xMin ? plotW / 2 : ((i - xMin) / (xMax - xMin)) * plotW);
  const Y = (v) => MT + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "progress-chart", role: "img" });
  svg.appendChild(svgEl("title", {})).textContent = `${label} by iteration`;

  for (let t = 0; t <= 2; t++) {
    const v = yMin + ((yMax - yMin) * t) / 2, y = Y(v);
    svg.appendChild(svgEl("line", { x1: ML, y1: y, x2: ML + plotW, y2: y, stroke: c.grid, "stroke-width": 1, opacity: 0.5 }));
    const lab = svgEl("text", { x: ML - 6, y: y + 3, "text-anchor": "end", fill: c.dim, "font-size": 9 });
    lab.textContent = v.toFixed(2);
    svg.appendChild(lab);
  }
  [xMin, xMax].forEach((i, idx) => {
    const t = svgEl("text", { x: X(i), y: H - 8, "text-anchor": idx ? "end" : "start", fill: c.dim, "font-size": 9 });
    t.textContent = `it ${i}`;
    svg.appendChild(t);
  });

  const d = pts.map((p, i) => `${i ? "L" : "M"}${X(p.iteration).toFixed(1)},${Y(p[key]).toFixed(1)}`).join(" ");
  svg.appendChild(svgEl("path", {
    d, fill: "none", stroke: c[color], "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));
  const last = pts[pts.length - 1];
  svg.appendChild(svgEl("circle", {
    cx: X(last.iteration), cy: Y(last[key]), r: 4, fill: c[color], stroke: c.surface, "stroke-width": 2,
  }));
  const lab = svgEl("text", { x: X(last.iteration) + 8, y: Y(last[key]) + 3, fill: c.text, "font-size": 10 });
  lab.textContent = `${last[key].toFixed(2)}${unit}`;
  svg.appendChild(lab);

  const hoverLine = svgEl("line", { y1: MT, y2: MT + plotH, stroke: c.dim, "stroke-width": 1, opacity: 0 });
  svg.appendChild(hoverLine);
  const hit = svgEl("rect", { x: ML, y: MT, width: plotW, height: plotH, fill: "transparent" });
  svg.appendChild(hit);
  const tip = document.createElement("div");
  tip.className = "progress-tooltip hidden";
  host.appendChild(svg);
  host.appendChild(tip);
  hit.addEventListener("mousemove", (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * W;
    let nearest = pts[0];
    pts.forEach((p) => { if (Math.abs(X(p.iteration) - px) < Math.abs(X(nearest.iteration) - px)) nearest = p; });
    hoverLine.setAttribute("x1", X(nearest.iteration));
    hoverLine.setAttribute("x2", X(nearest.iteration));
    hoverLine.setAttribute("opacity", "0.6");
    tip.classList.remove("hidden");
    tip.style.left = `${(X(nearest.iteration) / W) * 100}%`;
    tip.innerHTML = `<b>Iteration ${nearest.iteration}</b><br>${escapeHtml(label)}: ${nearest[key].toFixed(2)}${unit}`;
  });
  hit.addEventListener("mouseleave", () => {
    hoverLine.setAttribute("opacity", "0");
    tip.classList.add("hidden");
  });
}

// Bar chart: share of particles per class, latest iteration. One series, so no
// legend — the heading names it.
function drawClassDistributionChart(host, classes) {
  host.innerHTML = "";
  if (!classes.length) {
    host.innerHTML = '<div class="progress-empty">No class distribution reported yet.</div>';
    return;
  }
  const c = themeColors();
  const W = 460, barH = 16, gap = 6, ML = 34, MR = 46, MT = 6;
  const H = MT + classes.length * (barH + gap);
  const plotW = W - ML - MR;
  const maxV = Math.max(...classes.map((k) => k.distribution), 0.0001);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "progress-chart", role: "img" });
  svg.appendChild(svgEl("title", {})).textContent = "Share of particles per class";
  const tip = document.createElement("div");
  tip.className = "progress-tooltip hidden";

  classes.forEach((k, i) => {
    const y = MT + i * (barH + gap);
    const w = Math.max(1, (k.distribution / maxV) * plotW);
    const lab = svgEl("text", { x: ML - 6, y: y + barH - 4, "text-anchor": "end", fill: c.dim, "font-size": 9 });
    lab.textContent = `#${k.index}`;
    svg.appendChild(lab);
    // 4px rounded data-end, anchored to the baseline
    const bar = svgEl("rect", { x: ML, y, width: w, height: barH, rx: 4, fill: c.s1 });
    svg.appendChild(bar);
    const val = svgEl("text", { x: ML + w + 6, y: y + barH - 4, fill: c.text, "font-size": 10 });
    val.textContent = `${(k.distribution * 100).toFixed(1)}%`;
    svg.appendChild(val);
    bar.addEventListener("mouseenter", () => {
      tip.classList.remove("hidden");
      tip.style.left = "10%";
      tip.innerHTML = `<b>Class ${k.index}</b><br>${(k.distribution * 100).toFixed(1)}% of particles` +
        (k.resolution_A != null ? `<br>${k.resolution_A.toFixed(2)} Å` : "");
    });
    bar.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  });
  host.appendChild(svg);
  host.appendChild(tip);
}

// Line chart: DefocusU/DefocusV against micrograph order, for the CTF QC tab.
// Same shape as drawResolutionChart (two series, one shared y-axis, hover
// crosshair) but plotted against micrograph INDEX rather than iteration --
// CTF Estimation has no notion of iterations, just the order RELION joined
// the micrographs in.
function drawDefocusTrendChart(host, micrographs) {
  host.innerHTML = "";
  const series = [
    { key: "defocus_u", label: "Defocus U", color: "s1" },
    { key: "defocus_v", label: "Defocus V", color: "s2" },
  ].filter((sr) => micrographs.some((m) => m[sr.key] != null));
  if (!series.length) {
    host.innerHTML = '<div class="progress-empty">No defocus numbers reported.</div>';
    return;
  }
  const c = themeColors();
  const W = 460, H = 180, ML = 54, MR = 20, MT = 22, MB = 26;
  const plotW = W - ML - MR, plotH = H - MT - MB;
  const n = micrographs.length;
  const vals = [];
  series.forEach((sr) => micrographs.forEach((m) => { if (m[sr.key] != null) vals.push(m[sr.key]); }));
  let yMin = Math.min(...vals), yMax = Math.max(...vals);
  if (yMax - yMin < 1e-9) { yMin -= 1; yMax += 1; }
  const pad = (yMax - yMin) * 0.1;
  yMin -= pad; yMax += pad;
  const X = (i) => ML + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const Y = (v) => MT + plotH - ((v - yMin) / (yMax - yMin)) * plotH;
  // Defocus values run into the tens of thousands (Å) — µm reads far easier.
  const um = (v) => (v / 10000).toFixed(2);

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "progress-chart", role: "img" });
  svg.appendChild(svgEl("title", {})).textContent = "Defocus by micrograph order";

  for (let t = 0; t <= 3; t++) {
    const v = yMin + ((yMax - yMin) * t) / 3, y = Y(v);
    svg.appendChild(svgEl("line", { x1: ML, y1: y, x2: ML + plotW, y2: y, stroke: c.grid, "stroke-width": 1, opacity: 0.5 }));
    const lab = svgEl("text", { x: ML - 6, y: y + 3, "text-anchor": "end", fill: c.dim, "font-size": 9 });
    lab.textContent = `${um(v)}`;
    svg.appendChild(lab);
  }
  const yTitle = svgEl("text", { x: 0, y: 9, fill: c.dim, "font-size": 9 });
  yTitle.textContent = "Defocus, µm";
  svg.appendChild(yTitle);
  [0, n - 1].forEach((i, idx) => {
    if (i < 0) return;
    const t = svgEl("text", { x: X(i), y: H - 8, "text-anchor": idx ? "end" : "start", fill: c.dim, "font-size": 9 });
    t.textContent = `#${i + 1}`;
    svg.appendChild(t);
  });

  series.forEach((sr) => {
    const pts = micrographs.map((m, i) => [i, m[sr.key]]).filter(([, v]) => v != null);
    if (!pts.length) return;
    const d = pts.map(([i, v], k) => `${k ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
    svg.appendChild(svgEl("path", { d, fill: "none", stroke: c[sr.color], "stroke-width": 1.5, opacity: 0.85 }));
  });

  const hoverLine = svgEl("line", { y1: MT, y2: MT + plotH, stroke: c.dim, "stroke-width": 1, opacity: 0 });
  svg.appendChild(hoverLine);
  const hit = svgEl("rect", { x: ML, y: MT, width: plotW, height: plotH, fill: "transparent" });
  svg.appendChild(hit);
  const tip = document.createElement("div");
  tip.className = "progress-tooltip hidden";
  host.appendChild(svg);
  host.appendChild(tip);
  hit.addEventListener("mousemove", (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * W;
    let nearest = 0, best = Infinity;
    for (let i = 0; i < n; i++) {
      const dist = Math.abs(X(i) - px);
      if (dist < best) { best = dist; nearest = i; }
    }
    const m = micrographs[nearest];
    hoverLine.setAttribute("x1", X(nearest));
    hoverLine.setAttribute("x2", X(nearest));
    hoverLine.setAttribute("opacity", "0.6");
    tip.classList.remove("hidden");
    tip.style.left = `${(X(nearest) / W) * 100}%`;
    tip.innerHTML = `<b>${escapeHtml(m.name)}</b>` +
      series.map((sr) => m[sr.key] == null ? "" :
        `<br><span class="tip-swatch" style="background:${c[sr.color]}"></span>${escapeHtml(sr.label)}: ${um(m[sr.key])} µm`).join("");
  });
  hit.addEventListener("mouseleave", () => {
    hoverLine.setAttribute("opacity", "0");
    tip.classList.add("hidden");
  });

  const legend = document.createElement("div");
  legend.className = "progress-legend";
  legend.innerHTML = series.map((sr) =>
    `<span><span class="tip-swatch" style="background:${c[sr.color]}"></span>${escapeHtml(sr.label)}</span>`).join("");
  host.appendChild(legend);
}

// Histogram: bins a flat array of numbers into ~14 buckets and draws vertical
// bars. Used for the CTF QC tab's defocus/astigmatism/max-resolution/FOM
// distributions -- one series each, so no legend, just a hover tooltip
// giving the bucket's range and count.
function drawHistogramChart(host, values, { unit = "", color = "s1" } = {}) {
  host.innerHTML = "";
  const nums = values.filter((v) => v != null && Number.isFinite(v));
  if (!nums.length) {
    host.innerHTML = '<div class="progress-empty">No values reported.</div>';
    return;
  }
  const c = themeColors();
  const W = 460, H = 170, ML = 34, MR = 12, MT = 10, MB = 28;
  const plotW = W - ML - MR, plotH = H - MT - MB;
  let vMin = Math.min(...nums), vMax = Math.max(...nums);
  if (vMax - vMin < 1e-9) { vMin -= 0.5; vMax += 0.5; }
  const NBINS = Math.min(14, Math.max(5, Math.round(Math.sqrt(nums.length))));
  const binW = (vMax - vMin) / NBINS;
  const counts = new Array(NBINS).fill(0);
  nums.forEach((v) => {
    let idx = Math.floor((v - vMin) / binW);
    if (idx >= NBINS) idx = NBINS - 1;
    if (idx < 0) idx = 0;
    counts[idx]++;
  });
  const maxCount = Math.max(...counts, 1);
  const barGap = 2;
  const barW = plotW / NBINS - barGap;
  const X = (i) => ML + i * (plotW / NBINS) + barGap / 2;
  const Y = (v) => MT + plotH - (v / maxCount) * plotH;

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "progress-chart", role: "img" });
  svg.appendChild(svgEl("title", {})).textContent = "Distribution";
  const tip = document.createElement("div");
  tip.className = "progress-tooltip hidden";

  for (let t = 0; t <= 2; t++) {
    const v = (maxCount * t) / 2, y = Y(v);
    svg.appendChild(svgEl("line", { x1: ML, y1: y, x2: ML + plotW, y2: y, stroke: c.grid, "stroke-width": 1, opacity: 0.5 }));
    const lab = svgEl("text", { x: ML - 6, y: y + 3, "text-anchor": "end", fill: c.dim, "font-size": 9 });
    lab.textContent = Math.round(v);
    svg.appendChild(lab);
  }
  [vMin, vMax].forEach((v, idx) => {
    const t = svgEl("text", {
      x: idx ? ML + plotW : ML, y: H - 6,
      "text-anchor": idx ? "end" : "start", fill: c.dim, "font-size": 9,
    });
    t.textContent = `${v.toFixed(1)}${unit}`;
    svg.appendChild(t);
  });

  counts.forEach((count, i) => {
    const y = Y(count);
    const bar = svgEl("rect", { x: X(i), y, width: Math.max(1, barW), height: MT + plotH - y, fill: c[color] });
    svg.appendChild(bar);
    bar.addEventListener("mouseenter", () => {
      tip.classList.remove("hidden");
      tip.style.left = `${((X(i) + barW / 2) / W) * 100}%`;
      const lo = vMin + i * binW, hi = lo + binW;
      tip.innerHTML = `<b>${count}</b> micrograph${count === 1 ? "" : "s"}<br>${lo.toFixed(2)}–${hi.toFixed(2)}${unit}`;
    });
    bar.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  });
  host.appendChild(svg);
  host.appendChild(tip);
}

// Heatmap: particle count per (rot, tilt) bin -- the classic "is the sphere
// actually covered, or stuck in a couple of preferred views" viewing-
// direction QC plot. Grid cells rather than a projected sphere: this
// codebase's whole charting approach is small hand-rolled SVG, and a flat
// rot-x-tilt grid needs no projection math to read correctly, at the cost
// of the poles looking stretched (the same tradeoff an equirectangular map
// makes) -- an acceptable one for spotting a missing wedge of orientations,
// which is what this plot is actually for.
function drawOrientationHeatmap(host, data) {
  host.innerHTML = "";
  const { counts, n_rot_bins: nRot, n_tilt_bins: nTilt } = data;
  const maxCount = Math.max(...counts.map((row) => Math.max(...row)), 1);
  if (maxCount <= 1 && counts.every((row) => row.every((v) => v === 0))) {
    host.innerHTML = '<div class="progress-empty">No orientation data in this iteration.</div>';
    return;
  }
  const c = themeColors();
  const W = 460, H = 240, ML = 54, MR = 12, MT = 10, MB = 30;
  const plotW = W - ML - MR, plotH = H - MT - MB;
  const cellW = plotW / nRot, cellH = plotH / nTilt;

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "progress-chart", role: "img" });
  svg.appendChild(svgEl("title", {})).textContent = "Viewing-direction distribution";
  const tip = document.createElement("div");
  tip.className = "progress-tooltip hidden";

  // Accent-tinted intensity: 0 count stays the panel colour (invisible
  // against the chart background), highest count reaches full accent —
  // a single hue reads as "more/less", unlike a rainbow scale that implies
  // categories that don't exist here.
  const heat = (v) => {
    const t = v / maxCount;
    return `color-mix(in srgb, ${c.surface} ${(1 - t) * 100}%, ${c.s1} ${t * 100}%)`;
  };

  for (let ti = 0; ti < nTilt; ti++) {
    for (let ri = 0; ri < nRot; ri++) {
      const count = counts[ti][ri];
      const rect = svgEl("rect", {
        x: ML + ri * cellW, y: MT + ti * cellH,
        width: Math.ceil(cellW) + 0.5, height: Math.ceil(cellH) + 0.5,
        fill: count ? heat(count) : c.surface,
      });
      svg.appendChild(rect);
      if (count) {
        rect.addEventListener("mouseenter", () => {
          tip.classList.remove("hidden");
          tip.style.left = `${((ML + (ri + 0.5) * cellW) / W) * 100}%`;
          const rot = (ri / nRot) * 360 - 180, tilt = (ti / nTilt) * 180;
          tip.innerHTML = `<b>${count.toLocaleString()}</b> particle${count === 1 ? "" : "s"}` +
            `<br>rot ${rot.toFixed(0)}° · tilt ${tilt.toFixed(0)}°`;
        });
        rect.addEventListener("mouseleave", () => tip.classList.add("hidden"));
      }
    }
  }

  [-180, 0, 180].forEach((v) => {
    const x = ML + ((v + 180) / 360) * plotW;
    const t = svgEl("text", { x, y: H - 10, "text-anchor": "middle", fill: c.dim, "font-size": 9 });
    t.textContent = `${v}°`;
    svg.appendChild(t);
  });
  svg.appendChild((() => {
    const t = svgEl("text", { x: ML + plotW / 2, y: H - 1, "text-anchor": "middle", fill: c.dim, "font-size": 9 });
    t.textContent = "rot";
    return t;
  })());
  [0, 90, 180].forEach((v) => {
    const y = MT + (v / 180) * plotH;
    const t = svgEl("text", { x: ML - 6, y: y + 3, "text-anchor": "end", fill: c.dim, "font-size": 9 });
    t.textContent = `${v}°`;
    svg.appendChild(t);
  });
  const yTitle = svgEl("text", { x: 0, y: 9, fill: c.dim, "font-size": 9 });
  yTitle.textContent = "tilt";
  svg.appendChild(yTitle);

  host.appendChild(svg);
  host.appendChild(tip);
}

// ==========================================================================
// Theme (dark default / light alternative)
// The whole stylesheet is written against CSS variables, so switching themes
// is just stamping data-theme on <html>. Dark stays the default -- the light
// theme is opt-in and remembered.
// ==========================================================================
const THEME_STORAGE_KEY = "relion_us_theme";

function setTheme(theme, { persist = true } = {}) {
  const value = theme === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", value);
  const btn = document.getElementById("themeBtn");
  if (btn) {
    // The button shows the CURRENT theme, and its title says what clicking does.
    btn.textContent = value === "light" ? "☀ Light" : "🌙 Dark";
    btn.title = value === "light" ? "Switch to the dark theme" : "Switch to the light theme";
  }
  if (persist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, value);
    } catch (e) {
      // Non-fatal — the theme just won't be remembered across reloads.
    }
  }
  // Charts are drawn with resolved colours, so they need a repaint on switch.
  document.dispatchEvent(new CustomEvent("relion-us-theme-changed", { detail: { theme: value } }));
}

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

(function initTheme() {
  let stored = null;
  try {
    stored = localStorage.getItem(THEME_STORAGE_KEY);
  } catch (e) { /* storage unavailable */ }
  // A remembered choice always wins. With no choice stored we keep dark, which
  // is this app's designed default, rather than following the OS.
  setTheme(stored === "light" ? "light" : "dark", { persist: false });
  document.getElementById("themeBtn").addEventListener("click", () => {
    setTheme(currentTheme() === "light" ? "dark" : "light");
  });
})();

refreshProjectLabel();
refreshCommandCenter();
