const inventoryEl = document.querySelector("#inventory");
const stateView = document.querySelector("#stateView");
const resultPill = document.querySelector("#lastResult");
const partSelect = document.querySelector("#partSelect");
const quantityInput = document.querySelector("#quantityInput");
const basesEl = document.querySelector("#bases");
const baseLabelInput = document.querySelector("#baseLabelInput");
const branchesEl = document.querySelector("#branches");
const backendModeEl = document.querySelector("#backendMode");
const backendStatsEl = document.querySelector("#backendStats");
const snapshotModeEl = document.querySelector("#snapshotMode");
const branchDiffs = {};
let selectedBaseId = null;
let baseCache = [];

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
  resultPill.style.background = ok ? "#e8f1ec" : "#f8e7df";
  resultPill.style.color = ok ? "#174938" : "#8a341f";
}

function formatMs(value) {
  const number = Number(value || 0);
  if (number >= 1000) {
    return `${(number / 1000).toFixed(2)}s`;
  }
  return `${number.toFixed(number >= 10 ? 0 : 1)}ms`;
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

function renderBackendStatus(status) {
  const snapshotOps = status.operations?.snapshot || {};
  const restoreOps = status.operations?.restore || {};
  const totals = status.totals || {};
  backendModeEl.textContent = `${status.backend} / ${status.method}`;
  snapshotModeEl.textContent = `${status.host}:${status.port_start}+`;
  backendStatsEl.innerHTML = [
    statCard("Backend", status.backend, status.method),
    statCard("Bases", totals.bases ?? 0, "frozen starting points"),
    statCard("Branches", totals.branches ?? 0, "active branch count"),
    statCard("Tree snapshots", totals.snapshots ?? 0, "visible branch nodes"),
    statCard("Snapshot calls", snapshotOps.count ?? 0, `avg ${formatMs(snapshotOps.mean_ms)}`),
    statCard("Restore/fork calls", restoreOps.count ?? 0, `avg ${formatMs(restoreOps.mean_ms)}`),
  ].join("");
}

function renderInventory(items) {
  inventoryEl.innerHTML = items
    .map((item) => {
      const low = item.available <= item.reorder_point ? "low" : "";
      return `
        <article class="item">
          <div>
            <h3>${item.id} · ${item.name}</h3>
            <p>${item.location}</p>
          </div>
          <div class="metrics">
            <div class="metric">
              <strong>${item.on_hand}</strong>
              <span>on hand</span>
            </div>
            <div class="metric ${low}">
              <strong>${item.available}</strong>
              <span>available</span>
            </div>
            <div class="metric">
              <strong>${item.reserved}</strong>
              <span>reserved</span>
            </div>
          </div>
        </article>
      `;
    })
    .join("");

  const currentPart = partSelect.value;
  const options = items
    .map((item) => `<option value="${item.id}">${item.id}</option>`)
    .join("");
  partSelect.innerHTML = options;
  if (currentPart) {
    partSelect.value = currentPart;
  }
}

function badge(value) {
  const safeValue = escapeHtml(value);
  const normalized = String(value ?? "").toLowerCase();
  let tone = "neutral";
  if (["ready", "active", "draft"].includes(normalized)) {
    tone = "ok";
  }
  if (["blocked", "failed"].includes(normalized)) {
    tone = "warn";
  }
  return `<span class="badge ${tone}">${safeValue}</span>`;
}

function selectedInventoryPayload() {
  return {
    part_id: partSelect.value,
    quantity: Number(quantityInput.value),
    actor: "user",
  };
}

function emptyRow(columns, label = "No records yet") {
  return `<tr><td colspan="${columns}" class="empty">${label}</td></tr>`;
}

function renderRows(items, columns) {
  if (!items.length) {
    return emptyRow(columns.length);
  }
  return items
    .map(
      (item) => `
        <tr>
          ${columns
            .map((column) => {
              const value =
                typeof column.value === "function"
                  ? column.value(item)
                  : item[column.value];
              return `<td>${column.html ? value : escapeHtml(value)}</td>`;
            })
            .join("")}
        </tr>
      `,
    )
    .join("");
}

function renderTable(title, items, columns) {
  return `
    <section class="state-card">
      <div class="state-card-header">
        <h3>${escapeHtml(title)}</h3>
        <span>${items.length}</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>${columns.map((column) => `<th>${escapeHtml(column.label)}</th>`).join("")}</tr>
          </thead>
          <tbody>${renderRows(items, columns)}</tbody>
        </table>
      </div>
    </section>
  `;
}

function renderState(state) {
  const totals = state.inventory.reduce(
    (summary, item) => {
      summary.onHand += item.on_hand;
      summary.available += item.available;
      summary.reserved += item.reserved;
      if (item.available <= 0) {
        summary.stockouts += 1;
      }
      return summary;
    },
    { onHand: 0, available: 0, reserved: 0, stockouts: 0 },
  );

  stateView.innerHTML = `
    <section class="state-summary">
      <div>
        <strong>${totals.onHand}</strong>
        <span>On hand</span>
      </div>
      <div>
        <strong>${totals.available}</strong>
        <span>Available</span>
      </div>
      <div>
        <strong>${totals.reserved}</strong>
        <span>Reserved</span>
      </div>
      <div>
        <strong>${totals.stockouts}</strong>
        <span>Stockouts</span>
      </div>
    </section>

    <div class="state-grid">
      ${renderTable("Inventory", state.inventory, [
        { label: "Part", value: "id" },
        { label: "Name", value: "name" },
        { label: "On Hand", value: "on_hand" },
        { label: "Available", value: "available" },
        { label: "Reserved", value: "reserved" },
      ])}

      ${renderTable("Reservations", state.reservations, [
        { label: "ID", value: "id" },
        { label: "Part", value: "part_id" },
        { label: "Qty", value: "quantity" },
        { label: "Status", value: (row) => badge(row.status), html: true },
        { label: "Actor", value: "actor" },
      ])}
    </div>

    <section class="state-card audit-card">
      <div class="state-card-header">
        <h3>Audit Log</h3>
        <span>latest ${state.audit_log.length}</span>
      </div>
      <div class="audit-list">
        ${
          state.audit_log.length
            ? state.audit_log
                .map(
                  (event) => `
                    <article class="audit-event">
                      <div>
                        ${badge(event.action)}
                        <strong>${escapeHtml(event.actor)}</strong>
                      </div>
                      <p>${escapeHtml(event.detail)}</p>
                      <time>${escapeHtml(event.created_at)}</time>
                    </article>
                  `,
                )
                .join("")
            : `<p class="empty">No audit events yet</p>`
        }
      </div>
    </section>
  `;
}

function renderDiff(diff) {
  if (!diff) {
    return `<p class="empty">Run the agent to create a planned branch diff.</p>`;
  }
  const hasCountChanges = Object.values(diff.counts).some((count) => count.delta !== 0);
  const hasInventoryChanges = diff.inventory.length > 0;
  const summary = hasCountChanges || hasInventoryChanges
    ? `Branch has changes relative to main.`
    : `No branch changes relative to main.`;
  const countRows = Object.entries(diff.counts)
    .map(
      ([name, count]) => `
        <tr>
          <td>${escapeHtml(name)}</td>
          <td>${count.main}</td>
          <td>${count.branch}</td>
          <td>${count.delta > 0 ? "+" : ""}${count.delta}</td>
        </tr>
      `,
    )
    .join("");
  const inventoryRows = diff.inventory.length
    ? diff.inventory
        .map(
          (item) => `
            <tr>
              <td>${escapeHtml(item.part_id)}</td>
              <td>${item.on_hand_delta > 0 ? "+" : ""}${item.on_hand_delta}</td>
              <td>${item.available_delta > 0 ? "+" : ""}${item.available_delta}</td>
              <td>${item.reserved_delta > 0 ? "+" : ""}${item.reserved_delta}</td>
            </tr>
          `,
        )
        .join("")
    : emptyRow(4, "No inventory quantity changes");

  return `
    <p class="diff-summary">${summary}</p>
    <div class="branch-diff">
      <table>
        <thead>
          <tr><th>Table</th><th>Main</th><th>Branch</th><th>Delta</th></tr>
        </thead>
        <tbody>${countRows}</tbody>
      </table>
      <table>
        <thead>
          <tr><th>Part</th><th>On Hand Δ</th><th>Available Δ</th><th>Reserved Δ</th></tr>
        </thead>
        <tbody>${inventoryRows}</tbody>
      </table>
    </div>
  `;
}

function renderSnapshotTree(branch) {
  const snapshots = branch.snapshots || [];
  const baseLabel = branch.base_checkpoint_id || branch.base_id || "base";
  const snapshotItems = snapshots.length
    ? snapshots
        .map(
          (snapshot, index) => `
            <li>
              <span class="snapshot-index">${index + 1}</span>
              <div>
                <strong>${escapeHtml(snapshot.label)}</strong>
                <p>${escapeHtml(snapshot.backend)} · ${escapeHtml(snapshot.id)}</p>
                <p>parent ${escapeHtml(snapshot.parent_id || baseLabel)}</p>
              </div>
            </li>
          `,
        )
        .join("")
    : `<li class="snapshot-empty"><span></span><p>No branch actions yet.</p></li>`;

  return `
    <section class="snapshot-tree">
      <div class="snapshot-root">
        <span></span>
        <div>
          <strong>Base checkpoint</strong>
          <p>${escapeHtml(baseLabel)}</p>
        </div>
      </div>
      <ol>${snapshotItems}</ol>
    </section>
  `;
}

function formatTime(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleString();
}

function baseTitle(baseId) {
  const base = baseCache.find((candidate) => candidate.id === baseId);
  return base ? `${base.label} · ${base.id}` : baseId || "Ad hoc base";
}

function renderBases(bases) {
  if (bases.length && !selectedBaseId) {
    selectedBaseId = bases[0].id;
  }
  if (selectedBaseId && !bases.some((base) => base.id === selectedBaseId)) {
    selectedBaseId = bases[0]?.id || null;
  }

  basesEl.innerHTML = bases.length
    ? bases
        .map((base) => {
          const selected = base.id === selectedBaseId ? "selected" : "";
          const sessionDetails = base.session_id
            ? `<p>session ${escapeHtml(base.session_id)}</p>`
            : `<p>${escapeHtml(base.backend)}</p>`;
          return `
            <article class="base-card ${selected}" data-base-id="${escapeHtml(base.id)}">
              <div class="base-card-main">
                <div>
                  <h3>${escapeHtml(base.label)}</h3>
                  <p>${escapeHtml(base.id)} · checkpoint ${escapeHtml(base.checkpoint_id)}</p>
                  ${sessionDetails}
                  <time>${escapeHtml(formatTime(base.created_at))}</time>
                </div>
                ${badge(base.status)}
              </div>
              <div class="branch-actions">
                <button data-action="select-base" data-id="${escapeHtml(base.id)}" type="button">Select</button>
                <button data-action="create-branch" data-id="${escapeHtml(base.id)}" class="primary" type="button">Create Branch</button>
                <button data-action="delete-base" data-id="${escapeHtml(base.id)}" class="danger" type="button">Delete</button>
              </div>
            </article>
          `;
        })
        .join("")
    : `<p class="empty">No base checkpoints yet.</p>`;
}

function renderBranchCard(branch, diffs = {}) {
  const checkpointDetails = branch.base_checkpoint_id
    ? `<p>from ${escapeHtml(branch.base_id)} · checkpoint ${escapeHtml(branch.base_checkpoint_id)}</p>`
    : `<p>${escapeHtml(branch.backend)} · port ${escapeHtml(branch.port)}</p>`;
  return `
    <article class="branch-card" data-branch-id="${escapeHtml(branch.id)}">
      <div class="branch-card-main">
        <div>
          <h3>${escapeHtml(branch.id)}</h3>
          ${checkpointDetails}
          <p>${escapeHtml(branch.backend)} · port ${escapeHtml(branch.port)}</p>
        </div>
        ${badge(branch.status)}
      </div>
      <div class="branch-actions">
        <a class="icon-link" href="${escapeHtml(branch.url)}" target="_blank" rel="noreferrer">Open Branch</a>
        <button data-action="run-agent" data-id="${escapeHtml(branch.id)}" class="primary" type="button">Run Agent</button>
        <button data-action="refresh-diff" data-id="${escapeHtml(branch.id)}" type="button">Diff</button>
        <button data-action="commit" data-id="${escapeHtml(branch.id)}" type="button">Commit</button>
        <button class="danger" data-action="discard" data-id="${escapeHtml(branch.id)}" type="button">Discard</button>
      </div>
      ${renderSnapshotTree(branch)}
      ${renderDiff(diffs[branch.id])}
    </article>
  `;
}

function renderBranches(branches, diffs = {}) {
  if (!branches.length) {
    branchesEl.innerHTML = `<p class="empty">No agent branches yet.</p>`;
    return;
  }

  const grouped = branches.reduce((groups, branch) => {
    const key = branch.base_id || "ad-hoc";
    groups[key] ||= [];
    groups[key].push(branch);
    return groups;
  }, {});

  branchesEl.innerHTML = Object.entries(grouped)
    .map(
      ([baseId, group]) => `
        <section class="branch-group">
          <div class="branch-group-header">
            <h3>${escapeHtml(baseTitle(baseId))}</h3>
            <span>${group.length} branch${group.length === 1 ? "" : "es"}</span>
          </div>
          <div class="branch-group-list">
            ${group.map((branch) => renderBranchCard(branch, diffs)).join("")}
          </div>
        </section>
      `,
    )
    .join("");
}

async function refreshBranches(diffs = {}) {
  Object.assign(branchDiffs, diffs);
  const [backendData, baseData, branchData] = await Promise.all([
    request("/api/backend"),
    request("/api/bases"),
    request("/api/branches"),
  ]);
  renderBackendStatus(backendData);
  baseCache = baseData.bases;
  renderBases(baseCache);

  const liveBranchIds = new Set(branchData.branches.map((branch) => branch.id));
  for (const branchId of Object.keys(branchDiffs)) {
    if (!liveBranchIds.has(branchId)) {
      delete branchDiffs[branchId];
    }
  }
  renderBranches(branchData.branches, branchDiffs);
}

async function refresh() {
  const [inventory, state] = await Promise.all([
    request("/api/inventory"),
    request("/api/state"),
  ]);
  renderInventory(inventory.items);
  renderState(state);
  await refreshBranches();
}

async function mutate(label, fn) {
  try {
    await fn();
    showResult(label);
    await refresh();
  } catch (error) {
    showResult(error.message, false);
    await refresh();
  }
}

document.querySelector("#refreshBtn").addEventListener("click", refresh);

document.querySelector("#resetBtn").addEventListener("click", async () => {
  await mutate("Reset", () => request("/api/reset", { method: "POST" }));
});

document.querySelector("#buyBtn").addEventListener("click", async () => {
  await mutate("Bought", () =>
    request("/api/inventory/buy", {
      method: "POST",
      body: JSON.stringify(selectedInventoryPayload()),
    }),
  );
});

document.querySelector("#sellBtn").addEventListener("click", async () => {
  await mutate("Sold", () =>
    request("/api/inventory/sell", {
      method: "POST",
      body: JSON.stringify(selectedInventoryPayload()),
    }),
  );
});

document.querySelector("#reserveBtn").addEventListener("click", async () => {
  await mutate("Reserved", () =>
    request("/api/reservations", {
      method: "POST",
      body: JSON.stringify(selectedInventoryPayload()),
    }),
  );
});

document.querySelector("#createBaseBtn").addEventListener("click", async () => {
  await mutate("Base created", async () => {
    const label = baseLabelInput.value.trim();
    const data = await request("/api/bases", {
      method: "POST",
      body: JSON.stringify({ label: label || null }),
    });
    selectedBaseId = data.base.id;
    baseLabelInput.value = "";
    await refreshBranches();
    return data.base;
  });
});

basesEl.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) {
    return;
  }
  const baseId = button.dataset.id;
  const action = button.dataset.action;
  try {
    if (action === "select-base") {
      selectedBaseId = baseId;
      renderBases(baseCache);
      showResult("Base selected");
      return;
    }
    if (action === "create-branch") {
      selectedBaseId = baseId;
      showResult("Creating branch...");
      const data = await request(`/api/bases/${baseId}/branches`, { method: "POST" });
      await refreshBranches();
      showResult(`Branch ${data.branch.id} created`);
      return;
    }
    if (action === "delete-base") {
      await request(`/api/bases/${baseId}`, { method: "DELETE" });
      if (selectedBaseId === baseId) {
        selectedBaseId = null;
      }
      await refreshBranches();
      showResult("Base deleted");
    }
  } catch (error) {
    showResult(error.message, false);
    await refreshBranches();
  }
});

branchesEl.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) {
    return;
  }
  const branchId = button.dataset.id;
  const action = button.dataset.action;
  try {
    if (action === "run-agent") {
      showResult("Agent running...");
      const data = await request(`/api/branches/${branchId}/run-agent-demo`, {
        method: "POST",
      });
      await refreshBranches({ [branchId]: data.diff });
      showResult("Agent plan done");
      return;
    }
    if (action === "refresh-diff") {
      const diff = await request(`/api/branches/${branchId}/diff`);
      await refreshBranches({ [branchId]: diff });
      showResult("Diff refreshed");
      return;
    }
    if (action === "commit") {
      await request(`/api/branches/${branchId}/commit`, { method: "POST" });
      showResult("Branch committed");
      await refresh();
      return;
    }
    if (action === "discard") {
      await request(`/api/branches/${branchId}/discard`, { method: "POST" });
      showResult("Branch discarded");
      await refresh();
    }
  } catch (error) {
    showResult(error.message, false);
    await refreshBranches();
  }
});

refresh().catch((error) => showResult(error.message, false));
