const appSelect = document.querySelector("#appSelect");
const runtimeFrame = document.querySelector("#runtimeFrame");
const resultPill = document.querySelector("#lastResult");
const checkpointsEl = document.querySelector("#checkpoints");
const snapshotLabelInput = document.querySelector("#snapshotLabelInput");
const aiPickBtn = document.querySelector("#aiPickBtn");
const aiPickModal = document.querySelector("#aiPickModal");
const aiChatBody = document.querySelector("#aiChatBody");
const aiChatChoices = document.querySelector("#aiChatChoices");
const aiPickClose = document.querySelector("#aiPickClose");
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
  title = "Building Workspace",
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

function renderApps(payload) {
  apps = payload.apps || [];
  currentAppId = payload.current_app_id;
  appSelect.innerHTML = apps
    .map(
      (app) => `<option value="${escapeHtml(app.id)}" ${app.id === currentAppId ? "selected" : ""}>${escapeHtml(app.label)}</option>`,
    )
    .join("");
}

// Outline numbering for one node among its siblings, by depth:
// depth 1 → 1, 2, 3 ; depth 2 → a, b, c ; deeper levels alternate number/letter.
function snapshotSegment(index, depth) {
  if (depth % 2 === 1) {
    return String(index + 1);
  }
  const letters = "abcdefghijklmnopqrstuvwxyz";
  return index < letters.length ? letters[index] : String(index + 1);
}

// Rebuild the checkpoint tree from the flat snapshot list using parent_id (set by
// the control plane on each snapshot). A snapshot whose parent is the base/build
// checkpoint (not itself a snapshot) is a root — normally just "Initial checkpoint".
function buildSnapshotTree(snapshots) {
  const byId = new Map(snapshots.map((snap) => [snap.id, snap]));
  const childrenOf = new Map();
  const roots = [];
  for (const snap of snapshots) {
    if (snap.parent_id && byId.has(snap.parent_id)) {
      const siblings = childrenOf.get(snap.parent_id) || [];
      siblings.push(snap);
      childrenOf.set(snap.parent_id, siblings);
    } else {
      roots.push(snap);
    }
  }
  const byTime = (a, b) => (a.created_at || 0) - (b.created_at || 0);
  roots.sort(byTime);
  for (const siblings of childrenOf.values()) {
    siblings.sort(byTime);
  }
  return { roots, childrenOf };
}

function renderSnapshotNodes(nodes, depth, parentLabel, childrenOf, branch) {
  return nodes
    .map((snap, index) => {
      const isInitial = depth === 0;
      const segment = isInitial ? "" : snapshotSegment(index, depth);
      const label = isInitial ? "" : parentLabel ? `${parentLabel}.${segment}` : segment;
      const indent = depth * 18;
      const fontSize = Math.max(11, 13 - depth);
      const marker = isInitial
        ? `<span class="ckpt-init" title="Initial snapshot" aria-hidden="true">●</span>`
        : `<span class="ckpt-num">${escapeHtml(label)}</span>`;
      const kids = childrenOf.get(snap.id) || [];
      return `
        <div class="ckpt-row" style="padding-left:${indent}px;font-size:${fontSize}px">
          ${marker}
          <div class="ckpt-body">
            <div class="checkpoint-title">
              <strong>${escapeHtml(snap.label)}</strong>
              ${snap.id === branch.current_snapshot_id ? badge("current") : ""}
            </div>
            <p>${escapeHtml(snap.backend)} · ${escapeHtml(snap.id)}</p>
            <p>${escapeHtml(formatTime(snap.created_at))}</p>
            <button data-action="restore-snapshot" data-snapshot-id="${escapeHtml(snap.id)}" type="button">Restore</button>
          </div>
        </div>
        ${kids.length ? renderSnapshotNodes(kids, depth + 1, label, childrenOf, branch) : ""}
      `;
    })
    .join("");
}

function renderCheckpoints(branch) {
  const snapshots = branch.snapshots || [];
  if (!snapshots.length) {
    checkpointsEl.innerHTML = `<p class="empty">No snapshots yet.</p>`;
    return;
  }

  const { roots, childrenOf } = buildSnapshotTree(snapshots);
  checkpointsEl.innerHTML = `
    <section class="snapshot-tree checkpoint-tree">
      <div class="snapshot-root">
        <span></span>
        <div>
          <strong>Workspace runtime</strong>
          <p>${escapeHtml(branch.id)} · ${escapeHtml(branch.url)}</p>
        </div>
      </div>
      <div class="ckpt-tree">
        ${renderSnapshotNodes(roots, 0, "", childrenOf, branch)}
      </div>
    </section>
  `;
}

async function refreshApps() {
  const payload = await request("/api/apps");
  renderApps(payload);
  return payload;
}

async function refreshWorkspace({ skipFrame = false } = {}) {
  const data = await request("/api/workspace");
  workspace = data;
  currentAppId = data.app.id;
  renderApps({ apps, current_app_id: currentAppId });
  renderCheckpoints(data.branch);
  // Dirty/clean indicator on the result pill: red "unsnapshot change" when the
  // runtime differs from the last snapshot; otherwise leave the last action result.
  if (data.branch && data.branch.dirty) {
    showResult("unsnapshot change", false);
  }
  // AI Pick navigates the iframe to the cart itself, so it skips the frame reset.
  if (!skipFrame) {
    const url = data.workspace.runtime_ui_url;
    if (runtimeFrame.getAttribute("src") !== url) {
      showBuilding();
      runtimeFrame.src = url; // the load handler hides the overlay
    } else {
      hideBuilding();
    }
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
      const discard = window.confirm("Discard unsaved changes and restore the selected snapshot?");
      if (!discard) {
        showResult("Restore canceled");
        return;
      }
      force = true;
    }
  }
  // The runtime is about to revert (e.g. the storefront cart), so reload the
  // embedded iframe — refreshWorkspace re-sets src once it is cleared.
  showBuilding("Restoring snapshot…", "Reverting the shop runtime to the selected snapshot.");
  runtimeFrame.removeAttribute("src");
  await request("/api/workspace/restore", {
    method: "POST",
    body: JSON.stringify({ snapshot_id: snapshotId, force }),
  });
  showResult("Snapshot restored");
  await refresh();
}

appSelect.addEventListener("change", async () => {
  const app = apps.find((candidate) => candidate.id === appSelect.value);
  const label = app?.label || appSelect.value;
  if (!window.confirm(`Switch to ${label} and reset the current workspace?`)) {
    appSelect.value = currentAppId;
    return;
  }
  showBuilding("Building Workspace", `Starting ${label}. A cold start can take up to a minute.`);
  runtimeFrame.removeAttribute("src");
  await mutate(`Switched to ${label}`, async () => {
    await request(`/api/apps/${encodeURIComponent(appSelect.value)}/select`, { method: "POST" });
    await refreshApps();
  });
});

document.querySelector("#snapshotBtn").addEventListener("click", async () => {
  const label = snapshotLabelInput.value.trim();
  await mutate("Snapshot saved", async () => {
    await saveWorkspaceSnapshot(label);
    snapshotLabelInput.value = "";
  });
});

// AI Pick — a scripted "AI stylist" that demos State-Fork: it reverts the shop to
// the Initial snapshot, drops a preset look into the cart, and snapshots it. The
// picks are hard-coded per shop (no real model); items are real mock-api variants.
const AI_PICK_PRESETS = {
  shop_clothing: {
    intro: "Hey! I'm your State-Fork stylist. Tell me today's vibe and I'll fill your bag.",
    closing:
      "Here's what I picked for you. I reverted the shop to a clean state, added these, and saved a snapshot — restore it anytime to get this look back.",
    choices: [
      {
        id: "cozy",
        label: "Cozy loungewear",
        items: [
          { id: 10000, name: "Plush Hoodie — Flamingo / XS" },
          { id: 10084, name: "Print Joggers — Birch Fade / XS" },
        ],
      },
      {
        id: "active",
        label: "Studio active",
        items: [
          { id: 10273, name: "7/8 High-Waist Legging — Meadow" },
          { id: 10248, name: "Athletic Short — Black / S" },
        ],
      },
      {
        id: "casual",
        label: "Smart casual",
        items: [
          { id: 10288, name: "Crew Neck Pullover — Oatmeal" },
          { id: 10471, name: "Dual-Layer Tee — Bone / S" },
        ],
      },
    ],
  },
  shop_cookware: {
    intro: "Hey! I'm your State-Fork kitchen helper. What are you cooking up today?",
    closing:
      "Here's your kit. I reverted the shop to a clean state, added these, and saved a snapshot — restore it anytime to get this setup back.",
    choices: [
      {
        id: "everyday",
        label: "Everyday cooking",
        items: [
          { id: 10000, name: '10-inch Nonstick Frypan — Slate Gray' },
          { id: 10002, name: "Stainless Steel Saucepan, 2 qt" },
        ],
      },
      {
        id: "baking",
        label: "Baking day",
        items: [
          { id: 10161, name: "Half-Sheet Baking Pan" },
          { id: 10163, name: "Round Cake Pan, 9-inch" },
        ],
      },
      {
        id: "prep",
        label: "Knife & prep",
        items: [
          { id: 10088, name: "8-inch Chef's Knife" },
          { id: 10237, name: "Bamboo Cutting Board, Large" },
        ],
      },
    ],
  },
  shop_hardware: {
    intro: "Hey! I'm your State-Fork setup assistant. What are you setting up today?",
    closing:
      "Here's your setup. I reverted the shop to a clean state, added these, and saved a snapshot — restore it anytime to get it back.",
    choices: [
      {
        id: "checkout",
        label: "Checkout counter",
        items: [
          { id: 10008, name: "Bluetooth Thermal Slip Printer" },
          { id: 10015, name: "16-Inch Heavy Duty Till Tray" },
        ],
      },
      {
        id: "scanpay",
        label: "Scan & pay",
        items: [
          { id: 10002, name: "2D Wired Code Reader with Cradle" },
          { id: 10069, name: "Compact Card Reader" },
        ],
      },
      {
        id: "labels",
        label: "Shipping & labels",
        items: [
          { id: 10028, name: "XL Direct Thermal Label Printer" },
          { id: 10032, name: "Direct Thermal Shipping Label Pack" },
        ],
      },
    ],
  },
};

function aiAddMessage(role, html) {
  const message = document.createElement("div");
  message.className = `ai-msg ${role}`;
  message.innerHTML = html;
  aiChatBody.appendChild(message);
  aiChatBody.scrollTop = aiChatBody.scrollHeight;
  return message;
}

function aiCloseModal() {
  aiPickModal.hidden = true;
}

const aiSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Show a typing indicator for `delay`, then turn it into the given message. The
// seq guard drops stale staging if the modal was reopened meanwhile.
async function aiTypeThenSay(seq, text, delay = 800) {
  const bubble = aiAddMessage("bot", `<span class="ai-typing"><i></i><i></i><i></i></span>`);
  await aiSleep(delay);
  if (seq !== aiSeq) {
    return false;
  }
  bubble.innerHTML = escapeHtml(text);
  return true;
}

async function aiRevealChoices(seq, preset, delay = 700) {
  const typing = aiAddMessage("bot", `<span class="ai-typing"><i></i><i></i><i></i></span>`);
  await aiSleep(delay);
  if (seq !== aiSeq) {
    return;
  }
  typing.remove();
  preset.choices.forEach((choice, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = choice.label; // just the vibe — no item details
    button.style.animationDelay = `${index * 100}ms`;
    button.addEventListener("click", () => aiChoose(preset, choice));
    aiChatChoices.appendChild(button);
  });
}

// Bumped each time the modal opens so pending staging timers know to stop.
let aiSeq = 0;

async function aiOpenModal() {
  const seq = ++aiSeq;
  aiChatBody.innerHTML = "";
  aiChatChoices.innerHTML = "";
  aiPickModal.hidden = false;
  const preset = AI_PICK_PRESETS[currentAppId];
  if (!preset) {
    await aiTypeThenSay(seq, "AI Pick isn't set up for this shop yet — try the Clothing Shop.");
    return;
  }
  // Open like a real chat: think, greet, think again, then reveal the choices.
  if (!(await aiTypeThenSay(seq, preset.intro))) {
    return;
  }
  await aiRevealChoices(seq, preset);
}

async function aiChoose(preset, choice) {
  aiChatChoices.innerHTML = "";
  aiAddMessage("user", escapeHtml(choice.label));
  const thinking = aiAddMessage("bot", `<span class="ai-typing"><i></i><i></i><i></i></span>`);
  try {
    await aiPickOrchestrate(choice);
    const list = choice.items.map((item) => `<li>${escapeHtml(item.name)}</li>`).join("");
    thinking.innerHTML = `${escapeHtml(preset.closing)}<ul>${list}</ul>`;
    const again = document.createElement("button");
    again.type = "button";
    again.textContent = "Pick another vibe";
    again.addEventListener("click", aiOpenModal);
    const view = document.createElement("button");
    view.type = "button";
    view.className = "primary";
    view.textContent = "View bag";
    view.addEventListener("click", aiCloseModal);
    aiChatChoices.appendChild(again);
    aiChatChoices.appendChild(view);
  } catch (error) {
    hideBuilding();
    thinking.innerHTML = `Sorry, something went wrong: ${escapeHtml(error.message)}`;
  }
}

async function aiPickOrchestrate(choice) {
  showBuilding("AI Pick…", "Reverting to a clean shop, adding your items, and saving a snapshot.");
  // 1. Revert to the Initial snapshot so every pick starts from a clean shop.
  const current = await request("/api/workspace");
  const initial = (current.branch.snapshots || [])[0];
  if (initial) {
    await request("/api/workspace/restore", {
      method: "POST",
      body: JSON.stringify({ snapshot_id: initial.id, force: true }),
    });
  }
  // 2. Add the picked lines through the storefront cart action — the same path the
  //    "Add to cart" button uses — so the browser's cart cookie is updated too.
  const lines = choice.items.map((item) => ({
    merchandiseId: `gid://shopify/ProductVariant/${item.id}`,
    quantity: 1,
  }));
  const body =
    "cartFormInput=" + encodeURIComponent(JSON.stringify({ action: "LinesAdd", inputs: { lines } }));
  const cartResponse = await fetch("/runtime/cart", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    credentials: "same-origin",
    body,
  });
  if (!cartResponse.ok) {
    throw new Error(`cart add failed (${cartResponse.status})`);
  }
  // 3. Snapshot the populated cart, then show the bag without resetting the frame.
  await saveWorkspaceSnapshot(`AI Pick — ${choice.label}`);
  runtimeFrame.src = "/runtime/cart";
  showResult(`AI Pick: ${choice.label}`);
  await refreshWorkspace({ skipFrame: true });
}

aiPickBtn.addEventListener("click", aiOpenModal);
aiPickClose.addEventListener("click", aiCloseModal);
aiPickModal.addEventListener("click", (event) => {
  if (event.target === aiPickModal) {
    aiCloseModal();
  }
});

document.querySelector("#resetBtn").addEventListener("click", async () => {
  if (!window.confirm("Reset the workspace and discard runtime snapshots?")) {
    return;
  }
  showBuilding("Building Workspace", "Resetting and rebuilding a clean shop runtime.");
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
