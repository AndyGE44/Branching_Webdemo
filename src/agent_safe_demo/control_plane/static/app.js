const appSelect = document.querySelector("#appSelect");
const appDescriptionEl = document.querySelector("#appDescription");
const runtimeTitleEl = document.querySelector("#runtimeTitle");
const runtimeUrlEl = document.querySelector("#runtimeUrl");
const runtimeFrame = document.querySelector("#runtimeFrame");
const stateView = document.querySelector("#stateView");
const resultPill = document.querySelector("#lastResult");
const backendModeEl = document.querySelector("#backendMode");
const backendStatsEl = document.querySelector("#backendStats");
const snapshotModeEl = document.querySelector("#snapshotMode");
const workspaceStateEl = document.querySelector("#workspaceState");
const checkpointsEl = document.querySelector("#checkpoints");
const commitHeadEl = document.querySelector("#commitHead");
const commitHistoryEl = document.querySelector("#commitHistory");
const snapshotLabelInput = document.querySelector("#snapshotLabelInput");
const commitBtn = document.querySelector("#commitBtn");
const runAgentBtn = document.querySelector("#runAgentBtn");

let apps = [];
let currentAppId = null;
let workspace = null;
let runtimeBaseUrl = null;
let runtimeStatePath = "/api/state";

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || `Request failed: ${response.status}`);
  }
  return data;
}

function runtimeRequest(path, options = {}) {
  if (!runtimeBaseUrl) {
    throw new Error("Runtime is not ready yet");
  }
  return request(`${runtimeBaseUrl}${path}`, options);
}

function showResult(text, ok = true) {
  resultPill.textContent = text;
  resultPill.style.background = ok ? "#e8f1ec" : "#f8e7df";
  resultPill.style.color = ok ? "#174938" : "#8a341f";
}

function badge(value) {
  const safeValue = escapeHtml(value);
  const normalized = String(value ?? "").toLowerCase();
  let tone = "neutral";
  if (["ready", "active", "draft", "saved", "current", "running"].includes(normalized)) {
    tone = "ok";
  }
  if (["blocked", "failed", "dirty", "unsaved", "exited"].includes(normalized)) {
    tone = "warn";
  }
  return `<span class="badge ${tone}">${safeValue}</span>`;
}

function formatMs(value) {
  const number = Number(value || 0);
  if (number >= 1000) {
    return `${(number / 1000).toFixed(2)}s`;
  }
  return `${number.toFixed(number >= 10 ? 0 : 1)}ms`;
}

function formatTime(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleString();
}

function statCard(label, value, hint = "") {
  return `
    <article class="status-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      ${hint ? `<p>${escapeHtml(hint)}</p>` : ""}
    </article>
  `;
}

function activeApp() {
  return apps.find((app) => app.id === currentAppId) || workspace?.app || null;
}

function renderApps(payload) {
  apps = payload.apps || [];
  currentAppId = payload.current_app_id;
  appSelect.innerHTML = apps
    .map(
      (app) => `<option value="${escapeHtml(app.id)}" ${app.id === currentAppId ? "selected" : ""}>${escapeHtml(app.label)}</option>`,
    )
    .join("");
  const app = activeApp();
  if (app) {
    appDescriptionEl.textContent = app.description;
    runtimeTitleEl.textContent = `${app.label} Runtime`;
    runAgentBtn.textContent = app.agent_demo_label || "Run Agent";
  }
}

function renderBackendStatus(status, branch) {
  const snapshotOps = status.operations?.snapshot || {};
  const restoreOps = status.operations?.restore || {};
  const totals = status.totals || {};
  const details = status.details || {};
  const stateforkMode =
    details.statefork_runtime_mode === "docker-build"
      ? "Docker build"
      : details.statefork_runtime_mode === "init"
        ? "Init overlay"
        : "";
  backendModeEl.textContent = stateforkMode
    ? `${status.backend} / ${status.method} / ${stateforkMode}`
    : `${status.backend} / ${status.method}`;
  snapshotModeEl.textContent = `${branch.id} at ${branch.url}`;
  const cards = [
    statCard("App", details.app_label || workspace?.app?.label || "app", details.app_id || ""),
    statCard("Backend", status.backend, status.method),
  ];
  if (stateforkMode) {
    cards.push(
      statCard(
        "StateFork Mode",
        stateforkMode,
        details.statefork_build ? "Dockerfile enabled" : "Dockerfile disabled",
      ),
    );
  }
  const stateFiles = details.state_files || workspace?.workspace?.state_files || [];
  if (stateFiles.length) {
    cards.push(
      statCard(
        "State Files",
        stateFiles.map((file) => file.name || file.path).join(", "),
        stateFiles
          .map((file) => (file.exists ? `${(file.sha256 || "").slice(0, 8)} ${file.size_bytes || 0}B` : "missing"))
          .join(" · "),
      ),
    );
  }
  cards.push(
    statCard("Runtime", branch.status, branch.id),
    statCard("Checkpoints", branch.snapshots?.length ?? 0, "manual save points"),
    statCard("Saved State", branch.current_snapshot_id || "none", branch.dirty ? "unsaved changes" : "clean"),
    statCard("Snapshot Calls", snapshotOps.count ?? 0, `avg ${formatMs(snapshotOps.mean_ms)}`),
    statCard("Restore Calls", restoreOps.count ?? 0, `avg ${formatMs(restoreOps.mean_ms)}`),
    statCard("Bases", totals.bases ?? 0, "controller internals"),
    statCard("Branches", totals.branches ?? 0, "runtime processes"),
  );
  backendStatsEl.innerHTML = cards.join("");
}

function renderWorkspaceState(branch) {
  workspaceStateEl.innerHTML = branch.dirty ? badge("unsaved") : badge("saved");
  workspaceStateEl.title = branch.dirty
    ? "The runtime has changes since the last snapshot."
    : "The runtime matches the current checkpoint.";
}

function commitDiffSummary(commit) {
  const tables = commit.diff?.tables || [];
  return tables.length ? `Changed: ${tables.join(", ")}` : "No table changes";
}

function renderCommits(data) {
  const head = data.app_head;
  const commits = data.commits || [];
  if (!head) {
    commitHeadEl.innerHTML = `<p class="empty">No commits yet.</p>`;
  } else {
    commitHeadEl.innerHTML = `
      <article class="commit-card">
        <div class="checkpoint-title">
          <strong>${escapeHtml(head.label)}</strong>
          ${head.active ? badge("active") : badge("inactive")}
        </div>
        <p>${escapeHtml(head.id)} · ${escapeHtml(head.checkpoint_id)}</p>
        <p>${escapeHtml(formatTime(head.created_at))} · ${escapeHtml(head.author)}</p>
        ${head.message ? `<p>${escapeHtml(head.message)}</p>` : ""}
      </article>
    `;
  }

  if (!commits.length) {
    commitHistoryEl.innerHTML = "";
    return;
  }
  commitHistoryEl.innerHTML = `
    <ol>
      ${commits
        .map(
          (commit) => `
            <li>
              <strong>${escapeHtml(commit.label)}</strong>
              <p>${escapeHtml(commit.id)} · ${escapeHtml(formatTime(commit.created_at))}</p>
              <p>${escapeHtml(commitDiffSummary(commit))}</p>
            </li>
          `,
        )
        .join("")}
    </ol>
  `;
}

function renderCheckpoints(branch) {
  const snapshots = branch.snapshots || [];
  if (!snapshots.length) {
    checkpointsEl.innerHTML = `<p class="empty">No checkpoints yet.</p>`;
    return;
  }

  checkpointsEl.innerHTML = `
    <section class="snapshot-tree checkpoint-tree">
      <div class="snapshot-root">
        <span></span>
        <div>
          <strong>Workspace runtime</strong>
          <p>${escapeHtml(branch.id)} · ${escapeHtml(branch.url)}</p>
        </div>
      </div>
      <ol>
        ${snapshots
          .map(
            (snapshot, index) => `
              <li>
                <span class="snapshot-index">${index + 1}</span>
                <div>
                  <div class="checkpoint-title">
                    <strong>${escapeHtml(snapshot.label)}</strong>
                    ${snapshot.id === branch.current_snapshot_id ? badge("current") : ""}
                  </div>
                  <p>${escapeHtml(snapshot.backend)} · ${escapeHtml(snapshot.id)}</p>
                  <p>${escapeHtml(formatTime(snapshot.created_at))}</p>
                  <button data-action="restore-snapshot" data-snapshot-id="${escapeHtml(snapshot.id)}" type="button">Restore</button>
                </div>
              </li>
            `,
          )
          .join("")}
      </ol>
    </section>
  `;
}

function scalarEntries(state) {
  const entries = [];
  for (const [key, value] of Object.entries(state || {})) {
    if (Array.isArray(value)) {
      entries.push([key, value.length]);
    } else if (value && typeof value === "object") {
      entries.push([key, Object.keys(value).length]);
    } else {
      entries.push([key, value]);
    }
  }
  return entries.slice(0, 4);
}

function valuePreview(value) {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return value;
}

function renderRows(items, columns) {
  if (!items.length) {
    return `<tr><td colspan="${columns.length}" class="empty">No records yet</td></tr>`;
  }
  return items
    .slice(0, 25)
    .map(
      (item) => `
        <tr>
          ${columns.map((column) => `<td>${escapeHtml(valuePreview(item[column]))}</td>`).join("")}
        </tr>
      `,
    )
    .join("");
}

function renderTable(title, items) {
  const columns = Array.from(
    items.reduce((set, item) => {
      Object.keys(item || {}).forEach((key) => set.add(key));
      return set;
    }, new Set()),
  ).slice(0, 6);
  return `
    <section class="state-card">
      <div class="state-card-header">
        <h3>${escapeHtml(title)}</h3>
        <span>${items.length}</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead>
          <tbody>${renderRows(items, columns)}</tbody>
        </table>
      </div>
    </section>
  `;
}

function renderState(state) {
  const summary = state.summary && typeof state.summary === "object" ? Object.entries(state.summary) : scalarEntries(state);
  const tables = Object.entries(state || {}).filter(([, value]) => Array.isArray(value) && value.every((item) => item && typeof item === "object"));
  stateView.innerHTML = `
    <section class="state-summary">
      ${summary
        .slice(0, 4)
        .map(
          ([key, value]) => `
            <div>
              <strong>${escapeHtml(valuePreview(value))}</strong>
              <span>${escapeHtml(key)}</span>
            </div>
          `,
        )
        .join("")}
    </section>
    <div class="state-grid">
      ${tables.slice(0, 4).map(([key, value]) => renderTable(key, value)).join("")}
    </div>
    <section class="state-card">
      <div class="state-card-header"><h3>Raw State</h3><span>JSON</span></div>
      <pre>${escapeHtml(JSON.stringify(state, null, 2))}</pre>
    </section>
  `;
}

async function refreshApps() {
  const payload = await request("/api/apps");
  renderApps(payload);
  return payload;
}

async function refreshWorkspace() {
  const data = await request("/api/workspace");
  workspace = data;
  currentAppId = data.app.id;
  runtimeBaseUrl = data.workspace.runtime_proxy_url || data.workspace.runtime_url;
  runtimeStatePath = data.workspace.state_path || "/api/state";
  renderApps({ apps, current_app_id: currentAppId });
  renderWorkspaceState(data.branch);
  renderBackendStatus(data.backend, data.branch);
  renderCheckpoints(data.branch);
  renderCommits(data);
  runtimeUrlEl.textContent = data.workspace.runtime_ui_url;
  if (runtimeFrame.src !== data.workspace.runtime_ui_url) {
    runtimeFrame.src = data.workspace.runtime_ui_url;
  }
  return data;
}

async function refreshState() {
  const state = await runtimeRequest(runtimeStatePath);
  renderState(state);
  return state;
}

async function refresh() {
  if (!apps.length) {
    await refreshApps();
  }
  await refreshWorkspace();
  try {
    await refreshState();
  } catch (error) {
    stateView.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

async function mutate(label, fn) {
  try {
    await fn();
    showResult(label);
    await refresh();
  } catch (error) {
    showResult(error.message, false);
    try {
      await refreshWorkspace();
    } catch {
      // Keep the original error visible if refresh also fails.
    }
  }
}

async function saveWorkspaceSnapshot(label) {
  return request("/api/workspace/snapshots", {
    method: "POST",
    body: JSON.stringify({ label: label || null }),
  });
}

async function commitWorkspace() {
  const dirty = await request("/api/workspace/dirty");
  if (!dirty.dirty && !window.confirm("Commit the current clean workspace head?")) {
    showResult("Commit canceled");
    return;
  }
  const app = activeApp();
  const defaultLabel = `${app?.label || "App"} app head promotion`;
  const label = window.prompt("Commit label", defaultLabel);
  if (label === null) {
    showResult("Commit canceled");
    return;
  }
  const message = window.prompt("Commit message", "");
  if (message === null) {
    showResult("Commit canceled");
    return;
  }
  runtimeFrame.removeAttribute("src");
  stateView.innerHTML = `<p class="empty">Committing workspace...</p>`;
  showResult("Committing...");
  const data = await request("/api/workspace/commit", {
    method: "POST",
    body: JSON.stringify({ label: label.trim() || null, message: message.trim(), author: "user" }),
  });
  showResult(`Committed ${data.commit.id}`);
  await refresh();
}

async function restoreSnapshot(snapshotId) {
  const dirty = await request("/api/workspace/dirty");
  let force = false;
  if (dirty.dirty) {
    const saveFirst = window.confirm("This workspace has unsaved changes. Save a snapshot before restoring?");
    if (saveFirst) {
      const label = window.prompt("Snapshot label", "autosave before restore");
      if (label === null) {
        showResult("Restore canceled");
        return;
      }
      await saveWorkspaceSnapshot(label.trim() || "autosave before restore");
    } else {
      const discard = window.confirm("Discard unsaved changes and restore the selected checkpoint?");
      if (!discard) {
        showResult("Restore canceled");
        return;
      }
      force = true;
    }
  }
  await request("/api/workspace/restore", {
    method: "POST",
    body: JSON.stringify({ snapshot_id: snapshotId, force }),
  });
  showResult("Checkpoint restored");
  await refresh();
}

appSelect.addEventListener("change", async () => {
  const app = apps.find((candidate) => candidate.id === appSelect.value);
  const label = app?.label || appSelect.value;
  if (!window.confirm(`Switch to ${label} and reset the current workspace?`)) {
    appSelect.value = currentAppId;
    return;
  }
  runtimeFrame.removeAttribute("src");
  stateView.innerHTML = `<p class="empty">Switching app...</p>`;
  await mutate(`Switched to ${label}`, async () => {
    await request(`/api/apps/${encodeURIComponent(appSelect.value)}/select`, { method: "POST" });
    await refreshApps();
  });
});

document.querySelector("#refreshBtn").addEventListener("click", refresh);

document.querySelector("#snapshotBtn").addEventListener("click", async () => {
  const label = snapshotLabelInput.value.trim();
  await mutate("Snapshot saved", async () => {
    await saveWorkspaceSnapshot(label);
    snapshotLabelInput.value = "";
  });
});

commitBtn.addEventListener("click", async () => {
  try {
    await commitWorkspace();
  } catch (error) {
    showResult(error.message, false);
    await refreshWorkspace();
  }
});

runAgentBtn.addEventListener("click", async () => {
  try {
    showResult("Running agent...");
    const data = await request("/api/workspace/run-agent", { method: "POST" });
    showResult(`Agent ran ${data.actions.length} actions`);
    await refresh();
  } catch (error) {
    showResult(error.message, false);
    await refreshWorkspace();
  }
});

document.querySelector("#resetBtn").addEventListener("click", async () => {
  if (!window.confirm("Reset the workspace and discard runtime checkpoints?")) {
    return;
  }
  runtimeFrame.removeAttribute("src");
  await mutate("Workspace reset", () => request("/api/workspace/reset", { method: "POST" }));
});

checkpointsEl.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action='restore-snapshot']");
  if (!button) {
    return;
  }
  try {
    await restoreSnapshot(button.dataset.snapshotId);
  } catch (error) {
    showResult(error.message, false);
    await refreshWorkspace();
  }
});

refresh().catch((error) => showResult(error.message, false));
