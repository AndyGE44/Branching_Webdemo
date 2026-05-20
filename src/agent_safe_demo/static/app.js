const inventoryEl = document.querySelector("#inventory");
const stateView = document.querySelector("#stateView");
const resultPill = document.querySelector("#lastResult");
const partSelect = document.querySelector("#partSelect");
const substituteSelect = document.querySelector("#substituteSelect");
const quantityInput = document.querySelector("#quantityInput");
const actorSelect = document.querySelector("#actorSelect");
const buildOrderInput = document.querySelector("#buildOrderInput");
const branchesEl = document.querySelector("#branches");
const branchDiffs = {};

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
  substituteSelect.innerHTML = options;
  if (currentPart) {
    partSelect.value = currentPart;
  }
  substituteSelect.value = items.some((item) => item.id === "MCU-ALT")
    ? "MCU-ALT"
    : items[0]?.id;
}

function badge(value) {
  const safeValue = escapeHtml(value);
  const normalized = String(value ?? "").toLowerCase();
  let tone = "neutral";
  if (["ready", "active", "draft"].includes(normalized)) {
    tone = "ok";
  }
  if (["blocked", "failed", "substitute_failed"].includes(normalized)) {
    tone = "warn";
  }
  return `<span class="badge ${tone}">${safeValue}</span>`;
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

      ${renderTable("Build Orders", state.build_orders, [
        { label: "ID", value: "id" },
        { label: "SKU", value: "sku" },
        { label: "Part", value: "part_id" },
        { label: "Qty", value: "quantity" },
        { label: "Status", value: (row) => badge(row.status), html: true },
        { label: "Validation", value: "validation_message" },
      ])}

      ${renderTable("Purchase Orders", state.purchase_orders, [
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
    return `<p class="empty">Run the branch agent flow to see a diff.</p>`;
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
              <td>${item.available_delta > 0 ? "+" : ""}${item.available_delta}</td>
              <td>${item.reserved_delta > 0 ? "+" : ""}${item.reserved_delta}</td>
            </tr>
          `,
        )
        .join("")
    : emptyRow(3, "No inventory quantity changes");

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
          <tr><th>Part</th><th>Available Δ</th><th>Reserved Δ</th></tr>
        </thead>
        <tbody>${inventoryRows}</tbody>
      </table>
    </div>
  `;
}

function renderBranches(branches, diffs = {}) {
  branchesEl.innerHTML = branches.length
    ? branches
        .map(
          (branch) => {
            const checkpointDetails = branch.base_checkpoint_id
              ? `<p>session ${escapeHtml(branch.session_id)} · base ${escapeHtml(branch.base_checkpoint_id)}</p>`
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
                <button data-action="run-agent" data-id="${escapeHtml(branch.id)}" type="button">Run Agent</button>
                <button data-action="refresh-diff" data-id="${escapeHtml(branch.id)}" type="button">Diff</button>
                <button data-action="commit" data-id="${escapeHtml(branch.id)}" type="button">Commit</button>
                <button class="danger" data-action="discard" data-id="${escapeHtml(branch.id)}" type="button">Discard</button>
              </div>
              ${renderDiff(diffs[branch.id])}
            </article>
          `;
          },
        )
        .join("")
    : `<p class="empty">No agent branches yet.</p>`;
}

async function refreshBranches(diffs = {}) {
  Object.assign(branchDiffs, diffs);
  const data = await request("/api/branches");
  const liveBranchIds = new Set(data.branches.map((branch) => branch.id));
  for (const branchId of Object.keys(branchDiffs)) {
    if (!liveBranchIds.has(branchId)) {
      delete branchDiffs[branchId];
    }
  }
  renderBranches(data.branches, branchDiffs);
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
    const data = await fn();
    showResult(label);
    if (data.build_order_id) {
      buildOrderInput.value = data.build_order_id;
    }
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

document.querySelector("#reserveBtn").addEventListener("click", async () => {
  await mutate("Reserved", () =>
    request("/api/reservations", {
      method: "POST",
      body: JSON.stringify({
        part_id: partSelect.value,
        quantity: Number(quantityInput.value),
        actor: actorSelect.value,
      }),
    }),
  );
});

document.querySelector("#buildBtn").addEventListener("click", async () => {
  await mutate("Build order", () =>
    request("/api/build-orders", {
      method: "POST",
      body: JSON.stringify({
        sku: "DEMO-KIT",
        part_id: partSelect.value,
        quantity: Number(quantityInput.value),
        actor: actorSelect.value,
      }),
    }),
  );
});

document.querySelector("#poBtn").addEventListener("click", async () => {
  await mutate("Draft PO", () =>
    request("/api/purchase-orders", {
      method: "POST",
      body: JSON.stringify({
        part_id: partSelect.value,
        quantity: Number(quantityInput.value),
        actor: actorSelect.value,
      }),
    }),
  );
});

document.querySelector("#substituteBtn").addEventListener("click", async () => {
  await mutate("Substitute checked", () =>
    request(`/api/build-orders/${Number(buildOrderInput.value)}/try-substitute`, {
      method: "POST",
      body: JSON.stringify({
        substitute_part_id: substituteSelect.value,
        actor: actorSelect.value,
      }),
    }),
  );
});

document.querySelector("#agentDemoBtn").addEventListener("click", async () => {
  try {
    showResult("Running...");
    const order = await request("/api/build-orders", {
      method: "POST",
      body: JSON.stringify({
        sku: "AGENT-EXPLORATION",
        part_id: "SENSOR-9",
        quantity: 5,
        actor: "agent",
      }),
    });
    buildOrderInput.value = order.build_order_id;
    await request(`/api/build-orders/${order.build_order_id}/try-substitute`, {
      method: "POST",
      body: JSON.stringify({ substitute_part_id: "MCU-ALT", actor: "agent" }),
    });
    await request("/api/purchase-orders", {
      method: "POST",
      body: JSON.stringify({ part_id: "SENSOR-9", quantity: 6, actor: "agent" }),
    });
    showResult("Agent flow done");
    await refresh();
  } catch (error) {
    showResult(error.message, false);
    await refresh();
  }
});

document.querySelector("#createBranchBtn").addEventListener("click", async () => {
  await mutate("Branch created", async () => {
    const data = await request("/api/branches", { method: "POST" });
    await refreshBranches();
    return data.branch;
  });
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
      showResult("Branch agent...");
      const data = await request(`/api/branches/${branchId}/run-agent-demo`, {
        method: "POST",
      });
      await refreshBranches({ [branchId]: data.diff });
      showResult("Branch agent done");
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
