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
const catalogSection = document.querySelector("#catalogSection");
const catalogSummary = document.querySelector("#catalogSummary");
const catalogBtn = document.querySelector("#catalogBtn");
const catalogModal = document.querySelector("#catalogModal");
const catalogClose = document.querySelector("#catalogClose");
const catalogRows = document.querySelector("#catalogRows");
const catalogChanges = document.querySelector("#catalogChanges");
const catSearch = document.querySelector("#catSearch");
const mergeBox = document.querySelector("#mergeBox");
const mergeA = document.querySelector("#mergeA");
const mergeB = document.querySelector("#mergeB");
const mergeBase = document.querySelector("#mergeBase");
const mergeBtn = document.querySelector("#mergeBtn");

let apps = [];
let currentAppId = null;
let workspace = null;
let buildingTimer = null;
let catalogEnabled = false;
let catSearchTimer = null;

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
  const tone = ["current", "running"].includes(normalized) ? "ok" : "warn";
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
  updateCatalogSection(data.data_tier);
  updateMergeBox(data.branch, data.data_tier);
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
  // The runtime is about to revert (e.g. the storefront cart), so reload the
  // embedded iframe — refreshWorkspace re-sets src once it is cleared.
  showBuilding("Restoring snapshot…", "Reverting the shop runtime to the selected snapshot.");
  runtimeFrame.removeAttribute("src");
  await request("/api/workspace/restore", {
    method: "POST",
    body: JSON.stringify({ snapshot_id: snapshotId }),
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
      body: JSON.stringify({ snapshot_id: initial.id }),
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
  // POST the React Router single-fetch action endpoint (cart.data), NOT the
  // document route /runtime/cart — a plain POST to the latter renders the page
  // without persisting the lines. cart.data runs the action and sets the cart cookie.
  const cartResponse = await fetch("/runtime/cart.data", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    credentials: "same-origin",
    body,
  });
  if (!cartResponse.ok) {
    throw new Error(`cart add failed (${cartResponse.status})`);
  }
  // 3. Snapshot the populated cart.
  await saveWorkspaceSnapshot(`AI Pick — ${choice.label}`);
  showResult(`AI Pick: ${choice.label}`);
  // 4. Reload the home page (the cart badge now reflects the picks) and pop the
  //    cart drawer. This Hydrogen template has NO /cart page — the cart is an
  //    aside opened by the header cart button; the iframe is same-origin, so we
  //    click that button once it has hydrated.
  runtimeFrame.src = "/runtime/";
  openCartDrawerSoon();
  await refreshWorkspace({ skipFrame: true });
}

// Open the storefront's cart drawer inside the (same-origin) iframe by clicking
// the header cart button. Clicks before hydration are no-ops, so retry until the
// drawer reports open (`.overlay.expanded`) or we run out of attempts.
function openCartDrawerSoon() {
  let attempts = 0;
  const tryOpen = () => {
    attempts += 1;
    let doc;
    try {
      doc = runtimeFrame.contentDocument;
    } catch {
      return; // cross-origin (shouldn't happen) — give up quietly
    }
    if (doc) {
      if (doc.querySelector(".overlay.expanded")) {
        return; // a drawer is open — done
      }
      // The three shops use different themes (class names differ), but every cart
      // button carries an aria-label like "Cart, 2 items"; fall back to classes.
      const cartButton = doc.querySelector(
        '[aria-label^="Cart,"], .site-header-cart, .header-cart-button',
      );
      cartButton?.click();
    }
    if (attempts < 12) {
      setTimeout(tryOpen, 400);
    }
  };
  setTimeout(tryOpen, 700);
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

// ── Catalog editor ─────────────────────────────────────────────────────────
// Edits price/stock in the external Dolt working set (architecture A). A
// Snapshot commits them to a Dolt branch; Restore/Reset rolls them back. The
// section is shown only when the Dolt data tier is live for the current shop.

function updateCatalogSection(dataTier) {
  catalogEnabled = Boolean(dataTier && dataTier.enabled);
  catalogSection.hidden = !catalogEnabled;
  if (!catalogEnabled) {
    catalogModal.hidden = true;
    return;
  }
  // Cheap summary probe (also refreshes the "N changes" line).
  request("/api/catalog")
    .then((data) => {
      const n = (data.changes || []).length;
      catalogSummary.textContent = n
        ? `${data.variants.length} variants · ${n} edited vs clean`
        : `${data.variants.length} variants · no edits yet`;
    })
    .catch(() => {
      catalogSummary.textContent = "—";
    });
}

function renderCatalogChanges(changes) {
  if (!changes || !changes.length) {
    catalogChanges.innerHTML = `<p class="muted">No edits yet. Change a price or stock below, then Snapshot to commit it to a Dolt branch.</p>`;
    return;
  }
  const money = (v) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);
  const rows = changes
    .map((c) => {
      const bits = [];
      if (c.price && c.price.from !== c.price.to) {
        bits.push(`price ${money(c.price.from)} → <strong>${money(c.price.to)}</strong>`);
      }
      if (c.on_hand && c.on_hand.from !== c.on_hand.to) {
        bits.push(`stock ${c.on_hand.from ?? "—"} → <strong>${c.on_hand.to ?? "—"}</strong>`);
      }
      const name = `${escapeHtml(c.product_title || "")}${c.variant_title ? " · " + escapeHtml(c.variant_title) : ""}`;
      return `<li>${name} — ${bits.join(", ") || escapeHtml(c.diff_type || "changed")}</li>`;
    })
    .join("");
  catalogChanges.innerHTML = `<strong>Changes vs clean catalog (${changes.length})</strong><ul>${rows}</ul>`;
}

function renderCatalogRows(variants) {
  if (!variants || !variants.length) {
    catalogRows.innerHTML = `<tr><td colspan="6" class="muted">No variants.</td></tr>`;
    return;
  }
  catalogRows.innerHTML = variants
    .map((v) => {
      const cmp = v.compare_at_price == null ? "" : Number(v.compare_at_price).toFixed(2);
      return `
        <tr data-variant-id="${escapeHtml(v.variant_id)}">
          <td>${escapeHtml(v.product_title || "")}</td>
          <td>${escapeHtml(v.variant_title || "")}</td>
          <td><input class="cat-price" type="number" min="0" step="0.01" value="${Number(v.price).toFixed(2)}" data-field="price" /></td>
          <td><input class="cat-cmp" type="number" min="0" step="0.01" value="${cmp}" data-field="compare_at_price" placeholder="—" /></td>
          <td><input class="cat-stock" type="number" min="0" step="1" value="${v.on_hand ?? 0}" data-field="on_hand" /></td>
          <td>${v.available ? badge("in stock") : badge("sold out")}</td>
        </tr>`;
    })
    .join("");
}

async function loadCatalog(search) {
  const query = search ? `?search=${encodeURIComponent(search)}` : "";
  const data = await request(`/api/catalog${query}`);
  renderCatalogChanges(data.changes);
  renderCatalogRows(data.variants);
}

async function openCatalog() {
  catalogModal.hidden = false;
  catSearch.value = "";
  catalogRows.innerHTML = `<tr><td colspan="6" class="muted">Loading…</td></tr>`;
  try {
    await loadCatalog("");
  } catch (error) {
    catalogRows.innerHTML = `<tr><td colspan="6">${escapeHtml(error.message)}</td></tr>`;
  }
}

function closeCatalog() {
  catalogModal.hidden = true;
}

// Save one edited field for a variant, then refresh the change list + summary.
async function saveVariantField(variantId, field, rawValue) {
  const body = {};
  const value = rawValue === "" ? null : Number(rawValue);
  if (field === "compare_at_price" && value === null) {
    return; // clearing compare-at is a no-op edit for the demo
  }
  body[field] = value;
  const result = await request(`/api/catalog/${encodeURIComponent(variantId)}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  renderCatalogChanges(result.changes);
  showResult("Catalog updated");
  // Reflect the "N changes" line in the rail and the availability badge.
  updateCatalogSection(workspace && workspace.data_tier);
  const row = catalogRows.querySelector(`tr[data-variant-id="${CSS.escape(variantId)}"]`);
  if (row && result.variant) {
    const badgeCell = row.lastElementChild;
    badgeCell.innerHTML = result.variant.available ? badge("in stock") : badge("sold out");
  }
}

catalogBtn.addEventListener("click", openCatalog);
catalogClose.addEventListener("click", closeCatalog);
catalogModal.addEventListener("click", (event) => {
  if (event.target === catalogModal) {
    closeCatalog();
  }
});

// Commit an edit when a cell loses focus or Enter is pressed.
catalogRows.addEventListener("change", async (event) => {
  const input = event.target.closest("input[data-field]");
  if (!input) {
    return;
  }
  const row = input.closest("tr[data-variant-id]");
  try {
    await saveVariantField(row.dataset.variantId, input.dataset.field, input.value);
  } catch (error) {
    showResult(error.message, false);
  }
});

catSearch.addEventListener("input", () => {
  clearTimeout(catSearchTimer);
  catSearchTimer = setTimeout(() => {
    loadCatalog(catSearch.value.trim()).catch((error) => showResult(error.message, false));
  }, 250);
});

// ── Merge (Dolt data branches) ───────────────────────────────────────────────
// Combine two snapshots' catalog data into a new snapshot. CRIU can't merge, so
// you pick which app checkpoint the merged data runs on (Initial / A / B).
function updateMergeBox(branch, dataTier) {
  const enabled = Boolean(dataTier && dataTier.enabled);
  const snaps = (branch && branch.snapshots) || [];
  mergeBox.hidden = !(enabled && snaps.length >= 2);
  if (mergeBox.hidden) return;
  const options = snaps
    .map((s) => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.label)}</option>`)
    .join("");
  const prevA = mergeA.value;
  const prevB = mergeB.value;
  mergeA.innerHTML = options;
  mergeB.innerHTML = options;
  const has = (id) => snaps.some((s) => s.id === id);
  mergeA.value = has(prevA) ? prevA : (snaps[1] || snaps[0]).id;
  mergeB.value = has(prevB) ? prevB : snaps[snaps.length - 1].id;
}

mergeBtn.addEventListener("click", async () => {
  const a = mergeA.value;
  const b = mergeB.value;
  const app_base = mergeBase.value;
  if (a === b) {
    showResult("Pick two different snapshots", false);
    return;
  }
  showBuilding("Merging…", "Combining the two snapshots' catalog data and snapshotting the result.");
  runtimeFrame.removeAttribute("src");
  await mutate("Snapshots merged", async () => {
    await request("/api/workspace/merge", {
      method: "POST",
      body: JSON.stringify({ a, b, app_base }),
    });
  });
});

// Collapse / expand the control rail to give the shop website the full screen.
railHideBtn.addEventListener("click", () => document.body.classList.add("rail-collapsed"));
railShowBtn.addEventListener("click", () => document.body.classList.remove("rail-collapsed"));

showBuilding();
refresh().catch((error) => {
  hideBuilding();
  showResult(error.message, false);
});
