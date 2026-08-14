// app.js — RELION Job Manager frontend. Vanilla JS, no build step.
// Each job opened from the sidebar becomes an independent WinBox popup
// (draggable/resizable/minimizable, cryoSPARC-style) mounted with a form
// built from the job definition the backend serves. See style.css for the
// popup's internal layout (standard fields on top, tabs, editable command
// box, live output at the bottom).

const openPopups = {}; // internal_name+instance -> winbox instance, for future multi-instance support
let jobCounter = 0;

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json();
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
      item.innerHTML = `<span class="job-name">${escapeHtml(job.display_name)}</span>
                         <span class="job-desc">${escapeHtml(job.description)}</span>`;
      item.addEventListener("click", () => openJobPopup(job.internal_name, job.display_name, job.is_custom));
      block.appendChild(item);
    }
    container.appendChild(block);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

document.getElementById("jobSearch").addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll(".job-item").forEach((item) => {
    item.style.display = !q || item.dataset.search.includes(q) ? "" : "none";
  });
  document.querySelectorAll(".category-block").forEach((block) => {
    const anyVisible = Array.from(block.querySelectorAll(".job-item")).some(
      (i) => i.style.display !== "none"
    );
    block.style.display = anyVisible ? "" : "none";
  });
});

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

async function openJobPopup(internalName, displayName, isCustom) {
  jobCounter += 1;
  const popupId = `${internalName}-${jobCounter}`;

  let def;
  try {
    def = await api(`/api/jobs/${internalName}`);
  } catch (err) {
    alert(`Could not load job definition for ${internalName}: ${err.message}`);
    return;
  }

  const optionsByKey = {};
  (def.options || []).forEach((o) => (optionsByKey[o.key] = o));

  const body = document.createElement("div");
  body.className = "job-popup";
  body.innerHTML = `
    <div class="job-desc-bar">${escapeHtml(def.description || "")}</div>
    <div class="job-standard-form" data-role="standard-form"></div>
    <div class="tab-bar" data-role="tab-bar">
      <button class="tab-btn active" data-tab="advanced">Advanced</button>
      <button class="tab-btn" data-tab="errors">Errors<span class="badge" data-role="error-badge" style="display:none">0</span></button>
      ${def.is_custom ? "" : '<button class="tab-btn" data-tab="source">RELION Source</button>'}
    </div>
    <div class="tab-content active" data-tab-content="advanced"></div>
    <div class="tab-content" data-tab-content="errors"><pre class="errors-pre" data-role="errors-pre">(no errors yet)</pre></div>
    ${def.is_custom ? "" : `<div class="tab-content" data-tab-content="source"><pre class="source-pre">${escapeHtml(def.commands_source || "(source unavailable)")}</pre></div>`}
    ${def.is_custom ? "" : `
    <div class="command-row">
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
    <div class="command-row">
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
    const val = (def.default_values || {})[key];
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
      const val = (def.default_values || {})[opt.key];
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
        const val = (def.default_values || {})[key];
        grid.appendChild(buildFieldRow(key, opt, val));
      }
      advancedContent.appendChild(grid);
    }
  }

  // Tab switching
  body.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      body.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      body.querySelectorAll(".tab-content").forEach((c) => c.classList.remove("active"));
      btn.classList.add("active");
      body.querySelector(`[data-tab-content="${btn.dataset.tab}"]`).classList.add("active");
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
    commandBox.value = def.draft_command || "";
    const recomputeBtn = body.querySelector('[data-role="recompute-btn"]');
    recomputeBtn.addEventListener("click", async () => {
      try {
        const resp = await api(`/api/jobs/${internalName}/draft`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ field_values: collectValues() }),
        });
        commandBox.value = resp.draft_command;
        if (resp.unmapped_fields && resp.unmapped_fields.length) {
          commandBox.title = "Not auto-mapped to a flag (add manually if needed): " +
            resp.unmapped_fields.join(", ");
        }
      } catch (err) {
        alert("Could not recompute draft: " + err.message);
      }
    });
  }

  const liveOutput = body.querySelector('[data-role="live-output"]');
  const statusLine = body.querySelector('[data-role="status-line"]');
  const errorsPre = body.querySelector('[data-role="errors-pre"]');
  const errorBadge = body.querySelector('[data-role="error-badge"]');
  let errorLines = [];

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

  function connectWebSocket(runId) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/runs/${runId}`);
    ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data);
      if (msg.type === "stdout") appendOutputLine(msg.line, false);
      else if (msg.type === "stderr") appendOutputLine(msg.line, true);
      else if (msg.type === "status") {
        statusLine.textContent = `Status: ${msg.status}` +
          (msg.exit_code !== undefined && msg.exit_code !== null ? ` (exit ${msg.exit_code})` : "");
        statusLine.className = "status-line " + (msg.status === "completed" ? "ok" : msg.status === "failed" ? "failed" : "");
      } else if (msg.type === "error") {
        appendOutputLine(msg.line, true);
      }
    };
    ws.onerror = () => appendOutputLine("[websocket error]", true);
  }

  const runBtn = body.querySelector('[data-role="run-btn"]');
  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true;
    statusLine.textContent = "Starting…";
    statusLine.className = "status-line";
    try {
      const payload = { internal_name: internalName };
      if (def.is_custom) {
        payload.field_values = collectValues();
      } else {
        payload.command = commandBox.value;
      }
      const run = await api("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      connectWebSocket(run.run_id);
      refreshRunningJobsBar();
    } catch (err) {
      appendOutputLine("Failed to start run: " + err.message, true);
    } finally {
      runBtn.disabled = false;
    }
  });

  const win = new WinBox({
    title: displayName,
    width: "620px",
    height: "700px",
    x: "center",
    y: "center",
    mount: body,
    class: ["no-full"],
  });
  openPopups[popupId] = win;
}

loadCatalog().catch((err) => {
  document.getElementById("emptyState").innerHTML =
    `<p style="color:#e2584d">Failed to load job catalog: ${escapeHtml(err.message)}</p>`;
});

// --- Project switching + job history ----------------------------------

const projectDirLabel = document.getElementById("projectDirLabel");
const changeProjectBtn = document.getElementById("changeProjectBtn");
const projectModalOverlay = document.getElementById("projectModalOverlay");
const projectPathInput = document.getElementById("projectPathInput");
const projectBrowser = document.getElementById("projectBrowser");
const notAProjectOverlay = document.getElementById("notAProjectOverlay");
const runningJobsBar = document.getElementById("runningJobsBar");

let pendingProjectPath = null; // path awaiting a "start new / pick different" decision

async function refreshProjectLabel() {
  try {
    const proj = await api("/api/project");
    projectDirLabel.textContent = proj.path;
    projectDirLabel.title = proj.path;
  } catch (err) {
    projectDirLabel.textContent = "(unknown project)";
  }
}

async function refreshRunningJobsBar() {
  let history = [];
  try {
    const proj = await api("/api/project");
    history = proj.history || [];
  } catch (err) {
    runningJobsBar.innerHTML = "";
    return;
  }
  runningJobsBar.innerHTML = "";
  if (!history.length) return;

  const title = document.createElement("div");
  title.className = "running-jobs-title";
  title.textContent = "Job history for this project";
  runningJobsBar.appendChild(title);

  const list = document.createElement("div");
  list.className = "running-jobs-list";
  history.slice().reverse().forEach((run) => {
    const chip = document.createElement("div");
    chip.className = "run-chip status-" + run.status;
    chip.title = run.command || "";
    chip.innerHTML = `<span class="run-chip-name">${escapeHtml(run.display_name)}</span>
                       <span class="run-chip-status">${escapeHtml(run.status)}</span>`;
    chip.addEventListener("click", () => openRunHistoryPopup(run));
    list.appendChild(chip);
  });
  runningJobsBar.appendChild(list);
}

function appendPlainLine(container, text, isStderr) {
  const line = document.createElement("div");
  line.className = "output-line" + (isStderr ? " stderr" : "");
  line.textContent = text;
  container.appendChild(line);
  container.scrollTop = container.scrollHeight;
}

function openRunHistoryPopup(runSummary) {
  const body = document.createElement("div");
  body.className = "job-popup";
  body.innerHTML = `
    <div class="job-desc-bar">${escapeHtml(runSummary.command || "(custom job — see live output for the summary)")}</div>
    <div class="command-row">
      <span class="status-line" data-role="status-line">Status: ${escapeHtml(runSummary.status)}</span>
    </div>
    <div class="live-output" data-role="live-output"></div>
  `;
  const liveOutput = body.querySelector('[data-role="live-output"]');
  const statusLine = body.querySelector('[data-role="status-line"]');

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/runs/${runSummary.run_id}`);
  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === "stdout") appendPlainLine(liveOutput, msg.line, false);
    else if (msg.type === "stderr") appendPlainLine(liveOutput, msg.line, true);
    else if (msg.type === "status") {
      statusLine.textContent = `Status: ${msg.status}` +
        (msg.exit_code !== undefined && msg.exit_code !== null ? ` (exit ${msg.exit_code})` : "");
    } else if (msg.type === "error") {
      statusLine.textContent =
        `Status: ${runSummary.status} (live transcript unavailable — the backend was restarted since this run)`;
    }
  };
  ws.onerror = () => appendPlainLine(liveOutput, "[websocket error]", true);

  new WinBox({
    title: runSummary.display_name,
    width: "560px",
    height: "500px",
    x: "center",
    y: "center",
    mount: body,
    class: ["no-full"],
    onclose: () => { try { ws.close(); } catch (e) { /* noop */ } return false; },
  });
}

async function browseTo(path) {
  try {
    const listing = await api("/api/project/browse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    projectPathInput.value = listing.path;
    renderBrowser(listing);
  } catch (err) {
    projectBrowser.innerHTML = `<p class="modal-error">${escapeHtml(err.message)}</p>`;
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
  await refreshRunningJobsBar();
}

changeProjectBtn.addEventListener("click", openProjectModal);
document.getElementById("projectModalCancelBtn").addEventListener("click", closeProjectModal);
document.getElementById("projectPathGoBtn").addEventListener("click", () => browseTo(projectPathInput.value.trim()));
projectPathInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") browseTo(projectPathInput.value.trim());
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
      notAProjectOverlay.classList.remove("hidden");
      return;
    }
    closeProjectModal();
    await onProjectChanged();
  } catch (err) {
    alert("Could not switch project: " + err.message);
  }
});

document.getElementById("startNewProjectBtn").addEventListener("click", async () => {
  if (!pendingProjectPath) return;
  try {
    await api("/api/project/init", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: pendingProjectPath }),
    });
    notAProjectOverlay.classList.add("hidden");
    pendingProjectPath = null;
    await onProjectChanged();
  } catch (err) {
    alert("Could not start new project: " + err.message);
  }
});

document.getElementById("pickDifferentFolderBtn").addEventListener("click", () => {
  notAProjectOverlay.classList.add("hidden");
  pendingProjectPath = null;
  openProjectModal();
});

refreshProjectLabel();
refreshRunningJobsBar();
