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
};

const root = () => document.getElementById("app");

function setScreen(screen, extra = {}) {
  Object.assign(state, { screen, error: null }, extra);
  render();
}

function showError(err) {
  state.error = err && err.message ? err.message : String(err);
  render();
}

// --- Sync status chip ------------------------------------------------

function renderSyncChip(status, outboxCount) {
  const label =
    status === "syncing"
      ? "Syncing…"
      : !navigator.onLine
      ? `Offline${outboxCount ? ` — ${outboxCount} queued` : ""}`
      : outboxCount
      ? `Online — ${outboxCount} queued`
      : "Online — synced";
  const cls = !navigator.onLine ? "offline" : status === "syncing" ? "syncing" : "online";
  return `<div class="sync-chip sync-chip--${cls}">${label}</div>`;
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
        <label>Username<input name="username" autocomplete="username" required></label>
        <label>Password<input name="password" type="password" autocomplete="current-password" required></label>
        <button type="submit">Log in</button>
      </form>
      ${state.error ? `<p class="error">${state.error}</p>` : ""}
    </div>
  `;
}

async function blocksScreen() {
  const blocks = await window.PalmVisionDB.getAll("farm_blocks");
  const here = state.location ? window.PalmVisionGeo.findContainingBlock(state.location.lat, state.location.lon, blocks) : null;

  const items = blocks
    .map((block) => {
      const isHere = here && here.id === block.id;
      return `
        <li class="block-item ${isHere ? "block-item--here" : ""}" data-block-id="${block.id}">
          <div class="block-item__name">${block.name}</div>
          <div class="block-item__meta">${block.commodity || ""} ${block.year ? `· planted ${block.year}` : ""}</div>
          ${isHere ? '<div class="pill pill--accent">You are here</div>' : ""}
          ${block.overlap_warning ? `<div class="pill pill--warning">${block.overlap_warning}</div>` : ""}
        </li>`;
    })
    .join("");

  return `
    <div class="screen screen--blocks">
      <header class="screen-header">
        <h1>Your blocks</h1>
        <button id="logout-btn" class="btn-text">Log out</button>
      </header>
      ${blocks.length === 0 ? '<p class="muted">No blocks pre-cached yet. Pull to sync while online.</p>' : `<ul class="block-list">${items}</ul>`}
    </div>
  `;
}

async function blockScreen() {
  const block = await window.PalmVisionDB.getOne("farm_blocks", state.selectedBlockId);
  if (!block) return `<p class="error">Block not found in local cache.</p>`;

  const allTrees = await window.PalmVisionDB.getAll("trees");
  const trees = allTrees.filter((t) => t.farm_block === block.id && t.status === "active");

  const proximity = state.location ? window.PalmVisionGeo.proximityCandidate(state.location.lat, state.location.lon, trees) : null;

  const treeItems = trees
    .map(
      (tree) => `
      <li class="tree-item" data-tree-id="${tree.id}">
        <div class="tree-item__name">${tree.name || "Unnamed tree"}</div>
        <div class="tree-item__meta">${tree.current_health}</div>
      </li>`
    )
    .join("");

  return `
    <div class="screen screen--block">
      <header class="screen-header">
        <button id="back-btn" class="btn-text">&larr; Blocks</button>
        <h1>${block.name}</h1>
      </header>

      ${
        proximity
          ? `<div class="proximity-prompt" data-tree-id="${proximity.tree.id}">
               Near <strong>${proximity.tree.name || "a tree"}</strong> (${Math.round(proximity.distanceM)}m) — log yield?
               <button id="proximity-confirm-btn">Log here</button>
             </div>`
          : ""
      }

      <div class="action-row">
        <button id="add-tree-btn">+ Add tree</button>
        <button id="log-harvest-btn">Log harvest</button>
        <button id="log-fertilizer-btn">Log fertilizer</button>
      </div>

      <h2>Trees (${trees.length})</h2>
      <ul class="tree-list">${treeItems || '<li class="muted">No trees cached for this block yet.</li>'}</ul>
    </div>
  `;
}

function treeFormScreen() {
  return `
    <div class="screen screen--form">
      <header class="screen-header">
        <button id="back-btn" class="btn-text">&larr; Block</button>
        <h1>Add tree</h1>
      </header>
      <form id="tree-form">
        <label>Name (optional)<input name="name"></label>
        <p class="muted">Location: ${
          state.location ? `${state.location.lat.toFixed(6)}, ${state.location.lon.toFixed(6)}` : "waiting for GPS…"
        }</p>
        <button type="submit" ${state.location ? "" : "disabled"}>Save tree</button>
      </form>
      ${state.error ? `<p class="error">${state.error}</p>` : ""}
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
        <button id="back-btn" class="btn-text">&larr; Block</button>
        <h1>Log ${label.toLowerCase()}${treeMode ? " (tree)" : " (block total)"}</h1>
      </header>
      <form id="actual-form" data-kind="${kind}">
        <label>Date<input name="date" type="date" required value="${new Date().toISOString().slice(0, 10)}"></label>
        <label>Quantity<input name="quantity" type="number" step="0.01" inputmode="decimal" required></label>
        ${kind === "harvest" ? '<label>Weight (kg, optional)<input name="weight_kg" type="number" step="0.01" inputmode="decimal"></label>' : ""}
        <button type="submit">Save</button>
      </form>
      ${state.error ? `<p class="error">${state.error}</p>` : ""}
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
      body = `<p>Unknown screen</p>`;
  }
  const chip = state.screen === "login" ? "" : `<div id="sync-chip">${renderSyncChip(window._lastSyncStatus || "idle", outbox.length)}</div>`;
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

  document.querySelectorAll(".block-item").forEach((li) => {
    li.addEventListener("click", () => setScreen("block", { selectedBlockId: li.dataset.blockId }));
  });

  const backBtn = document.getElementById("back-btn");
  if (backBtn) {
    backBtn.addEventListener("click", () => {
      if (state.screen === "block") setScreen("blocks");
      else setScreen("block", { selectedTreeId: null });
    });
  }

  const addTreeBtn = document.getElementById("add-tree-btn");
  if (addTreeBtn) addTreeBtn.addEventListener("click", () => setScreen("treeForm"));

  const logHarvestBtn = document.getElementById("log-harvest-btn");
  if (logHarvestBtn) logHarvestBtn.addEventListener("click", () => setScreen("harvestForm", { selectedTreeId: null }));

  const logFertilizerBtn = document.getElementById("log-fertilizer-btn");
  if (logFertilizerBtn) logFertilizerBtn.addEventListener("click", () => setScreen("fertilizerForm", { selectedTreeId: null }));

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
        await window.PalmVisionDB.putOne("trees", result.response || payload);
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
