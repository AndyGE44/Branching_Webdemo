const mailboxSummaryEl = document.querySelector("#mailboxSummary");
const messageListEl = document.querySelector("#messageList");
const messageDetailEl = document.querySelector("#messageDetail");
const stateView = document.querySelector("#stateView");
const resultPill = document.querySelector("#lastResult");
const basesEl = document.querySelector("#bases");
const baseLabelInput = document.querySelector("#baseLabelInput");
const branchesEl = document.querySelector("#branches");
const backendModeEl = document.querySelector("#backendMode");
const backendStatsEl = document.querySelector("#backendStats");
const snapshotModeEl = document.querySelector("#snapshotMode");
let selectedBaseId = null;
let baseCache = [];
let selectedMessageId = null;

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

function renderMailboxSummary(mailbox) {
  const folderCards = mailbox.folders
    .map((folder) => statCard(folder.folder, folder.count, "folder"))
    .join("");
  mailboxSummaryEl.innerHTML = [
    statCard("Unread", mailbox.unread, "needs review"),
    statCard("Drafts", mailbox.drafts, "saved replies"),
    folderCards,
  ].join("");
}

function labelPills(labels) {
  if (!labels.length) {
    return `<span class="muted inline-muted">No labels</span>`;
  }
  return labels.map((label) => `<span class="label-pill">${escapeHtml(label)}</span>`).join("");
}

function replySubject(subject) {
  return String(subject || "").toLowerCase().startsWith("re:") ? subject : `Re: ${subject}`;
}

function renderMessageList(messages) {
  if (messages.length && !selectedMessageId) {
    selectedMessageId = messages[0].id;
  }
  if (selectedMessageId && !messages.some((message) => message.id === selectedMessageId)) {
    selectedMessageId = messages[0]?.id || null;
  }

  messageListEl.innerHTML = messages.length
    ? messages
        .map((message) => {
          const selected = message.id === selectedMessageId ? "selected" : "";
          const unread = message.is_read ? "" : "unread";
          return `
            <button class="message-row ${selected} ${unread}" data-message-id="${escapeHtml(message.id)}" type="button">
              <div>
                <strong>${escapeHtml(message.subject)}</strong>
                <p>${escapeHtml(message.from_address)}</p>
              </div>
              <div class="message-meta">
                ${badge(message.folder)}
                ${badge(message.priority)}
              </div>
            </button>
          `;
        })
        .join("")
    : `<p class="empty">No messages yet.</p>`;
}

function renderMessageDetail(message) {
  if (!message) {
    messageDetailEl.innerHTML = `<p class="empty">Select a message.</p>`;
    return;
  }
  messageDetailEl.innerHTML = `
    <article>
      <div class="message-detail-header">
        <div>
          <h3>${escapeHtml(message.subject)}</h3>
          <p>From ${escapeHtml(message.from_address)}</p>
          <p>To ${escapeHtml(message.to_address)}</p>
        </div>
        <div class="message-meta">
          ${badge(message.folder)}
          ${badge(message.priority)}
          ${message.is_read ? badge("read") : badge("unread")}
        </div>
      </div>
      <div class="label-row">${labelPills(message.labels)}</div>
      <p class="message-body">${escapeHtml(message.body)}</p>
      <section class="message-actions" data-message-id="${escapeHtml(message.id)}">
        <div class="quick-actions">
          <form data-action="label-message" class="inline-form">
            <label>
              Label
              <input name="label" type="text" maxlength="40" placeholder="finance" />
            </label>
            <button class="primary" type="submit">Add</button>
          </form>
          <form data-action="move-message" class="inline-form">
            <label>
              Folder
              <select name="folder">
                ${["Inbox", "Archive", "Spam"]
                  .map(
                    (folder) =>
                      `<option value="${folder}" ${folder === message.folder ? "selected" : ""}>${folder}</option>`,
                  )
                  .join("")}
              </select>
            </label>
            <button type="submit">Move</button>
          </form>
          <button data-action="toggle-read" data-read="${message.is_read ? "false" : "true"}" type="button">
            Mark ${message.is_read ? "Unread" : "Read"}
          </button>
          <button data-action="archive-message" type="button">Archive</button>
        </div>

        <form data-action="create-draft" class="draft-form">
          <div class="draft-form-row">
            <label>
              To
              <input name="to_address" type="email" value="${escapeHtml(message.from_address)}" />
            </label>
            <label>
              Subject
              <input name="subject" type="text" maxlength="200" value="${escapeHtml(replySubject(message.subject))}" />
            </label>
          </div>
          <label>
            Reply
            <textarea name="body" rows="4" placeholder="Draft a reply..."></textarea>
          </label>
          <div class="button-row">
            <button class="primary" type="submit">Create Draft</button>
          </div>
        </form>
      </section>
    </article>
  `;
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

function renderDraftContents(drafts) {
  return `
    <section class="state-card draft-content-card">
      <div class="state-card-header">
        <h3>Draft Contents</h3>
        <span>${drafts.length}</span>
      </div>
      <div class="draft-content-list">
        ${
          drafts.length
            ? drafts
                .map(
                  (draft) => `
                    <article class="draft-preview">
                      <div class="draft-preview-header">
                        <div>
                          <strong>${escapeHtml(draft.subject)}</strong>
                          <p>To ${escapeHtml(draft.to_address)}</p>
                        </div>
                        ${badge(draft.created_by)}
                      </div>
                      <p class="draft-preview-body">${escapeHtml(draft.body)}</p>
                    </article>
                  `,
                )
                .join("")
            : `<p class="empty">No draft content yet.</p>`
        }
      </div>
    </section>
  `;
}

function renderState(state) {
  const folderCount = state.mailbox.folders.length;
  const unreadCount = state.messages.filter((message) => !message.is_read).length;

  stateView.innerHTML = `
    <section class="state-summary">
      <div>
        <strong>${state.messages.length}</strong>
        <span>Messages</span>
      </div>
      <div>
        <strong>${unreadCount}</strong>
        <span>Unread</span>
      </div>
      <div>
        <strong>${folderCount}</strong>
        <span>Folders</span>
      </div>
      <div>
        <strong>${state.drafts.length}</strong>
        <span>Drafts</span>
      </div>
    </section>

    <div class="state-grid">
      ${renderTable("Messages", state.messages, [
        { label: "ID", value: "id" },
        { label: "Folder", value: (row) => badge(row.folder), html: true },
        { label: "Priority", value: (row) => badge(row.priority), html: true },
        { label: "Subject", value: "subject" },
        { label: "From", value: "from_address" },
      ])}

      ${renderTable("Drafts", state.drafts, [
        { label: "ID", value: "id" },
        { label: "To", value: "to_address" },
        { label: "Subject", value: "subject" },
        { label: "Status", value: (row) => badge(row.status), html: true },
        { label: "Created By", value: "created_by" },
      ])}
    </div>

    ${renderDraftContents(state.drafts)}

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

function renderBranchCard(branch) {
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
        <button class="primary" data-action="run-agent" data-id="${escapeHtml(branch.id)}" type="button">Run Email Agent</button>
        <button data-action="commit" data-id="${escapeHtml(branch.id)}" type="button">Commit</button>
        <button class="danger" data-action="discard" data-id="${escapeHtml(branch.id)}" type="button">Discard</button>
      </div>
      ${renderSnapshotTree(branch)}
    </article>
  `;
}

function renderBranches(branches) {
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
            ${group.map((branch) => renderBranchCard(branch)).join("")}
          </div>
        </section>
      `,
    )
    .join("");
}

async function refreshBranches() {
  const [backendData, baseData, branchData] = await Promise.all([
    request("/api/backend"),
    request("/api/bases"),
    request("/api/branches"),
  ]);
  renderBackendStatus(backendData);
  baseCache = baseData.bases;
  renderBases(baseCache);
  renderBranches(branchData.branches);
}

async function refresh() {
  const [mailbox, state] = await Promise.all([
    request("/api/mailbox"),
    request("/api/state"),
  ]);
  renderMailboxSummary(mailbox);
  renderMessageList(mailbox.messages);
  renderMessageDetail(mailbox.messages.find((message) => message.id === selectedMessageId));
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

messageListEl.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-message-id]");
  if (!button) {
    return;
  }
  selectedMessageId = button.dataset.messageId;
  refresh().catch((error) => showResult(error.message, false));
});

messageDetailEl.addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-action]");
  if (!form) {
    return;
  }
  event.preventDefault();
  const actions = form.closest(".message-actions");
  const messageId = actions?.dataset.messageId;
  if (!messageId) {
    return;
  }
  const action = form.dataset.action;
  const formData = new FormData(form);
  if (action === "label-message") {
    await mutate("Label added", () =>
      request(`/api/messages/${messageId}/label`, {
        method: "POST",
        body: JSON.stringify({ label: formData.get("label") }),
      }),
    );
    return;
  }
  if (action === "move-message") {
    await mutate("Message moved", () =>
      request(`/api/messages/${messageId}/move`, {
        method: "POST",
        body: JSON.stringify({ folder: formData.get("folder") }),
      }),
    );
    return;
  }
  if (action === "create-draft") {
    await mutate("Draft created", () =>
      request("/api/drafts", {
        method: "POST",
        body: JSON.stringify({
          source_message_id: messageId,
          to_address: formData.get("to_address"),
          subject: formData.get("subject"),
          body: formData.get("body"),
        }),
      }),
    );
  }
});

messageDetailEl.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) {
    return;
  }
  const actions = button.closest(".message-actions");
  const messageId = actions?.dataset.messageId;
  if (!messageId) {
    return;
  }
  const action = button.dataset.action;
  if (action === "toggle-read") {
    await mutate("Read state updated", () =>
      request(`/api/messages/${messageId}/read`, {
        method: "POST",
        body: JSON.stringify({ is_read: button.dataset.read === "true" }),
      }),
    );
    return;
  }
  if (action === "archive-message") {
    await mutate("Message archived", () =>
      request(`/api/messages/${messageId}/archive`, {
        method: "POST",
        body: JSON.stringify({ actor: "user" }),
      }),
    );
  }
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
      showResult("Running email agent...");
      const data = await request(`/api/branches/${branchId}/run-agent-demo`, { method: "POST" });
      showResult(`Email agent ran ${data.snapshots.length} steps`);
      await refreshBranches();
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
