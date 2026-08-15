// app.js — RELION-US frontend. Vanilla JS, no build step.
// Each job opened from the sidebar (or reopened from the Command Center)
// becomes an independent WinBox popup — draggable, resizable, minimizable,
// several open at once — mounted with a form built from the job definition
// the backend serves. See style.css for the popup's internal layout (standard
// fields on top, tabs, editable command box, live output at the bottom).


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

// --- Lightweight custom confirm/prompt dialogs -----------------------------
// Deliberately never native confirm()/prompt() (or errorDialog()): those are
// modal at the OS/browser level and block the whole page, including
// anything driving it programmatically (e.g. Playwright) -- this app
// already avoids native errorDialog() for project-switch errors for the same
// reason (see the #projectModalError/#notAProjectError banners). These
// build a throwaway overlay, resolve a promise, and remove themselves.

function statusLineClass(status) {
  if (status === "completed") return "status-line ok";
  if (status === "failed" || status === "aborted") return "status-line failed";
  return "status-line";
}

// Error reporting matches the confirm/prompt dialogs below. Native errorDialog() is
// deliberately avoided everywhere in this file (see the note on confirmDialog):
// it blocks the whole page at the browser level, including anything driving
// the UI programmatically, which is exactly what the Playwright suites do.
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
  // persist=false is used for the project's auto-detected pipeline hint. That
  // hint is a convenience, not a preference: writing it to localStorage
  // overwrote the user's own deliberate choice (it shares the same key), so
  // picking "All" in a tomo project was silently undone on the next reload.
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
});

const zoomSlider = document.getElementById("zoomSlider");
const zoomValue = document.getElementById("zoomValue");
zoomSlider.addEventListener("input", () => {
  const pct = zoomSlider.value;
  zoomValue.textContent = pct + "%";
  document.getElementById("layout").style.zoom = pct / 100;
});

// --- Field rendering -------------------------------------------------------

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
  const prefillValues = (isReopen && existingRun.field_values) || def.default_values || {};

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
    <div class="job-standard-form" data-role="standard-form"></div>
    <div class="tab-bar" data-role="tab-bar">
      <button class="tab-btn active" data-tab="advanced">Advanced</button>
      <button class="tab-btn" data-tab="progress" hidden>Progress</button>
      <button class="tab-btn" data-tab="outputs" hidden>Outputs</button>
      <button class="tab-btn" data-tab="errors">Errors<span class="badge" data-role="error-badge" style="display:none">0</span></button>
      ${def.is_custom ? "" : '<button class="tab-btn" data-tab="source">RELION Source</button>'}
    </div>
    <div class="tab-content active" data-tab-content="advanced"></div>
    <div class="tab-content" data-tab-content="progress"></div>
    <div class="tab-content" data-tab-content="outputs"></div>
    <div class="tab-content" data-tab-content="errors"><pre class="errors-pre" data-role="errors-pre">(no errors yet)</pre></div>
    ${def.is_custom ? "" : `<div class="tab-content" data-tab-content="source"><pre class="source-pre">${escapeHtml(def.commands_source || "(source unavailable)")}</pre></div>`}
    ${def.is_custom ? "" : `
    <div class="command-row" data-role="command-row">
      <label>Command (edit freely — this exact string runs, nothing added or removed under the hood)
        <button class="btn" data-role="recompute-btn" style="padding:2px 8px;">Recompute draft</button>
      </label>
      <textarea class="command-box" data-role="command-box"></textarea>
      <div class="command-actions">
        <button class="btn primary" data-role="run-btn">Run</button>
        <span class="status-line" data-role="status-line"></span>
      </div>
    </div>`}
    ${def.is_custom ? `
    <div class="command-row" data-role="command-row">
      <div class="command-actions">
        <button class="btn primary" data-role="run-btn">Run</button>
        <span class="status-line" data-role="status-line"></span>
      </div>
    </div>` : ""}
    <div class="live-output" data-role="live-output"></div>
  `;

  // Standard fields
  const standardForm = body.querySelector('[data-role="standard-form"]');
  for (const key of def.standard_fields || []) {
    const opt = optionsByKey[key];
    if (!opt) continue;
    const val = prefillValues[key];
    standardForm.appendChild(buildFieldRow(key, opt, val));
  }

  // Advanced fields, grouped by RELION's own real tab names
  const advancedContent = body.querySelector('[data-tab-content="advanced"]');
  const advancedGroups = def.advanced_groups || {};
  if (Object.keys(advancedGroups).length === 0) {
    // custom jobs / jobs with no second tab: show any non-standard options here flat
    const shown = new Set(def.standard_fields || []);
    (def.options || []).forEach((opt) => {
      if (shown.has(opt.key)) return;
      const val = prefillValues[opt.key];
      const row = document.createElement("div");
      row.className = "job-standard-form";
      row.style.borderBottom = "none";
      row.style.maxHeight = "none";
      row.appendChild(buildFieldRow(opt.key, opt, val));
      advancedContent.appendChild(row);
    });
  } else {
    for (const [groupName, keys] of Object.entries(advancedGroups)) {
      const title = document.createElement("div");
      title.className = "advanced-group-title";
      title.textContent = groupName;
      advancedContent.appendChild(title);
      const grid = document.createElement("div");
      grid.className = "job-standard-form";
      grid.style.border = "none";
      grid.style.maxHeight = "none";
      grid.style.padding = "0";
      for (const key of keys) {
        const opt = optionsByKey[key];
        if (!opt) continue;
        const val = prefillValues[key];
        grid.appendChild(buildFieldRow(key, opt, val));
      }
      advancedContent.appendChild(grid);
    }
  }

  // Tab switching
  body.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.hidden) return;
      body.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      body.querySelectorAll(".tab-content").forEach((c) => c.classList.remove("active"));
      btn.classList.add("active");
      body.querySelector(`[data-tab-content="${btn.dataset.tab}"]`).classList.add("active");
      if (btn.dataset.tab === "outputs") loadOutputsTab();
      if (btn.dataset.tab === "progress") refreshProgress();
    });
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
          body: JSON.stringify({ field_values: collectValues(), output_subdir: popupOutputSubdir }),
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

  const liveOutput = body.querySelector('[data-role="live-output"]');
  const statusLine = body.querySelector('[data-role="status-line"]');
  const errorsPre = body.querySelector('[data-role="errors-pre"]');
  const errorBadge = body.querySelector('[data-role="error-badge"]');
  let errorLines = [];
  let ws = null;

  function appendOutputLine(text, isStderr) {
    const line = document.createElement("div");
    line.className = "output-line" + (isStderr ? " stderr" : "");
    line.textContent = text;
    liveOutput.appendChild(line);
    liveOutput.scrollTop = liveOutput.scrollHeight;
    if (isStderr) {
      errorLines.push(text);
      errorsPre.textContent = errorLines.join("\n");
      errorsPre.classList.add("has-errors");
      errorBadge.style.display = "";
      errorBadge.textContent = String(errorLines.length);
    }
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
  let progressTimer = null;
  let progressState = {
    enabled: true,          // "Live progress" — on by default for supported jobs
    everyN: 1,              // refresh thumbnails every N iterations (1 = every)
    keepAll: false,         // keep earlier iterations' thumbnails (off by default)
    lastThumbIteration: null,
    history: [],            // [{iteration, classes}] when keepAll is on
    data: null,
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
    jobNameDisplay.textContent = hasRun ? currentRun.job_name : displayName;
    overwriteBtn.hidden = !hasRun || status === "running";
    abortBtn.hidden = !hasRun || status !== "running";
    markFinishedBtn.hidden = !hasRun || status === "running" || status === "completed";
    markFailedBtn.hidden = !hasRun || status === "running" || status === "failed";
    deleteBtn.hidden = !hasRun || status === "running";
    outputsTabBtn.hidden = !hasRun;
    refreshProgressTabVisibility();
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
      else { payload.command = cmdToRun; payload.field_values = collectValues(); }
      const run = await api("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      // Only close once the run actually started -- closing first meant a
      // failed overwrite destroyed the user's edited command and field values
      // with nothing but an error message left behind.
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
        <label class="progress-num" title="Only refresh class images every N iterations. 1 = every iteration.">
          Images every
          <input type="number" data-role="prog-every" min="1" max="99" value="${progressState.everyN}" /> it
        </label>
        <label class="progress-check" title="Keep earlier iterations' images so you can compare. Off by default to bound memory.">
          <input type="checkbox" data-role="prog-keepall" ${progressState.keepAll ? "checked" : ""} /> Keep all
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
    });
    progressContent.querySelector('[data-role="prog-keepall"]').addEventListener("change", (e) => {
      progressState.keepAll = e.target.checked;
      if (!progressState.keepAll) progressState.history = [];
      renderProgressBody();
    });
  }

  function renderProgressBody() {
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
    if (statusEl) {
      statusEl.textContent = `iteration ${d.latest.iteration}` +
        (d.dimensionality ? ` · ${d.dimensionality}D` : "") +
        (d.nr_classes ? ` · ${d.nr_classes} class${d.nr_classes === 1 ? "" : "es"}` : "");
    }

    host.innerHTML = `
      <div class="progress-section"><h4>Resolution by iteration</h4><div data-role="chart-res"></div></div>
      <div class="progress-section"><h4>Particles per class (iteration ${d.latest.iteration})</h4><div data-role="chart-dist"></div></div>
      <div class="progress-section"><h4 data-role="thumbs-title"></h4><div data-role="thumbs"></div></div>
    `;
    drawResolutionChart(host.querySelector('[data-role="chart-res"]'), d.iterations);
    drawClassDistributionChart(host.querySelector('[data-role="chart-dist"]'), d.latest.classes);
    renderThumbnails(host);
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

  function renderThumbnails(host) {
    const d = progressState.data;
    const wrap = host.querySelector('[data-role="thumbs"]');
    const title = host.querySelector('[data-role="thumbs-title"]');
    if (!wrap || !d) return;
    const it = d.latest.iteration;
    // Honour "images every N iterations": show the newest iteration that is a
    // multiple of N (iteration 1 always counts, so something appears early).
    const showIt = (it % progressState.everyN === 0 || it === 1)
      ? it
      : (progressState.lastThumbIteration ?? null);
    if (showIt === it) {
      progressState.lastThumbIteration = it;
      if (progressState.keepAll && !progressState.history.some((h) => h.iteration === it)) {
        progressState.history.push({ iteration: it, classes: d.latest.classes });
      }
    }
    const is3D = d.dimensionality === 3;
    title.textContent = is3D ? "Class volumes (central slice)" : "Class averages";
    if (showIt == null) {
      wrap.innerHTML = `<div class="progress-empty">No image update yet — showing every ${progressState.everyN} iterations.</div>`;
      return;
    }
    if (progressState.keepAll && progressState.history.length) {
      wrap.innerHTML = progressState.history
        .slice()
        .reverse()
        .map((h) => `<div class="thumb-iter"><span class="thumb-iter-label">iteration ${h.iteration}</span>${thumbGridHtml(h.iteration, h.classes)}</div>`)
        .join("");
    } else {
      wrap.innerHTML = thumbGridHtml(showIt, d.latest.classes);
    }
  }

  function stopProgressPolling() {
    if (progressTimer) { clearTimeout(progressTimer); progressTimer = null; }
  }

  async function refreshProgress() {
    stopProgressPolling();
    if (!progressSupported() || !progressState.enabled) { renderProgressBody(); return; }
    try {
      const d = await api(`/api/runs/${currentRun.run_id}/progress`);
      progressState.data = d;
      if (d.supported === false) {
        progressTabBtn.hidden = true;
        return;
      }
      renderProgressBody();
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
  const onThemeChange = () => { if (progressContent.dataset.built) renderProgressBody(); };
  document.addEventListener("relion-us-theme-changed", onThemeChange);

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
        // Only re-enable on failure -- leaving it enabled after a successful
        // start let a second click launch a whole second job (new jobNNN) and
        // orphan the first job's websocket.
        runBtn.disabled = false;
      }
    });
  }

  const win = new WinBox({
    title: displayName,
    width: "660px",
    height: "740px",
    x: "center",
    y: "center",
    mount: body,
    class: ["no-full"],
    onclose: () => {
      stopProgressPolling();
      document.removeEventListener("relion-us-theme-changed", onThemeChange);
      if (ws) try { ws.close(); } catch (e) { /* noop */ }
      return false;
    },
  });
}

loadCatalog().catch((err) => {
  document.getElementById("ccHint").textContent = `Failed to load job catalog: ${err.message}`;
});

// --- Command Center (job history: table / timeline) -----------------------

const CC_VIEW_KEY = "relion_us_cc_view";
const CC_DIRECTION_KEY = "relion_us_cc_direction";
let ccRuns = [];
let ccView = "table";
let ccSort = { key: "started_at", dir: "desc" };
let ccDirection = "desc"; // timeline: 'desc' = newest first, 'asc' = oldest first
try {
  const savedView = localStorage.getItem(CC_VIEW_KEY);
  if (savedView === "table" || savedView === "timeline") ccView = savedView;
  const savedDir = localStorage.getItem(CC_DIRECTION_KEY);
  if (savedDir === "asc" || savedDir === "desc") ccDirection = savedDir;
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
      <td>${escapeHtml(run.job_name || "job???")}${run.note ? '<span class="cc-job-note-icon" title="' + escapeHtml(run.note) + '">📝</span>' : ""}</td>
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
      </div>
      <div class="cc-card-meta">${formatTimestamp(run.started_at)} · ${formatDuration(run.started_at, run.ended_at)}</div>
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

function renderCommandCenterViews() {
  document.getElementById("ccTableView").classList.toggle("hidden", ccView !== "table");
  document.getElementById("ccTimelineView").classList.toggle("hidden", ccView !== "timeline");
  document.getElementById("ccDirectionBtn").style.display = ccView === "timeline" ? "inline-block" : "none";
  document.getElementById("ccDirectionBtn").textContent = ccDirection === "desc" ? "Newest first ↓" : "Oldest first ↑";
  if (ccView === "table") renderTable();
  else renderTimeline();
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

function reopenRun(run) {
  // Note: openJobPopup fetches the job definition fresh from
  // /api/jobs/{internal_name} and uses ITS is_custom flag throughout, so we
  // don't need (and shouldn't try) to infer custom-vs-RELION from the run
  // summary here.
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
  ccDirection = ccDirection === "desc" ? "asc" : "desc";
  try { localStorage.setItem(CC_DIRECTION_KEY, ccDirection); } catch (e) { /* noop */ }
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

function openProjectModal() {
  projectModalOverlay.classList.remove("hidden");
  clearModalError(projectModalError);
  newFolderNameInput.value = "";
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
  body.innerHTML = `
    <div class="viz-inputs">
      <label>STAR (optimiser/tomograms/particles) or MRC:
        <span class="viz-input-row">
          <input type="text" data-role="viz-path" placeholder="e.g. Tomograms/job012/tomograms.star or TS_01.mrc" />
          <button type="button" class="btn" data-role="viz-browse-main" title="Browse for a STAR or MRC file on the machine running the backend">Browse…</button>
        </span>
      </label>
      <label>Particles/coords STAR (optional, if separate):
        <span class="viz-input-row">
          <input type="text" data-role="viz-particles" placeholder="e.g. particles.star" />
          <button type="button" class="btn" data-role="viz-browse-particles" title="Browse for a particles/coordinates STAR file">Browse…</button>
        </span>
      </label>
      <div class="viz-inputs-row">
        <button class="btn primary" data-role="viz-load">Load</button>
        <select data-role="viz-tomo" style="display:none"></select>
        <span class="status-line" data-role="viz-status"></span>
      </div>
    </div>
    <div class="viz-controls" data-role="viz-controls" style="display:none">
      <div class="viz-ctrl-row">
        <span>Axis:</span>
        <button class="btn viz-axis active" data-axis="z">XY (Z)</button>
        <button class="btn viz-axis" data-axis="y">XZ (Y)</button>
        <button class="btn viz-axis" data-axis="x">ZY (X)</button>
        <label class="viz-check"><input type="checkbox" data-role="viz-showpicks" checked /> Show picks</label>
      </div>
      <div class="viz-ctrl-row">
        <span data-role="viz-slice-label">Slice</span>
        <input type="range" data-role="viz-slice" min="0" max="0" value="0" style="flex:1" />
      </div>
      <div class="viz-ctrl-row">
        <span>Contrast</span>
        <input type="range" data-role="viz-lo" min="0" max="100" value="0" title="black point" />
        <input type="range" data-role="viz-hi" min="0" max="100" value="100" title="white point" />
      </div>
      <div class="viz-ctrl-row">
        <span>Pick Ø (vox)</span>
        <input type="range" data-role="viz-diam" min="2" max="80" value="16" />
        <span data-role="viz-diam-val">16</span>
        <span>Line</span>
        <input type="range" data-role="viz-width" min="1" max="6" value="2" />
      </div>
    </div>
    <div class="viz-stage" data-role="viz-stage" style="display:none">
      <div class="viz-image-wrap" data-role="viz-wrap">
        <img data-role="viz-img" alt="tomogram slice" />
        <canvas data-role="viz-overlay"></canvas>
      </div>
      <div class="viz-meta" data-role="viz-meta"></div>
    </div>
  `;

  const q = (sel) => body.querySelector(sel);
  const statusEl = q('[data-role="viz-status"]');
  const state = {
    mrc: null, particles: null, tomo: null, vinfo: null, picks: [],
    axis: "z", index: 0, lo: null, hi: null, diameter: 16, width: 2, showPicks: true,
  };

  function axisDims() {
    const v = state.vinfo;
    if (state.axis === "z") return { w: v.nx, h: v.ny, depth: v.nz };
    if (state.axis === "y") return { w: v.nx, h: v.nz, depth: v.ny };
    return { w: v.ny, h: v.nz, depth: v.nx }; // axis x
  }

  function drawOverlay() {
    const img = q('[data-role="viz-img"]');
    const cv = q('[data-role="viz-overlay"]');
    if (!state.vinfo || !img.naturalWidth) return;
    const dims = axisDims();
    const cw = img.clientWidth, ch = img.clientHeight;
    cv.width = cw; cv.height = ch;
    cv.style.width = cw + "px"; cv.style.height = ch + "px";
    const ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, cw, ch);
    if (!state.showPicks || !state.picks.length) return;
    const sx = cw / dims.w, sy = ch / dims.h;
    const r = state.diameter / 2;
    const idx = state.index;
    const palette = ["#39d353", "#ff6ac1", "#f5a623", "#4aa3ff", "#e5484d"];
    for (const pk of state.picks) {
      let center, colVal, rowVal;
      if (state.axis === "z") { center = pk.z; colVal = pk.x; rowVal = pk.y; }
      else if (state.axis === "y") { center = pk.y; colVal = pk.x; rowVal = pk.z; }
      else { center = pk.x; colVal = pk.y; rowVal = pk.z; }
      const d = Math.abs(idx - center);
      if (d > r) continue;
      const rr = Math.sqrt(Math.max(0, r * r - d * d));
      ctx.beginPath();
      ctx.arc(colVal * sx, rowVal * sy, Math.max(1, rr * sx), 0, 2 * Math.PI);
      ctx.strokeStyle = palette[(pk.class || 0) % palette.length];
      ctx.lineWidth = state.width;
      ctx.stroke();
    }
  }

  let sliceTimer = null;
  function renderSliceSoon() {
    // Coalesce rapid slider input (a drag fires ~60 events/sec) into one
    // request; the label/overlay still update immediately.
    if (sliceTimer) clearTimeout(sliceTimer);
    sliceTimer = setTimeout(() => { sliceTimer = null; renderSlice(); }, 60);
  }

  function renderSlice() {
    if (!state.mrc || !state.vinfo) return;
    const img = q('[data-role="viz-img"]');
    const params = new URLSearchParams({
      mrc_path: state.mrc, axis: state.axis, index: String(state.index),
    });
    if (state.lo != null) params.set("lo", String(state.lo));
    if (state.hi != null) params.set("hi", String(state.hi));
    img.onload = drawOverlay;
    img.src = `/api/viz/slice?${params.toString()}`;
    q('[data-role="viz-meta"]').textContent =
      `${state.tomo || ""}  ·  ${state.vinfo.nx}×${state.vinfo.ny}×${state.vinfo.nz}` +
      (state.vinfo.voxel_size ? `  ·  ${state.vinfo.voxel_size.toFixed(2)} Å/vox` : "") +
      `  ·  ${state.picks.length} picks`;
  }

  function setupSliceRange() {
    const dims = axisDims();
    const slider = q('[data-role="viz-slice"]');
    slider.max = String(dims.depth - 1);
    state.index = Math.floor(dims.depth / 2);
    slider.value = String(state.index);
    q('[data-role="viz-slice-label"]').textContent = `Slice ${state.index}/${dims.depth - 1}`;
  }

  async function loadVolume(mrcPath) {
    statusEl.textContent = "Loading volume…";
    try {
      // Fetch FIRST, commit after: assigning state.mrc up front meant a failed
      // load left state.mrc pointing at the new volume while state.vinfo still
      // described the old one, so the next slider nudge requested slices of the
      // new MRC using the old volume's index range.
      const info = await api(`/api/viz/volume-info?mrc_path=${encodeURIComponent(mrcPath)}`);
      state.mrc = mrcPath;
      state.vinfo = info;
      // contrast sliders map 0..100 -> the sampled intensity range
      state.lo = state.vinfo.contrast_lo;
      state.hi = state.vinfo.contrast_hi;
      const vmin = state.vinfo.sample_min, vmax = state.vinfo.sample_max, span = (vmax - vmin) || 1;
      q('[data-role="viz-lo"]').value = String(Math.round(((state.lo - vmin) / span) * 100));
      q('[data-role="viz-hi"]').value = String(Math.round(((state.hi - vmin) / span) * 100));
      q('[data-role="viz-controls"]').style.display = "";
      q('[data-role="viz-stage"]').style.display = "";
      setupSliceRange();
      renderSlice();
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
        body: JSON.stringify({ particles_path: state.particles, tomo_name: state.tomo || mrcPathForMatch, volume: state.vinfo }),
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
        renderSlice();
      };
    } else {
      sel.style.display = "none";
    }
    if (!tomos.length) { statusEl.textContent = "No tomogram volume found to display."; return; }
    const t = tomos[0];
    state.tomo = t.name;
    await loadVolume(t.mrc_path);
    await loadPicks(t.mrc_path);
    renderSlice();
  });

  // --- Controls ---
  q('[data-role="viz-slice"]').addEventListener("input", (e) => {
    state.index = parseInt(e.target.value, 10);
    const dims = axisDims();
    q('[data-role="viz-slice-label"]').textContent = `Slice ${state.index}/${dims.depth - 1}`;
    renderSliceSoon();
  });
  body.querySelectorAll(".viz-axis").forEach((btn) => {
    btn.addEventListener("click", () => {
      body.querySelectorAll(".viz-axis").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.axis = btn.dataset.axis;
      setupSliceRange();
      renderSlice();
    });
  });
  function contrastFromSliders() {
    const v = state.vinfo; if (!v) return;
    const vmin = v.sample_min, span = (v.sample_max - v.sample_min) || 1;
    let lo = vmin + (parseInt(q('[data-role="viz-lo"]').value, 10) / 100) * span;
    let hi = vmin + (parseInt(q('[data-role="viz-hi"]').value, 10) / 100) * span;
    if (hi <= lo) hi = lo + span * 0.01;
    state.lo = lo; state.hi = hi;
    renderSliceSoon();
  }
  q('[data-role="viz-lo"]').addEventListener("input", contrastFromSliders);
  q('[data-role="viz-hi"]').addEventListener("input", contrastFromSliders);
  q('[data-role="viz-diam"]').addEventListener("input", (e) => {
    state.diameter = parseInt(e.target.value, 10);
    q('[data-role="viz-diam-val"]').textContent = String(state.diameter);
    drawOverlay();
  });
  q('[data-role="viz-width"]').addEventListener("input", (e) => { state.width = parseInt(e.target.value, 10); drawOverlay(); });
  q('[data-role="viz-showpicks"]').addEventListener("change", (e) => { state.showPicks = e.target.checked; drawOverlay(); });

  new WinBox({
    title: "Tomogram Viewer",
    width: "760px", height: "820px",
    x: "center", y: "center",
    mount: body,
    class: ["viz-winbox"],
    onresize: () => drawOverlay(),
  });
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
      let listing;
      try {
        listing = await api("/api/project/browse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: path || "" }),
        });
      } catch (err) {
        listEl.innerHTML = `<div class="browser-entry picker-note">Could not open: ${escapeHtml(err.message)}</div>`;
        return;
      }
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
