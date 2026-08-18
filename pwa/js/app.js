/**
 * Main UI controller — vanilla JS, no framework (CLAUDE.md). A tiny hash
 * router over a handful of full-screen views, each a render(state) function
 * that rebuilds a chunk of DOM. Deliberately simple: this app has ~6
 * screens total, not enough surface to justify a framework's overhead on a
 * device that might be a low-end Android phone in the field.
 */

const state = {
  screen: "login",
  selectedBlockId: null,
  selectedTreeId: null,
  location: null,
  watchId: null,
  error: null,
  // Survives the re-render that showError() triggers. Without this the
  // login screen came back with both fields blank after a bad password,
  // forcing a gloved user to retype their username every time.
  loginUsername: "",
};

const root = () => document.getElementById("app");

/** Every screen here builds HTML strings and assigns them to innerHTML, so
 * any server- or user-supplied value interpolated into one has to be
 * escaped. Block/tree names come from the API and are editable in the
 * office admin, and error messages can echo a server response body.
 */
function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/"/g, "&quot;");
}

/** Trees are shown in the order a person walking the block would expect,
 * not IndexedDB insertion order (which put A2, A4, A1, A3 on screen).
 * Numeric-aware so "Tree 10" sorts after "Tree 9".
 */
const TREE_COLLATOR = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function sortTrees(trees) {
  return [...trees].sort((a, b) => TREE_COLLATOR.compare(a.name || "", b.name || ""));
}

/** A record created offline has no server fields yet — `status` among them.
 * Treat "no status" as active rather than filtering it out; see the
 * optimistic-write path in the tree form handler.
 */
function isActiveRecord(record) {
  return record.status === undefined || record.status === "active";
}

const HEALTH_PILLS = {
  healthy: { cls: "pill--health-healthy", label: "Healthy" },
  unhealthy: { cls: "pill--health-attention", label: "Needs attention" },
  dead: { cls: "pill--health-attention", label: "Dead" },
  unknown: { cls: "pill--health-unknown", label: "Not yet assessed" },
};

/** Health is shown as a text label inside the pill, never colour alone —
 * a red/green ring would be invisible to a colourblind user in the field.
 */
function healthPill(health) {
  const pill = HEALTH_PILLS[health] || HEALTH_PILLS.unknown;
  return `<span class="pill ${pill.cls}">${pill.label}</span>`;
}

function setScreen(screen, extra = {}) {
  Object.assign(state, { screen, error: null }, extra);
  render();
}

function showError(err) {
  state.error = err && err.message ? err.message : String(err);
  render();
}

// --- Sync status chip ------------------------------------------------

/** Returns the chip element markup INCLUDING its id. updateSyncChip()
 * swaps this in via outerHTML, so the id has to be part of what's returned
 * — the previous version emitted an id-less div, which meant the very
 * first update orphaned the element and the chip silently stopped
 * reflecting reality for the rest of the session. Offline now takes
 * precedence over "syncing" in the label too: a sync cannot be in progress
 * while the device is offline, and claiming otherwise is the one lie this
 * component must never tell.
 */
function renderSyncChip(status, outboxCount) {
  const offline = !navigator.onLine;
  const label = offline
    ? `Offline${outboxCount ? ` — ${outboxCount} queued` : " — no connection"}`
    : status === "syncing"
    ? "Syncing…"
    : outboxCount
    ? `Online — ${outboxCount} queued`
    : "Online — synced";
  const cls = offline ? "offline" : status === "syncing" ? "syncing" : "online";
  return `<div id="sync-chip" class="sync-chip sync-chip--${cls}" role="status" aria-live="polite">${label}</div>`;
}

async function updateSyncChip() {
  const chip = document.getElementById("sync-chip");
  if (!chip) return;
  const outbox = await window.PalmVisionDB.getOutboxEntries();
  chip.outerHTML = renderSyncChip(window._lastSyncStatus || "idle", outbox.length);
}

window.PalmVisionSync.onSyncStatusChange((status) => {
  window._lastSyncStatus = status;
  updateSyncChip();
});

// --- Screens -----------------------------------------------------------

function loginScreen() {
  return `
    <div class="screen screen--login">
      <h1>Palm Vision</h1>
      <p class="muted">Field access — works offline once logged in.</p>
      <form id="login-form">
        <label>Username<input name="username" autocomplete="username" required value="${escapeAttr(state.loginUsername)}"></label>
        <label>Password<input name="password" type="password" autocomplete="current-password" required></label>
        <button type="submit">Log in</button>
      </form>
      ${state.error ? `<p class="error" role="alert">${escapeHtml(state.error)}</p>` : ""}
    </div>
  `;
}

async function blocksScreen() {
  const blocks = await window.PalmVisionDB.getAll("farm_blocks");
  const here = state.location ? window.PalmVisionGeo.findContainingBlock(state.location.lat, state.location.lon, blocks) : null;

  const items = blocks
    .map((block) => {
      const isHere = here && here.id === block.id;
      const meta = [block.commodity, block.year ? `planted ${block.year}` : null].filter(Boolean).join(" · ");
      return `
        <li class="block-item ${isHere ? "block-item--here" : ""}" data-block-id="${escapeAttr(block.id)}"
            role="button" tabindex="0">
          <div class="block-item__name">${escapeHtml(block.name)}</div>
          <div class="block-item__meta">${escapeHtml(meta)}</div>
          ${isHere ? '<div class="pill pill--here">You are here</div>' : ""}
          ${block.overlap_warning ? `<div class="pill pill--warning">${escapeHtml(block.overlap_warning)}</div>` : ""}
        </li>`;
    })
    .join("");

  return `
    <div class="screen screen--blocks">
      <header class="screen-header">
        <h1>Your blocks</h1>
        <button id="logout-btn" class="btn-text btn-text--end">Log out</button>
      </header>
      ${
        blocks.length === 0
          ? `<div class="empty-state">
               <span class="empty-state__title">No blocks downloaded yet</span>
               <p class="empty-state__hint">Connect to the network and this list will fill in automatically. You can keep working offline once it has.</p>
             </div>`
          : `<ul class="block-list">${items}</ul>`
      }
    </div>
  `;
}

async function blockScreen() {
  const block = await window.PalmVisionDB.getOne("farm_blocks", state.selectedBlockId);
  if (!block) {
    return `
      <div class="screen screen--block">
        <header class="screen-header">
          <button id="back-btn" class="btn-text btn-text--back">&larr; Blocks</button>
          <h1>Block unavailable</h1>
        </header>
        <div class="empty-state">
          <span class="empty-state__title">This block isn't downloaded</span>
          <p class="empty-state__hint">It hasn't been cached to this device yet. Reconnect to the network and it will sync automatically.</p>
        </div>
      </div>`;
  }

  const allTrees = await window.PalmVisionDB.getAll("trees");
  // isActiveRecord(), not `status === "active"` — a tree created offline has
  // no server-assigned status yet, and the strict check made the record the
  // field worker had just added disappear from this list while it sat in the
  // outbox. They would then add it again.
  const trees = sortTrees(allTrees.filter((t) => t.farm_block === block.id && isActiveRecord(t)));

  const proximity = state.location ? window.PalmVisionGeo.proximityCandidate(state.location.lat, state.location.lon, trees) : null;

  const treeItems = trees
    .map((tree) => {
      const pending = tree.status === undefined;
      return `
      <li class="tree-item ${pending ? "tree-item--pending" : ""}" data-tree-id="${escapeAttr(tree.id)}"
          role="button" tabindex="0">
        <div class="tree-item__row">
          <span class="tree-item__name">${escapeHtml(tree.name || "Unnamed tree")}</span>
          ${pending ? '<span class="pill pill--queued">Queued</span>' : ""}
        </div>
        <div class="tree-item__meta">${healthPill(tree.current_health)}</div>
      </li>`;
    })
    .join("");

  return `
    <div class="screen screen--block">
      <header class="screen-header">
        <button id="back-btn" class="btn-text btn-text--back">&larr; Blocks</button>
        <h1>${escapeHtml(block.name)}</h1>
      </header>

      ${
        proximity
          ? `<div class="proximity-prompt" data-tree-id="${escapeAttr(proximity.tree.id)}">
               <p class="proximity-prompt__text">You are ${Math.round(proximity.distanceM)}m from <strong>${escapeHtml(proximity.tree.name || "a tree")}</strong>. Log a harvest against it?</p>
               <button id="proximity-confirm-btn">Log harvest here</button>
             </div>`
          : ""
      }

      <div class="action-row">
        <button id="add-tree-btn">+ Add tree</button>
        <button id="log-harvest-btn">Log harvest</button>
        <button id="log-fertilizer-btn">Log fertilizer</button>
      </div>

      <h2>Trees (${trees.length})</h2>
      ${
        trees.length === 0
          ? `<div class="empty-state">
               <span class="empty-state__title">No trees in this block yet</span>
               <p class="empty-state__hint">Use “+ Add tree” above to record the first one. It will save on this device straight away, even with no signal.</p>
             </div>`
          : `<ul class="tree-list">${treeItems}</ul>`
      }
    </div>
  `;
}

/** Tree detail. Previously the tree rows carried `cursor: pointer` and a
 * data-tree-id but had no click handler at all, so every row in the list
 * was a dead affordance — and the only route to tree-level logging was the
 * GPS proximity prompt, which only appears when you happen to be standing
 * within 15m of a tree. This is that missing destination.
 */
async function treeDetailScreen() {
  const tree = await window.PalmVisionDB.getOne("trees", state.selectedTreeId);
  if (!tree) {
    return `
      <div class="screen screen--tree">
        <header class="screen-header">
          <button id="back-btn" class="btn-text btn-text--back">&larr; Block</button>
          <h1>Tree unavailable</h1>
        </header>
        <div class="empty-state">
          <span class="empty-state__title">This tree isn't on this device</span>
          <p class="empty-state__hint">Reconnect to the network to download it.</p>
        </div>
      </div>`;
  }

  const pending = tree.status === undefined;
  const coords = tree.location && tree.location.coordinates
    ? `${tree.location.coordinates[1].toFixed(6)}, ${tree.location.coordinates[0].toFixed(6)}`
    : "Not recorded";

  return `
    <div class="screen screen--tree">
      <header class="screen-header">
        <button id="back-btn" class="btn-text btn-text--back">&larr; Block</button>
        <h1>${escapeHtml(tree.name || "Unnamed tree")}</h1>
      </header>

      ${pending ? '<div class="proximity-prompt"><p class="proximity-prompt__text">This tree was recorded offline and is still waiting to sync. It is saved safely on this device.</p></div>' : ""}

      <p class="tree-item__meta">Health: ${healthPill(tree.current_health)}</p>
      <p class="muted">Location: ${escapeHtml(coords)}</p>

      <div class="action-row">
        <button id="tree-harvest-btn">Log harvest</button>
        <button id="tree-fertilizer-btn">Log fertilizer</button>
      </div>
    </div>
  `;
}

function treeFormScreen() {
  return `
    <div class="screen screen--form">
      <header class="screen-header">
        <button id="back-btn" class="btn-text btn-text--back">&larr; Block</button>
        <h1>Add tree</h1>
      </header>
      <form id="tree-form">
        <label>Name (optional)<input name="name"></label>
        ${
          state.location
            ? `<p class="muted">Location: ${state.location.lat.toFixed(6)}, ${state.location.lon.toFixed(6)}</p>`
            : `<p class="muted" role="status" aria-live="polite">Waiting for GPS… hold still with a clear view of the sky.</p>`
        }
        <button type="submit" ${state.location ? "" : "disabled"}>Save tree</button>
      </form>
      ${state.error ? `<p class="error" role="alert">${escapeHtml(state.error)}</p>` : ""}
    </div>
  `;
}

/** Shared by both harvest and fertilizer logging — `kind` is 'harvest' or
 * 'fertilizer'. Supports both block-level (manual total) and, when a tree
 * is pre-selected via the proximity prompt, tree-level entry — see
 * docs/05-offline-sync.md § Yield granularity.
 */
function actualFormScreen(kind) {
  const label = kind === "harvest" ? "Harvest" : "Fertilizer";
  const treeMode = Boolean(state.selectedTreeId);
  return `
    <div class="screen screen--form">
      <header class="screen-header">
        <button id="back-btn" class="btn-text btn-text--back">&larr; Block</button>
        <h1>Log ${label.toLowerCase()}${treeMode ? " (tree)" : " (block total)"}</h1>
      </header>
      <form id="actual-form" data-kind="${kind}">
        <label>Date<input name="date" type="date" required value="${new Date().toISOString().slice(0, 10)}"></label>
        <label>Quantity (${kind === "harvest" ? "bunches" : "kg"})<input name="quantity" type="number" step="0.01" min="0" inputmode="decimal" required></label>
        ${kind === "harvest" ? '<label>Weight (kg, optional)<input name="weight_kg" type="number" step="0.01" min="0" inputmode="decimal"></label>' : ""}
        <button type="submit">Save</button>
      </form>
      ${state.error ? `<p class="error" role="alert">${escapeHtml(state.error)}</p>` : ""}
    </div>
  `;
}

// --- Render dispatch -----------------------------------------------------

async function render() {
  const el = root();
  const outbox = await window.PalmVisionDB.getOutboxEntries();
  let body;
  switch (state.screen) {
    case "login":
      body = loginScreen();
      break;
    case "blocks":
      body = await blocksScreen();
      break;
    case "block":
      body = await blockScreen();
      break;
    case "treeDetail":
      body = await treeDetailScreen();
      break;
    case "treeForm":
      body = treeFormScreen();
      break;
    case "harvestForm":
      body = actualFormScreen("harvest");
      break;
    case "fertilizerForm":
      body = actualFormScreen("fertilizer");
      break;
    default:
      body = `<div class="screen"><div class="empty-state"><span class="empty-state__title">Something went wrong</span><p class="empty-state__hint">This screen could not be displayed. Go back and try again.</p></div></div>`;
  }
  const chip = state.screen === "login" ? "" : renderSyncChip(window._lastSyncStatus || "idle", outbox.length);
  el.innerHTML = chip + body;
  wireScreenEvents();
}

// --- Event wiring ---------------------------------------------------------

function wireScreenEvents() {
  const loginForm = document.getElementById("login-form");
  if (loginForm) {
    loginForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(loginForm);
      state.loginUsername = fd.get("username") || "";
      try {
        await window.PalmVisionAPI.login(fd.get("username"), fd.get("password"));
        await window.PalmVisionSync.runSync();
        setScreen("blocks");
      } catch (err) {
        showError(err);
      }
    });
  }

  const logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) logoutBtn.addEventListener("click", async () => {
    await window.PalmVisionAPI.logout();
    setScreen("login");
  });

  // These rows are role="button" tabindex="0", so they must answer Enter and
  // Space as well as a tap — a pointer-only handler on a div/li is invisible
  // to keyboard and switch-control users.
  const activate = (el, handler) => {
    el.addEventListener("click", handler);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        handler();
      }
    });
  };

  document.querySelectorAll(".block-item").forEach((li) => {
    activate(li, () => setScreen("block", { selectedBlockId: li.dataset.blockId }));
  });

  // Previously absent entirely: .tree-item rows rendered with cursor:pointer
  // and a data-tree-id but nothing ever listened, so tapping a tree did
  // nothing at all.
  document.querySelectorAll(".tree-item").forEach((li) => {
    activate(li, () => setScreen("treeDetail", { selectedTreeId: li.dataset.treeId }));
  });

  const backBtn = document.getElementById("back-btn");
  if (backBtn) {
    backBtn.addEventListener("click", () => {
      if (state.screen === "block") setScreen("blocks");
      else if (state.screen === "treeDetail") setScreen("block", { selectedTreeId: null });
      else setScreen("block", { selectedTreeId: null });
    });
  }

  const addTreeBtn = document.getElementById("add-tree-btn");
  if (addTreeBtn) addTreeBtn.addEventListener("click", () => setScreen("treeForm"));

  const logHarvestBtn = document.getElementById("log-harvest-btn");
  if (logHarvestBtn) logHarvestBtn.addEventListener("click", () => setScreen("harvestForm", { selectedTreeId: null }));

  const logFertilizerBtn = document.getElementById("log-fertilizer-btn");
  if (logFertilizerBtn) logFertilizerBtn.addEventListener("click", () => setScreen("fertilizerForm", { selectedTreeId: null }));

  const treeHarvestBtn = document.getElementById("tree-harvest-btn");
  if (treeHarvestBtn) treeHarvestBtn.addEventListener("click", () => setScreen("harvestForm", { selectedTreeId: state.selectedTreeId }));

  const treeFertilizerBtn = document.getElementById("tree-fertilizer-btn");
  if (treeFertilizerBtn) treeFertilizerBtn.addEventListener("click", () => setScreen("fertilizerForm", { selectedTreeId: state.selectedTreeId }));

  const proximityBtn = document.getElementById("proximity-confirm-btn");
  if (proximityBtn) {
    const promptEl = document.querySelector(".proximity-prompt");
    proximityBtn.addEventListener("click", () => setScreen("harvestForm", { selectedTreeId: promptEl.dataset.treeId }));
  }

  const treeForm = document.getElementById("tree-form");
  if (treeForm) {
    treeForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(treeForm);
      const id = crypto.randomUUID();
      const payload = {
        id,
        farm_block: state.selectedBlockId,
        name: fd.get("name") || "",
        location: { type: "Point", coordinates: [state.location.lon, state.location.lat] },
      };
      try {
        const result = await window.PalmVisionAPI.queueableWrite("trees", "/api/trees/", "POST", payload);
        // Optimistic local write regardless of queued/sent — the field
        // worker needs to see it immediately either way.
        //
        // When the write was queued rather than accepted, `result.response`
        // is undefined and we store the request payload instead. That
        // payload has no server-assigned fields, so give it the defaults the
        // list rendering depends on: without `current_health` the health
        // pill reads as garbage, and the record is flagged as still-queued
        // by the *absence* of `status` (see isActiveRecord/tree-item--pending).
        const stored = result.response || { ...payload, current_health: "unknown" };
        await window.PalmVisionDB.putOne("trees", stored);
        setScreen("block", { selectedBlockId: state.selectedBlockId });
      } catch (err) {
        showError(err);
      }
    });
  }

  const actualForm = document.getElementById("actual-form");
  if (actualForm) {
    actualForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(actualForm);
      const kind = actualForm.dataset.kind;
      try {
        if (state.selectedTreeId) {
          await submitTreeLevelActual(kind, fd);
        } else {
          await submitBlockLevelActual(kind, fd);
        }
        setScreen("block", { selectedBlockId: state.selectedBlockId, selectedTreeId: null });
      } catch (err) {
        showError(err);
      }
    });
  }
}

/** Block-level entry needs an open plan (Harvest/Fertilizer) to attach the
 * actual to. This finds the most recent one for the block rather than
 * making the user pick — plan selection UI is a reasonable Phase 4
 * follow-up, not required for the offline-queue mechanics this phase is
 * about proving out.
 */
async function findLatestPlan(kind) {
  const store = kind === "harvest" ? "harvests" : "fertilizers";
  const plans = (await window.PalmVisionDB.getAll(store)).filter((p) => p.farm_block === state.selectedBlockId);
  plans.sort((a, b) => (a.assessment_date < b.assessment_date ? 1 : -1));
  if (!plans.length) throw new Error(`No ${kind} plan found for this block — create one from the office dashboard first.`);
  return plans[0];
}

async function submitBlockLevelActual(kind, fd) {
  const plan = await findLatestPlan(kind);
  const resource = kind === "harvest" ? "actual_harvests" : "actual_fertilizers";
  const path = kind === "harvest" ? "/api/actual-harvests/" : "/api/actual-fertilizers/";
  const payload = {
    id: crypto.randomUUID(),
    [kind]: plan.id,
    date: fd.get("date"),
    quantity: Number(fd.get("quantity")),
  };
  const result = await window.PalmVisionAPI.queueableWrite(resource, path, "POST", payload);
  await window.PalmVisionDB.putOne(resource, result.response || payload);
}

async function submitTreeLevelActual(kind, fd) {
  const plan = await findLatestPlan(kind);
  // Tree-level entry is itself nested under an "actual" for the plan — one
  // actual per field visit, per docs/03-data-model.md — so create (or
  // reuse today's) actual first, then the per-tree row.
  const actualResource = kind === "harvest" ? "actual_harvests" : "actual_fertilizers";
  const actualPath = kind === "harvest" ? "/api/actual-harvests/" : "/api/actual-fertilizers/";
  const actualPayload = { id: crypto.randomUUID(), [kind]: plan.id, date: fd.get("date") };
  const actualResult = await window.PalmVisionAPI.queueableWrite(actualResource, actualPath, "POST", actualPayload);
  const actual = actualResult.response || actualPayload;
  await window.PalmVisionDB.putOne(actualResource, actual);

  const treeResource = kind === "harvest" ? "harvest_trees" : "fertilizer_trees";
  const treePath = kind === "harvest" ? "/api/harvest-trees/" : "/api/fertilizer-trees/";
  const treePayload = {
    id: crypto.randomUUID(),
    [kind === "harvest" ? "actual_harvest" : "actual_fertilizer"]: actual.id,
    tree: state.selectedTreeId,
    quantity: Number(fd.get("quantity")),
    ...(kind === "harvest" ? { weight_kg: fd.get("weight_kg") ? Number(fd.get("weight_kg")) : null } : {}),
    ...(kind === "harvest" && state.location
      ? { recorded_location: { type: "Point", coordinates: [state.location.lon, state.location.lat] } }
      : {}),
  };
  const treeResult = await window.PalmVisionAPI.queueableWrite(treeResource, treePath, "POST", treePayload);
  await window.PalmVisionDB.putOne(treeResource, treeResult.response || treePayload);
}

// --- Boot ------------------------------------------------------------------

async function boot() {
  if ("serviceWorker" in navigator) {
    try {
      await navigator.serviceWorker.register("/service-worker.js");
    } catch (err) {
      console.error("Service worker registration failed", err);
    }
  }

  state.watchId = window.PalmVisionGeo.watchLocation(
    (loc) => {
      state.location = loc;
      if (["blocks", "block"].includes(state.screen)) render();
    },
    (err) => console.warn("Geolocation unavailable", err)
  );

  const { access } = await window.PalmVisionAPI.getTokens();
  if (access) {
    setScreen("blocks");
    window.PalmVisionSync.runSync().catch((err) => console.error("Initial sync failed", err));
  } else {
    setScreen("login");
  }

  // Periodic sync attempt while the app stays open — a fallback for
  // devices/browsers without working Background Sync support (notably iOS
  // Safari, which doesn't implement the Background Sync API at all).
  setInterval(() => {
    if (navigator.onLine) window.PalmVisionSync.runSync().catch((err) => console.error("Periodic sync failed", err));
  }, 60000);
}

document.addEventListener("DOMContentLoaded", boot);
