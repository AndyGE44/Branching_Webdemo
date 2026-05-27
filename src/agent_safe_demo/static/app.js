const mailboxSummaryEl = document.querySelector("#mailboxSummary");
const messageListEl = document.querySelector("#messageList");
const messageDetailEl = document.querySelector("#messageDetail");
const stateView = document.querySelector("#stateView");
const resultPill = document.querySelector("#lastResult");
const backendModeEl = document.querySelector("#backendMode");
const backendStatsEl = document.querySelector("#backendStats");
const snapshotModeEl = document.querySelector("#snapshotMode");
const workspaceStateEl = document.querySelector("#workspaceState");
const checkpointsEl = document.querySelector("#checkpoints");
const snapshotLabelInput = document.querySelector("#snapshotLabelInput");

let selectedMessageId = null;
let workspace = null;
let runtimeBaseUrl = null;

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
  if (["ready", "active", "draft", "saved", "current"].includes(normalized)) {
    tone = "ok";
  }
  if (["blocked", "failed", "dirty", "unsaved"].includes(normalized)) {
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

function renderBackendStatus(status, branch) {
  const snapshotOps = status.operations?.snapshot || {};
  const restoreOps = status.operations?.restore || {};
  const totals = status.totals || {};
  backendModeEl.textContent = `${status.backend} / ${status.method}`;
  snapshotModeEl.textContent = `${branch.id} at ${branch.url}`;
  backendStatsEl.innerHTML = [
    statCard("Backend", status.backend, status.method),
    statCard("Runtime", branch.status, branch.id),
    statCard("Checkpoints", branch.snapshots?.length ?? 0, "manual save points"),
    statCard("Saved State", branch.current_snapshot_id || "none", branch.dirty ? "unsaved changes" : "clean"),
    statCard("Snapshot Calls", snapshotOps.count ?? 0, `avg ${formatMs(snapshotOps.mean_ms)}`),
    statCard("Restore Calls", restoreOps.count ?? 0, `avg ${formatMs(restoreOps.mean_ms)}`),
    statCard("Bases", totals.bases ?? 0, "controller internals"),
    statCard("Branches", totals.branches ?? 0, "runtime processes"),
  ].join("");
}

function renderWorkspaceState(branch) {
  workspaceStateEl.innerHTML = branch.dirty ? badge("unsaved") : badge("saved");
  workspaceStateEl.title = branch.dirty
    ? "The runtime has changes since the last snapshot."
    : "The runtime matches the current checkpoint.";
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

async function refreshWorkspace() {
  const data = await request("/api/workspace");
  workspace = data;
  runtimeBaseUrl = data.workspace.runtime_url;
  renderWorkspaceState(data.branch);
  renderBackendStatus(data.backend, data.branch);
  renderCheckpoints(data.branch);
  return data;
}

async function refresh() {
  await refreshWorkspace();
  const [mailbox, state] = await Promise.all([
    runtimeRequest("/api/mailbox"),
    runtimeRequest("/api/state"),
  ]);
  renderMailboxSummary(mailbox);
  renderMessageList(mailbox.messages);
  renderMessageDetail(mailbox.messages.find((message) => message.id === selectedMessageId));
  renderState(state);
}

async function mutate(label, fn) {
  try {
    await fn();
    showResult(label);
    await refresh();
  } catch (error) {
    showResult(error.message, false);
    try {
      await refresh();
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
  await request("/api/workspace/restore", {
    method: "POST",
    body: JSON.stringify({ snapshot_id: snapshotId, force }),
  });
  showResult("Checkpoint restored");
  await refresh();
}

document.querySelector("#refreshBtn").addEventListener("click", refresh);

document.querySelector("#snapshotBtn").addEventListener("click", async () => {
  const label = snapshotLabelInput.value.trim();
  await mutate("Snapshot saved", async () => {
    await saveWorkspaceSnapshot(label);
    snapshotLabelInput.value = "";
  });
});

document.querySelector("#runAgentBtn").addEventListener("click", async () => {
  try {
    showResult("Running email agent...");
    const data = await request("/api/workspace/run-agent", { method: "POST" });
    showResult(`Email agent ran ${data.actions.length} actions`);
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
  selectedMessageId = null;
  await mutate("Workspace reset", () => request("/api/workspace/reset", { method: "POST" }));
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
      runtimeRequest(`/api/messages/${messageId}/label`, {
        method: "POST",
        body: JSON.stringify({ label: formData.get("label") }),
      }),
    );
    return;
  }
  if (action === "move-message") {
    await mutate("Message moved", () =>
      runtimeRequest(`/api/messages/${messageId}/move`, {
        method: "POST",
        body: JSON.stringify({ folder: formData.get("folder") }),
      }),
    );
    return;
  }
  if (action === "create-draft") {
    await mutate("Draft created", () =>
      runtimeRequest("/api/drafts", {
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
      runtimeRequest(`/api/messages/${messageId}/read`, {
        method: "POST",
        body: JSON.stringify({ is_read: button.dataset.read === "true" }),
      }),
    );
    return;
  }
  if (action === "archive-message") {
    await mutate("Message archived", () =>
      runtimeRequest(`/api/messages/${messageId}/archive`, {
        method: "POST",
        body: JSON.stringify({ actor: "user" }),
      }),
    );
  }
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
