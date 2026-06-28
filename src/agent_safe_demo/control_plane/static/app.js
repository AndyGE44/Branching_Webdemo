const appSelect = document.querySelector("#appSelect");
const appDescriptionEl = document.querySelector("#appDescription");
const runtimeUrlEl = document.querySelector("#runtimeUrl");
const runtimeFrame = document.querySelector("#runtimeFrame");
const resultPill = document.querySelector("#lastResult");
const workspaceStateEl = document.querySelector("#workspaceState");
const checkpointsEl = document.querySelector("#checkpoints");
const snapshotLabelInput = document.querySelector("#snapshotLabelInput");
const runAgentBtn = document.querySelector("#runAgentBtn");
const buildingOverlay = document.querySelector("#buildingOverlay");
const buildingTitle = document.querySelector("#buildingTitle");
const buildingHint = document.querySelector("#buildingHint");
const railHideBtn = document.querySelector("#railHide");
const railShowBtn = document.querySelector("#railShow");

let apps = [];
let currentAppId = null;
let workspace = null;
let buildingTimer = null;

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

function showResult(text, ok = true) {
  resultPill.textContent = text;
  resultPill.style.background = ok ? "#e7f1ff" : "#f8e7df";
  resultPill.style.color = ok ? "#173f7a" : "#8a341f";
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

function formatTime(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleString();
}

// The "Building…" sign shown over the website while the runtime boots (initial
// load, app switch, reset, restore). It is hidden when the iframe finishes
// loading the real runtime URL, with a timeout as a safety net.
function showBuilding(
  title = "Building runtime…",
  hint = "Starting the shop website. A cold start can take up to a minute.",
) {
  buildingTitle.textContent = title;
  buildingHint.textContent = hint;
  buildingOverlay.hidden = false;
  clearTimeout(buildingTimer);
  buildingTimer = setTimeout(() => {
    buildingOverlay.hidden = true;
  }, 90000);
}

function hideBuilding() {
  clearTimeout(buildingTimer);
  buildingOverlay.hidden = true;
}

runtimeFrame.addEventListener("load", () => {
  // Ignore the initial empty frame; only hide once a real src has loaded.
  if (runtimeFrame.getAttribute("src")) {
    hideBuilding();
  }
});

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
    runAgentBtn.textContent = app.agent_demo_label || "Run Agent";
  }
}

function renderWorkspaceState(branch) {
  workspaceStateEl.innerHTML = branch.dirty ? badge("unsaved") : badge("saved");
  workspaceStateEl.title = branch.dirty
    ? "The runtime has changes since the last snapshot."
    : "The runtime matches the current checkpoint.";
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

async function refreshApps() {
  const payload = await request("/api/apps");
  renderApps(payload);
  return payload;
}

async function refreshWorkspace() {
  const data = await request("/api/workspace");
  workspace = data;
  currentAppId = data.app.id;
  renderApps({ apps, current_app_id: currentAppId });
  renderWorkspaceState(data.branch);
  renderCheckpoints(data.branch);
  runtimeUrlEl.textContent = data.workspace.runtime_ui_url;
  const url = data.workspace.runtime_ui_url;
  if (runtimeFrame.getAttribute("src") !== url) {
    showBuilding();
    runtimeFrame.src = url; // the load handler hides the overlay
  } else {
    hideBuilding();
  }
  return data;
}

async function refresh() {
  if (!apps.length) {
    await refreshApps();
  }
  await refreshWorkspace();
}

async function mutate(label, fn) {
  try {
    await fn();
    showResult(label);
    await refresh();
  } catch (error) {
    hideBuilding();
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
  // The runtime is about to revert (e.g. the storefront cart), so reload the
  // embedded iframe — refreshWorkspace re-sets src once it is cleared.
  showBuilding("Restoring checkpoint…", "Reverting the shop runtime to the selected save point.");
  runtimeFrame.removeAttribute("src");
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
  showBuilding("Building runtime…", `Starting ${label}. A cold start can take up to a minute.`);
  runtimeFrame.removeAttribute("src");
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
  showBuilding("Building runtime…", "Resetting and rebuilding a clean shop runtime.");
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
    hideBuilding();
    showResult(error.message, false);
    await refreshWorkspace();
  }
});

// Collapse / expand the control rail to give the shop website the full screen.
railHideBtn.addEventListener("click", () => document.body.classList.add("rail-collapsed"));
railShowBtn.addEventListener("click", () => document.body.classList.remove("rail-collapsed"));

showBuilding();
refresh().catch((error) => {
  hideBuilding();
  showResult(error.message, false);
});
