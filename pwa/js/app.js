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
  // Survives the re-render showError() triggers. Without it the login screen
  // came back with both fields blank after a bad password, forcing a gloved
  // user to retype their username every attempt.
  loginUsername: "",
  // Which mark-done banner ("harvest" | "fertilizer" | null) is currently
  // expanded into its inline quantity form on the tree detail screen — see
  // markBanner()'s docstring. Reset whenever a different tree is opened
  // (setScreen("treeDetail", ...) below already blows away selectedTreeId,
  // so tying this reset to the same call keeps it simple).
  quickMarkOpenKind: null,
  // Tree ids currently tapped-selected on the block map (blockMapScreen()
  // below) — an array, not a Set, so it survives untouched through the
  // plain-object state this app already uses everywhere else (no special
  // serialization needed anywhere state gets spread/copied). Reset to []
  // whenever the block map screen is (re)entered and after a bulk action
  // completes, same lifecycle as quickMarkOpenKind above.
  blockMapSelectedTreeIds: [],
  // "marked" | "all" — which trees blockScreen()'s list shows. Defaults to
  // "marked": reported directly, opening a block with its full tree count
  // (a block can hold over a thousand trees) read as an unsettling wall of
  // mostly-irrelevant rows, when in the field what actually matters day to
  // day is "which trees still need something." Not a hard filter — a
  // "Show all trees" toggle switches it back for the times a field worker
  // genuinely needs to find/edit a tree that isn't currently marked. Reset
  // to "marked" whenever a *different* block is freshly opened (from the
  // blocks list or the farm map), but left alone when just returning to
  // the same block (e.g. from a tree's detail screen), so an explicit
  // "Show all" choice survives that round trip.
  blockTreeFilter: "marked",
  // Search text + page size for the block tree list — a 1,000-tree block
  // rendered as a ~97,000px-tall list with no way to find one tree.
  blockTreeQuery: "",
  blockTreeLimit: 100,
  // {x,y,w,h} SVG viewBox for blockMapScreen()'s zoom/pan; null = fit whole block.
  blockMapView: null,
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

/** Every value that comes from the server, IndexedDB, an error message or
 * the user must go through this before it's interpolated into markup —
 * screens are HTML strings assigned to innerHTML (render()). Tree names in
 * particular are free text a PIC Block user can edit, and they render for
 * every other user of that block, Managers included, whose login tokens
 * live in this same origin's storage — so an unescaped name was a stored
 * XSS path, not just a rendering glitch. Safe in both text and quoted
 * attribute contexts.
 */
function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Health values as people read them — the tree list used to show the raw
 * stored value ("yellow"). Same wording as the dashboard map legend.
 */
const HEALTH_LABELS = { healthy: "Healthy", yellow: "Yellowing", unknown: "Unknown health" };

function healthLabel(value) {
  return HEALTH_LABELS[value] || value || HEALTH_LABELS.unknown;
}

/** The server's label (name, or block + number like B4-0123) — every tree
 * used to read "Unnamed tree". A tree added on this device offline has no
 * number until it syncs.
 */
function treeLabel(tree) {
  if (tree.label || tree.name) return tree.label || tree.name;
  // created_at comes from the server, so a record without it was added on
  // this device and hasn't synced; one with it just predates tree numbers
  // and picks its label up on the next sync.
  return tree.created_at ? "Unnamed tree" : "New tree (not synced yet)";
}

/** A record created offline has no server-assigned fields yet, `status`
 * among them. Treat "no status" as active rather than filtering it out.
 */
function isActiveRecord(record) {
  return record.status === undefined || record.status === "active";
}

/** Trees render in the order someone walking the block expects, not
 * IndexedDB insertion order (which put A2, A4, A1, A3 on screen). Numeric
 * -aware so "Tree 10" sorts after "Tree 9".
 */
const TREE_COLLATOR = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

// By label, so unnamed trees come out in block-number order (B4-0001,
// B4-0002, …), which follows the walk through the block.
function sortTrees(trees) {
  return [...trees].sort((a, b) => TREE_COLLATOR.compare(treeLabel(a), treeLabel(b)));
}

/** Whether a Manager has stage-1 flagged this tree as needing harvest or
 * fertilizer (Tree.harvest_marked_at/fertilizer_marked_at's help_text,
 * apps/trees/models.py) — the field app's own to-do list for this block.
 */
function isMarkedTree(tree) {
  return Boolean(tree.harvest_marked_at || tree.fertilizer_marked_at);
}

/** Marked trees first, alphabetical within each group — requested
 * directly: a block's to-do trees were easy to miss buried alphabetically
 * among however many other trees the block has, instead of standing out
 * at the top of the list the way the attention banner above them already
 * implies they should. A plain .sort() on an already-alphabetized array
 * (sortTrees() above) is stable in every engine this PWA targets, so this
 * only reorders the two groups relative to each other, never scrambling
 * names within either one.
 */
function sortTreesMarkedFirst(trees) {
  return sortTrees(trees).sort((a, b) => Number(isMarkedTree(b)) - Number(isMarkedTree(a)));
}

// --- Sync status chip ------------------------------------------------

/** Emits the chip INCLUDING its id, because updateSyncChip() swaps this in
 * via outerHTML. The previous version returned an id-less div wrapped by a
 * separate <div id="sync-chip">, so the very first update replaced the
 * element carrying the id and every later getElementById returned null —
 * the chip then silently stopped updating for the rest of the session.
 * Reproduced in a browser: forcing syncing→idle left it reading "Syncing…".
 *
 * Offline also takes precedence over "syncing" in the label: a sync cannot
 * be in progress with no connection, and this is the one component that
 * must never misreport that.
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

/** A more prominent, opt-in-only banner — the always-visible sync-chip
 * above already satisfies docs/09-design-system.md's "never buried in a
 * menu" rule for ambient status, but a one-line colored chip at the very
 * top is easy to stop noticing after the first few times. This banner only
 * ever renders when there's actually something queued (empty string
 * otherwise — never shown "just in case"), states the count in plain
 * language, and gives a manual "Sync now" affordance rather than only
 * relying on the automatic reconnect/foreground/interval triggers in
 * sync.js — a field user who's back in signal range but hasn't triggered
 * any of those events yet (e.g. the tab's been open and idle) shouldn't
 * have to guess why their queue isn't clearing.
 */
/** Items the server rejected (status "failed") are split out: they never
 * retry on their own and "Sync now" skips them, so counting them as
 * "waiting to sync" left them there forever with no reason given —
 * docs/05-offline-sync.md step 5 says failures must surface, never
 * silently. They get their own line and a Review screen (syncIssuesScreen()).
 */
function splitOutbox(entries) {
  return {
    pending: entries.filter((e) => e.status !== "failed").length,
    failed: entries.filter((e) => e.status === "failed").length,
  };
}

function renderUnsyncedBanner(entries) {
  const { pending, failed } = splitOutbox(entries);
  if (!pending && !failed) return "";
  const pendingLine = pending
    ? `<div class="unsynced-banner__row">
         <span>${pending} ${pending === 1 ? "item" : "items"} waiting to sync${navigator.onLine ? "" : " — will send automatically once you're back online"}.</span>
         <button type="button" id="sync-now-btn" ${navigator.onLine ? "" : "disabled"}>Sync now</button>
       </div>`
    : "";
  const failedLine = failed
    ? `<div class="unsynced-banner__row unsynced-banner__row--failed" role="alert">
         <span>${failed} ${failed === 1 ? "item" : "items"} couldn't be saved.</span>
         <button type="button" id="review-sync-issues-btn">Review</button>
       </div>`
    : "";
  return `<div class="unsynced-banner" id="unsynced-banner">${pendingLine}${failedLine}</div>`;
}

async function updateSyncChip() {
  const chip = document.getElementById("sync-chip");
  const outbox = await window.PalmVisionDB.getOutboxEntries();
  if (chip) chip.outerHTML = renderSyncChip(window._lastSyncStatus || "idle", splitOutbox(outbox).pending);

  const banner = document.getElementById("unsynced-banner");
  const newBannerHtml = renderUnsyncedBanner(outbox);
  if (banner) {
    if (newBannerHtml) banner.outerHTML = newBannerHtml;
    else banner.remove(); // queue just cleared — don't leave a stale "0 items" banner behind
  } else if (newBannerHtml && chip) {
    chip.insertAdjacentHTML("afterend", newBannerHtml);
  }
  wireSyncNowButton();
}

function wireSyncNowButton() {
  const reviewBtn = document.getElementById("review-sync-issues-btn");
  if (reviewBtn) reviewBtn.addEventListener("click", () => setScreen("syncIssues"));
  const btn = document.getElementById("sync-now-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Syncing…";
    try {
      await window.PalmVisionSync.runSync();
    } catch (err) {
      console.error("Manual sync failed", err);
    } finally {
      await render(); // rebuild chip/banner/screen against whatever the outbox looks like now
    }
  });
}

window.PalmVisionSync.onSyncStatusChange((status) => {
  window._lastSyncStatus = status;
  updateSyncChip();
  // A finished sync may have brought new blocks/trees/marks: redraw the
  // read-only list screens (never a form, which would lose typed input).
  if (status === "idle" && ["blocks", "block", "map"].includes(state.screen)) render();
});

// --- Screens -----------------------------------------------------------

function loginScreen() {
  return `
    <div class="screen screen--login">
      <h1>Palm Vision</h1>
      <p class="muted">Field access — works offline once logged in.</p>
      <form id="login-form">
        <label>Username<input name="username" autocomplete="username" required value="${escapeHtml(state.loginUsername)}"></label>
        <label>Password<input name="password" type="password" autocomplete="current-password" required></label>
        <button type="submit">Log in</button>
      </form>
      ${state.error ? `<p class="error" role="alert">${escapeHtml(state.error)}</p>` : ""}
    </div>
  `;
}

async function blocksScreen() {
  const blocks = (await window.PalmVisionDB.getAll("farm_blocks")).sort((a, b) =>
    TREE_COLLATOR.compare(a.name || "", b.name || "")
  );
  const here = state.location ? window.PalmVisionGeo.findContainingBlock(state.location.lat, state.location.lon, blocks) : null;

  const items = blocks
    .map((block) => {
      const isHere = here && here.id === block.id;
      return `
        <li>
          <button type="button" class="block-item ${isHere ? "block-item--here" : ""}" data-block-id="${escapeHtml(block.id)}">
            <span class="block-item__name">${escapeHtml(block.name)}</span>
            <span class="block-item__meta">${escapeHtml(block.commodity || "")} ${block.year ? `· planted ${escapeHtml(block.year)}` : ""}</span>
            ${isHere ? '<span class="pill pill--accent">You are here</span>' : ""}
          </button>
        </li>`;
    })
    .join("");

  return `
    <div class="screen screen--blocks">
      <header class="screen-header">
        <h1>Your blocks</h1>
        <div class="screen-header__actions">
          <button id="map-btn" class="btn-text">Map</button>
          <button id="logout-btn" class="btn-text">Log out</button>
        </div>
      </header>
      ${
        blocks.length === 0
          ? `<div class="empty-state">
               <span class="empty-state__title">No blocks downloaded yet</span>
               <p class="empty-state__hint">Connect to the network and this list fills in automatically. You can keep working offline once it has.</p>
             </div>`
          : `<ul class="block-list">${items}</ul>`
      }
      <p class="muted resync-note">
        Something missing or out of date?
        <button type="button" id="redownload-btn" class="btn-text">Re-download all data</button>
      </p>
      ${state.error ? `<p class="error" role="alert">${escapeHtml(state.error)}</p>` : ""}
    </div>
  `;
}

/** Farm-level map — docs/10-user-journeys.md step 2: "Travel to the block
 * (offline). No signal needed — farm-level map shows live GPS position
 * against block boundaries." Deliberately a small, custom-drawn SVG of
 * block boundaries plus a live position dot, not a tile-based map — this
 * app has no offline basemap imagery to show (no internet-independent way
 * to ship real satellite/street tiles here), and cached IndexedDB
 * boundary/GPS data is exactly what the journey doc actually asks this
 * screen to show: "GPS position against block boundaries," not photoreal
 * imagery. Works fully offline once blocks are pre-cached, using nothing
 * but the browser's built-in Geolocation API (which itself needs no
 * network) and pwa/js/geo.js's existing pointInPolygon/haversine helpers —
 * no external library, no CDN, consistent with this app's local-first
 * stance everywhere else (see pwa/css/app.css's own header comment).
 */
function buildMapProjection(blocks, location, extraPoints = []) {
  const points = [];
  blocks.forEach((block) => {
    if (block.boundary && block.boundary.coordinates && block.boundary.coordinates[0]) {
      points.push(...block.boundary.coordinates[0]);
    }
  });
  if (location) points.push([location.lon, location.lat]);
  // Extra [lon, lat] points to fold into the bounding box alongside block
  // boundaries — used by blockMapScreen() below to make sure every tree
  // pin lands inside the view, not just the block outline itself (a tree
  // planted a little outside its nominal boundary, e.g. a GPS-orphaned
  // one — see Tree.is_location_orphaned() — would otherwise render off
  // the edge of the SVG or get silently clipped).
  points.push(...extraPoints);
  if (!points.length) return null;

  const lons = points.map((p) => p[0]);
  const lats = points.map((p) => p[1]);
  let minLon = Math.min(...lons);
  let maxLon = Math.max(...lons);
  let minLat = Math.min(...lats);
  let maxLat = Math.max(...lats);
  // A single-point or degenerately small extent (e.g. only one block, or a
  // GPS fix sitting exactly on a boundary) would otherwise divide by ~0 —
  // pad by 10% of the span, with a small absolute floor for a near-zero span.
  const padLon = (maxLon - minLon) * 0.1 || 0.0008;
  const padLat = (maxLat - minLat) * 0.1 || 0.0008;
  minLon -= padLon; maxLon += padLon; minLat -= padLat; maxLat += padLat;

  // cos(latitude) correction — at the equator (this project's fixture data
  // and, per docs/03-data-model.md, the plantations this is built for are
  // tropical) a degree of longitude and a degree of latitude are both
  // ~111km, but that stops being true moving away from the equator; this
  // keeps block shapes from looking horizontally stretched or squashed.
  const lonScale = Math.cos((((minLat + maxLat) / 2) * Math.PI) / 180) || 1;
  const spanLon = (maxLon - minLon) * lonScale;
  const spanLat = maxLat - minLat;
  const PIXELS_PER_DEGREE = 90000; // tuned so a typical block (tens of meters across) renders at a legible size
  const scale = PIXELS_PER_DEGREE;

  return {
    viewWidth: Math.max(spanLon * scale, 40),
    viewHeight: Math.max(spanLat * scale, 40),
    metersToPixels: (m) => (m / 111320) * scale,
    project([lon, lat]) {
      const x = (lon - minLon) * lonScale * scale;
      const y = (maxLat - lat) * scale; // SVG y grows downward; lat grows northward, so flip
      return [x, y];
    },
  };
}

async function mapScreen() {
  const blocks = await window.PalmVisionDB.getAll("farm_blocks");
  const projection = buildMapProjection(blocks, state.location);
  const here = state.location ? window.PalmVisionGeo.findContainingBlock(state.location.lat, state.location.lon, blocks) : null;

  let svgContent = "";
  if (projection) {
    const blockShapes = blocks
      .filter((block) => block.boundary && block.boundary.coordinates)
      .map((block) => {
        const ring = block.boundary.coordinates[0];
        const pathPoints = ring.map((coord) => projection.project(coord).join(",")).join(" L ");
        const isHere = here && here.id === block.id;
        const [labelX, labelY] = projection.project(ring[0]);
        return `
          <g class="map-block" data-block-id="${escapeHtml(block.id)}">
            <path class="map-block-shape ${isHere ? "map-block-shape--here" : ""}" d="M ${pathPoints} Z"></path>
            <text class="map-block-label" x="${labelX}" y="${labelY - 4}">${escapeHtml(block.name)}</text>
          </g>`;
      })
      .join("");

    let positionMarker = "";
    if (state.location) {
      const [x, y] = projection.project([state.location.lon, state.location.lat]);
      const accuracyRadius = state.location.accuracyM ? projection.metersToPixels(state.location.accuracyM) : 0;
      positionMarker = `
        ${accuracyRadius > 0 ? `<circle class="map-accuracy-ring" cx="${x}" cy="${y}" r="${accuracyRadius}"></circle>` : ""}
        <circle class="map-user-dot" cx="${x}" cy="${y}" r="8"></circle>`;
    }

    svgContent = `
      <svg class="map-svg" viewBox="0 0 ${projection.viewWidth} ${projection.viewHeight}" preserveAspectRatio="xMidYMid meet">
        ${blockShapes}
        ${positionMarker}
      </svg>`;
  }

  return `
    <div class="screen screen--map">
      <header class="screen-header">
        <button id="back-btn" class="btn-text btn-text--back">&larr; Blocks</button>
        <h1>Farm map</h1>
      </header>
      ${
        here
          ? `<p class="pill pill--accent">You are in: ${escapeHtml(here.name)}</p>`
          : state.location
          ? `<p class="muted">You're not currently inside a mapped block boundary.</p>`
          : state.locationError
          ? `<p class="pill pill--warning">${escapeHtml(state.locationError)}</p>`
          : `<p class="muted">Waiting for GPS…</p>`
      }
      ${
        svgContent ||
        `<div class="empty-state">
           <span class="empty-state__title">Nothing to draw yet</span>
           <p class="empty-state__hint">Block boundaries haven't been downloaded to this device. Connect to the network to load the map.</p>
         </div>`
      }
      <p class="muted map-legend-note">Boundaries only — no satellite/street imagery is available offline. Tap a block to open it.</p>
    </div>
  `;
}

async function blockScreen() {
  const block = await window.PalmVisionDB.getOne("farm_blocks", state.selectedBlockId);
  if (!block) return `<p class="error">Block not found in local cache.</p>`;

  const allTrees = await window.PalmVisionDB.getAll("trees");
  // isActiveRecord(), not `status === "active"`: a tree created offline is
  // stored from the request payload, which carries no server-assigned
  // `status`, so the strict comparison hid the record the field worker had
  // just added while it sat in the outbox. Confirmed in a browser — the
  // list read "Trees (4)" with the new tree missing. They would then add it
  // again, duplicating it on sync.
  const trees = sortTreesMarkedFirst(allTrees.filter((t) => t.farm_block === block.id && isActiveRecord(t)));

  const proximity = state.location ? window.PalmVisionGeo.proximityCandidates(state.location.lat, state.location.lon, trees) : [];

  // Stage-1 "needs harvest/fertilizer" flags a Manager set from the office
  // dashboard (see Tree.harvest_marked_at's help_text, apps/trees/models.py)
  // — surfaced here as the PIC Block field app's to-do list for this block.
  // Nothing new to fetch: harvest_marked_at/fertilizer_marked_at already
  // arrive on every synced tree record (TreeSerializer), so this is pure
  // client-side filtering of data already pulled.
  const needsHarvest = trees.filter((t) => t.harvest_marked_at);
  const needsFertilizer = trees.filter((t) => t.fertilizer_marked_at);
  const attentionBanner =
    needsHarvest.length || needsFertilizer.length
      ? `<div class="attention-banner">
          ${needsHarvest.length ? `<span class="pill pill--accent">${needsHarvest.length} ${needsHarvest.length === 1 ? "tree needs" : "trees need"} harvest</span>` : ""}
          ${needsFertilizer.length ? `<span class="pill pill--accent">${needsFertilizer.length} ${needsFertilizer.length === 1 ? "tree needs" : "trees need"} fertilizer</span>` : ""}
        </div>`
      : "";

  // state.blockTreeFilter (see its own docstring on `state`) — "marked"
  // shows only trees still carrying a stage-1 flag (the same set the
  // attention banner above counts), "all" is the previous unfiltered list.
  // Built from `trees` (already active-only, marked-first), never from a
  // separate query, so this stays one source of truth with the banner/
  // proximity logic above.
  const showingMarkedOnly = state.blockTreeFilter !== "all";
  const markedTreeIds = new Set([...needsHarvest, ...needsFertilizer].map((t) => t.id));
  const stageBase = showingMarkedOnly ? trees.filter((t) => markedTreeIds.has(t.id)) : trees;
  const query = (state.blockTreeQuery || "").trim().toLowerCase();
  const visibleTrees = query ? stageBase.filter((t) => treeLabel(t).toLowerCase().includes(query)) : stageBase;
  const shownTrees = visibleTrees.slice(0, state.blockTreeLimit);
  const STAGE_TEXT = { overdue: "Overdue", due_soon: "Due soon", ok: "OK", never: "Never harvested", marked: "Needs harvest" };

  const treeItems = shownTrees
    .map((tree) => {
      const badges = [
        tree.harvest_marked_at ? '<span class="pill pill--accent">Needs harvest</span>' : "",
        tree.fertilizer_marked_at ? '<span class="pill pill--accent">Needs fertilizer</span>' : "",
      ].join("");
      const harvestLine = !tree.harvest_marked_at && tree.harvest_stage
        ? ` · Harvest: ${escapeHtml(STAGE_TEXT[tree.harvest_stage] || tree.harvest_stage)}`
        : "";
      return `
      <li>
        <button type="button" class="tree-item" data-tree-id="${escapeHtml(tree.id)}">
          <span class="tree-item__name">${escapeHtml(treeLabel(tree))}</span>
          <span class="tree-item__meta">${escapeHtml(healthLabel(tree.current_health))}${harvestLine} ${badges}</span>
        </button>
      </li>`;
    })
    .join("");
  // Tapping a tree opens its detail/edit screen — docs/05-offline-sync.md
  // § Farm → Block → Tree geo-navigation, step 3: "Tree level — tapping a
  // tree ... opens that tree's detail/yield-entry screen." Wired in
  // wireScreenEvents() below (this function only builds markup).

  return `
    <div class="screen screen--block">
      <header class="screen-header">
        <button id="back-btn" class="btn-text btn-text--back">&larr; Blocks</button>
        <h1>${escapeHtml(block.name)}</h1>
      </header>

      ${attentionBanner}

      ${
        proximity.length
          ? `<div class="proximity-prompt">
               <p class="proximity-prompt__text">Trees near you${state.location.accuracyM ? ` (GPS ±${Math.round(state.location.accuracyM)}m)` : ""} — pick the one you're at to log a harvest:</p>
               ${proximity
                 .map(
                   (c) => `<button type="button" class="proximity-choice" data-tree-id="${escapeHtml(c.tree.id)}">
                     ${escapeHtml(treeLabel(c.tree))} · ${Math.round(c.distanceM)}m
                   </button>`
                 )
                 .join("")}
             </div>`
          : ""
      }

      <div class="action-row">
        <button id="add-tree-btn">+ Add tree</button>
        <button id="log-harvest-btn">Log harvest</button>
        <button id="log-fertilizer-btn">Log fertilizer</button>
        ${trees.length ? '<button id="block-map-btn" type="button">Map view</button>' : ""}
      </div>

      <div class="tree-list-header">
        <h2>${showingMarkedOnly ? "Needs action" : "Trees"} (${visibleTrees.length})</h2>
        ${
          trees.length
            ? `<button type="button" id="block-tree-filter-toggle" class="btn-text">
                 ${showingMarkedOnly ? `Show all trees (${trees.length})` : "Show marked only"}
               </button>`
            : ""
        }
      </div>
      ${
        stageBase.length > 8 || query
          ? `<input type="search" id="block-tree-search" class="tree-search" placeholder="Search tree name or number"
               aria-label="Search trees in this block" value="${escapeHtml(state.blockTreeQuery || "")}" autocomplete="off">`
          : ""
      }
      ${
        treeItems
          ? `<ul class="tree-list">${treeItems}</ul>
             ${
               visibleTrees.length > shownTrees.length
                 ? `<p class="muted">Showing ${shownTrees.length} of ${visibleTrees.length}.</p>
                    <button type="button" id="block-tree-more-btn">Show ${Math.min(100, visibleTrees.length - shownTrees.length)} more</button>`
                 : ""
             }`
          : query
          ? `<div class="empty-state"><span class="empty-state__title">No match</span>
               <p class="empty-state__hint">No tree in this list matches “${escapeHtml(state.blockTreeQuery)}”.</p></div>`
          : showingMarkedOnly
          ? `<div class="empty-state">
               <span class="empty-state__title">Nothing marked right now</span>
               <p class="empty-state__hint">No trees in this block are currently flagged as needing harvest or fertilizer. ${trees.length ? "Tap “Show all trees” to browse the rest." : ""}</p>
             </div>`
          : `<div class="empty-state">
               <span class="empty-state__title">No trees in this block yet</span>
               <p class="empty-state__hint">Use “+ Add tree” above to record the first one. It saves on this device straight away, even with no signal.</p>
             </div>`
      }
    </div>
  `;
}

/** Colorblind-safe combined harvest+fertilizer indicator for one tree pin —
 * ported directly from the office dashboard's own treeIndicatorStyle()
 * (apps/dashboard/templates/dashboard/farm_map.html) so a tree reads the
 * same way in the field as it does in the office: same five states, same
 * colors, same stroke-pattern vocabulary (a pattern as well as a hue,
 * since the two most urgent states — overdue/due soon — are exactly the
 * red/amber pair protanopia and deuteranopia collapse). "Marked" (stage 1,
 * a Manager flagged this tree from the dashboard) wins over "overdue,"
 * which wins over "due soon," which wins over "never recorded," which
 * wins over the plain "ok" default — one ring has to stand in for BOTH
 * harvest_stage and fertilizer_stage at once, so this is worst-of-both,
 * same rule the dashboard's version uses.
 */
function treeIndicatorStyle(tree) {
  const marked = tree.harvest_stage === "marked" || tree.fertilizer_stage === "marked";
  const overdue = tree.harvest_stage === "overdue" || tree.fertilizer_stage === "overdue";
  const dueSoon = tree.harvest_stage === "due_soon" || tree.fertilizer_stage === "due_soon";
  const neverRecorded = tree.harvest_stage === "never" && tree.fertilizer_stage === "never";
  if (marked) return { color: "#3B6FA0", dashArray: null };
  if (overdue) return { color: "#C0392B", dashArray: "6 3" };
  if (dueSoon) return { color: "#D9A441", dashArray: "2 3" };
  if (neverRecorded) return { color: "#8A8578", dashArray: "1 6" };
  return { color: "#1F7A8C", dashArray: null };
}

// Same fill-by-health mapping as the dashboard's own healthColors — pin
// fill is health, pin ring is harvest/fertilizer stage (treeIndicatorStyle
// above), independent axes shown at once, exactly like the office map.
const TREE_HEALTH_FILL_COLORS = { healthy: "#99B29D", yellow: "#9B4D3E", unknown: "#8A8578" };

/** Block-level offline map — the field-app counterpart to blockScreen()'s
 * plain text list, plotting every tree's actual GPS pin instead of just a
 * name in a list. Built after direct feedback asking for two things at
 * once: "add map to show where marked trees location" and bulk mark-done
 * input for harvest/fertilizer completion — combined into one screen
 * deliberately, not two disconnected pieces, since in practice "which
 * trees need doing, and where are they" and "which trees am I about to
 * mark done" are the same question a field worker standing in the block
 * is asking. Tapping a pin both shows where that tree physically is AND
 * toggles it into the bulk-action-bar selection at the bottom — see
 * wireScreenEvents()' ".map-tree" handler and submitBulkMarkDone() below.
 *
 * Reuses buildMapProjection() (mapScreen()'s own farm-level projection
 * math) for a single block, with every tree's coordinates folded into the
 * bounding box via its extraPoints parameter, so a tree sitting right at
 * (or just past) the boundary edge — including a GPS-orphaned one, see
 * Tree.is_location_orphaned() — still lands inside the visible view.
 */
async function blockMapScreen() {
  const block = await window.PalmVisionDB.getOne("farm_blocks", state.selectedBlockId);
  if (!block) return `<p class="error">Block not found in local cache.</p>`;

  const allTrees = await window.PalmVisionDB.getAll("trees");
  // Same isActiveRecord()/filter shape as blockScreen() above; `t.location`
  // is required on every synced Tree (see apps/trees/models.py), but an
  // offline-queued create not yet round-tripped through the server could
  // in principle be missing other fields, so this stays defensive.
  const trees = sortTrees(allTrees.filter((t) => t.farm_block === block.id && isActiveRecord(t) && t.location));

  const treePoints = trees.map((t) => t.location.coordinates);
  const projection = buildMapProjection([block], state.location, treePoints);
  const selectedIds = new Set(state.blockMapSelectedTreeIds);

  let svgContent = "";
  let view = null;
  if (projection) {
    view = state.blockMapView || { x: 0, y: 0, w: projection.viewWidth, h: projection.viewHeight };
    let boundaryPath = "";
    if (block.boundary && block.boundary.coordinates && block.boundary.coordinates[0]) {
      const ring = block.boundary.coordinates[0];
      const pathPoints = ring.map((coord) => projection.project(coord).join(",")).join(" L ");
      boundaryPath = `<path class="map-block-shape" d="M ${pathPoints} Z"></path>`;
    }

    const treePins = trees
      .map((tree) => {
        const [x, y] = projection.project(tree.location.coordinates);
        const indicator = treeIndicatorStyle(tree);
        const fill = TREE_HEALTH_FILL_COLORS[tree.current_health] || TREE_HEALTH_FILL_COLORS.unknown;
        const isSelected = selectedIds.has(tree.id);
        return `
          <g class="map-tree" data-tree-id="${escapeHtml(tree.id)}">
            ${isSelected ? `<circle class="map-tree-selected-ring" cx="${x}" cy="${y}" r="11"></circle>` : ""}
            <circle class="map-tree-pin" cx="${x}" cy="${y}" r="7" fill="${fill}" stroke="${indicator.color}"
              stroke-width="3" ${indicator.dashArray ? `stroke-dasharray="${indicator.dashArray}"` : ""}></circle>
          </g>`;
      })
      .join("");

    let positionMarker = "";
    if (state.location) {
      const [x, y] = projection.project([state.location.lon, state.location.lat]);
      positionMarker = `<circle class="map-user-dot" cx="${x}" cy="${y}" r="6"></circle>`;
    }

    svgContent = `
      <div class="map-wrap">
        <svg class="map-svg map-svg--interactive" data-full-w="${projection.viewWidth}" data-full-h="${projection.viewHeight}"
          viewBox="${view.x} ${view.y} ${view.w} ${view.h}" preserveAspectRatio="xMidYMid meet" role="img"
          aria-label="Map of trees in ${escapeHtml(block.name)}. Drag to pan, use the zoom buttons, tap a tree to select it.">
          ${boundaryPath}
          ${treePins}
          ${positionMarker}
        </svg>
        <div class="map-zoom">
          <button type="button" id="map-zoom-in" aria-label="Zoom in">+</button>
          <button type="button" id="map-zoom-out" aria-label="Zoom out">&minus;</button>
          <button type="button" id="map-zoom-reset" aria-label="Fit whole block">&#8962;</button>
        </div>
      </div>`;
  }

  const selectedCount = selectedIds.size;
  const bulkBar = selectedCount
    ? `
      <div class="bulk-action-bar" id="bulk-action-bar">
        <span class="bulk-action-bar__count">${selectedCount} tree${selectedCount === 1 ? "" : "s"} selected</span>
        <div class="bulk-action-bar__buttons">
          <button type="button" id="bulk-mark-harvested-btn" ${navigator.onLine ? "" : "disabled"}>Mark harvested</button>
          <button type="button" id="bulk-mark-fertilized-btn" ${navigator.onLine ? "" : "disabled"}>Mark fertilized</button>
          <button type="button" id="bulk-clear-btn" class="btn-text">Clear</button>
        </div>
      </div>`
    : "";

  return `
    <div class="screen screen--block-map">
      <header class="screen-header">
        <button id="back-btn" class="btn-text btn-text--back">&larr; ${escapeHtml(block.name)}</button>
        <h1>${escapeHtml(block.name)} — map</h1>
      </header>
      ${
        svgContent ||
        `<div class="empty-state">
           <span class="empty-state__title">Nothing to draw yet</span>
           <p class="empty-state__hint">This block has no boundary and no located trees downloaded to this device yet.</p>
         </div>`
      }
      <p class="muted map-legend-note">Tap a tree to select it (the nearest one to your finger is picked), then mark it done below. Drag to pan; use + / − to zoom. Ring: blue = assigned by office, red = overdue, amber = due soon, dashed gray = never recorded, teal = OK. Fill: tree health.</p>
      ${bulkBar}
      ${!navigator.onLine && selectedCount ? `<p class="pill pill--warning">You're offline — marking selected trees needs a live connection.</p>` : ""}
      ${state.error ? `<p class="error" role="alert">${escapeHtml(state.error)}</p>` : ""}
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
        <p class="muted">Location: ${
          state.location ? `${state.location.lat.toFixed(6)}, ${state.location.lon.toFixed(6)}` : "waiting for GPS…"
        }</p>
        <button type="submit" ${state.location ? "" : "disabled"}>Save tree</button>
      </form>
      ${state.error ? `<p class="error" role="alert">${escapeHtml(state.error)}</p>` : ""}
    </div>
  `;
}

/** Tree detail + edit — docs/05-offline-sync.md's "tree detail/yield-entry
 * screen" (step 3 of the Farm → Block → Tree drill-down), previously
 * missing entirely: tapping a tree in blockScreen() had no handler at all
 * before this. Two distinct write paths on this one screen, deliberately
 * not the same one:
 *   - "Log harvest/fertilizer here" reuses actualFormScreen()'s existing
 *     tree-level path (selectedTreeId set) — already offline-queueable,
 *     unchanged.
 *   - The edit form (name/status/pin) is a *record edit*, which
 *     docs/05-offline-sync.md § Conflict handling says explicitly must be
 *     online-only, not queued — see api.js's onlineOnlyWrite(), used here
 *     instead of queueableWrite().
 * `current_health` and `source` are shown but never editable here:
 * current_health is server/ML-derived (TreeSerializer's read_only_fields),
 * and source is meaningful provenance (manual vs drone-detected) that a
 * name/status/location correction shouldn't silently overwrite.
 */
/** Formats an ISO date or datetime string in the user's locale, or "never"
 * for null — shared by the last-harvested/fertilized line and the
 * needs-attention banners below.
 */
function formatDateOrNever(isoString) {
  return isoString ? new Date(isoString).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "never";
}

/** "Last harvested: DATE" line, extended with a quantity when one was
 * recorded (Tree.last_harvest_quantity/last_fertilizer_quantity — see that
 * field's help_text) — previously this line only ever showed a date, with
 * no way to tell how much was actually done. Quantity is optional at
 * capture time, so a tree with a date but no quantity on record is
 * expected and shown plainly, not as an error.
 */
function formatLastDone(isoDate, quantity, unit) {
  if (!isoDate) return "never";
  const when = new Date(isoDate).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  return escapeHtml(quantity === null || quantity === undefined ? when : `${when} (${quantity} ${unit})`);
}

/** Stage-1 "needs harvest/fertilizer" banner + completion control — `kind`
 * is 'harvest' or 'fertilizer'. Only rendered when the corresponding
 * *_marked_at field is set (see Tree.harvest_marked_at's help_text,
 * apps/trees/models.py, for the two-stage workflow this is stage 2 of).
 *
 * Two states, toggled by state.quickMarkOpenKind: a plain "Mark
 * harvested"/"Mark fertilized" button, or — once tapped — an inline
 * optional-quantity form in its place. It used to be a single button that
 * posted immediately with no way to record how much was actually done;
 * this is the field-app side of the same gap the office dashboard's
 * "Record harvest/fertilizer directly" flow had. Wired in
 * wireScreenEvents() below, alongside the tree-edit-form, since both are
 * online-only record edits (see that handler's own comment).
 */
function markBanner(tree, kind) {
  const markedAt = kind === "harvest" ? tree.harvest_marked_at : tree.fertilizer_marked_at;
  if (!markedAt) return "";
  const label = kind === "harvest" ? "harvest" : "fertilizer";
  const doneVerb = label === "harvest" ? "harvested" : "fertilized";
  const unit = "kg";
  const assignedLine = `<span class="pill pill--accent">Needs ${label} — assigned ${formatDateOrNever(markedAt)}</span>`;

  if (state.quickMarkOpenKind !== kind) {
    return `
      <div class="mark-banner">
        ${assignedLine}
        <button id="mark-${kind}-btn" data-tree-id="${escapeHtml(tree.id)}" ${navigator.onLine ? "" : "disabled"}>Mark ${doneVerb}</button>
      </div>`;
  }

  return `
    <div class="mark-banner mark-banner--open">
      ${assignedLine}
      <form id="mark-${kind}-form" data-tree-id="${escapeHtml(tree.id)}" data-kind="${kind}">
        <label>Quantity (${unit}, optional)<input name="quantity" type="number" step="0.01" min="0" inputmode="decimal"></label>
        <div class="action-row">
          <button type="submit" ${navigator.onLine ? "" : "disabled"}>Save</button>
          <button type="button" id="mark-${kind}-cancel-btn" class="btn-text">Cancel</button>
        </div>
      </form>
    </div>`;
}

async function treeDetailScreen() {
  const tree = await window.PalmVisionDB.getOne("trees", state.selectedTreeId);
  if (!tree) return `<p class="error">Tree not found in local cache.</p>`;

  const [lon, lat] = tree.location.coordinates;
  const needsAnyAction = Boolean(tree.harvest_marked_at || tree.fertilizer_marked_at);
  const offlineNotice = !navigator.onLine
    ? `<p class="pill pill--warning">You appear to be offline — saving${needsAnyAction ? " or marking as done" : " an edit"} needs a live connection (nothing here is queued, so this doesn't risk overwriting a change made elsewhere while you were offline).</p>`
    : "";

  return `
    <div class="screen screen--tree-detail">
      <header class="screen-header">
        <button id="back-btn" class="btn-text btn-text--back">&larr; Block</button>
        <h1>${escapeHtml(treeLabel(tree))}</h1>
      </header>

      <p class="pill ${tree.status === "removed" ? "pill--warning" : "pill--accent"}">${tree.status === "removed" ? "Removed" : "Active"}</p>
      <p class="muted">Health: ${escapeHtml(healthLabel(tree.current_health))} &middot; Source: ${tree.source === "drone_detected" ? "Drone detected" : "Manual"}</p>
      <p class="muted">Pin: ${lat.toFixed(6)}, ${lon.toFixed(6)}</p>
      <p class="muted">Last harvested: ${formatLastDone(tree.last_harvested_at, tree.last_harvest_quantity, "kg")} &middot; Last fertilized: ${formatLastDone(tree.last_fertilized_at, tree.last_fertilizer_quantity, "kg")}</p>

      ${offlineNotice}
      ${markBanner(tree, "harvest")}
      ${markBanner(tree, "fertilizer")}

      <div class="action-row">
        <button id="log-harvest-tree-btn" data-tree-id="${escapeHtml(tree.id)}">Log harvest here</button>
        <button id="log-fertilizer-tree-btn" data-tree-id="${escapeHtml(tree.id)}">Log fertilizer here</button>
      </div>

      <h2>Edit</h2>
      <form id="tree-edit-form">
        <label>Name<input name="name" value="${escapeHtml(tree.name || "")}"></label>
        <label>Status
          <select name="status">
            <option value="active" ${tree.status === "active" ? "selected" : ""}>Active</option>
            <option value="removed" ${tree.status === "removed" ? "selected" : ""}>Removed</option>
          </select>
        </label>
        <label class="checkbox-label">
          <input type="checkbox" name="update_location" ${state.location ? "" : "disabled"}>
          Update pin to my current GPS position${
            state.location ? ` (${state.location.lat.toFixed(6)}, ${state.location.lon.toFixed(6)})` : " (waiting for GPS…)"
          }
        </label>
        <button type="submit">Save changes</button>
      </form>
      ${state.error ? `<p class="error" role="alert">${escapeHtml(state.error)}</p>` : ""}
    </div>
  `;
}

/** Shared by both harvest and fertilizer logging — `kind` is 'harvest' or
 * 'fertilizer'. Supports both block-level (manual total) and, when a tree
 * is pre-selected via the proximity prompt, tree-level entry — see
 * docs/05-offline-sync.md § Yield granularity.
 *
 * Block-level entry only (`!treeMode`) also offers a "log against a plan"
 * choice — requested directly: picking an existing plan should
 * auto-complete every tree in the block still marked as needing this
 * action, not leave a Manager's to-do list untouched next to a plan
 * that's now been executed against. Not offered in tree mode: that flow
 * already targets one specific tree by hand, so "pick a plan and
 * auto-complete every OTHER marked tree on it" doesn't apply. See
 * submitBlockLevelActual()'s own comment for the online-only mechanics
 * this choice triggers.
 */
async function actualFormScreen(kind) {
  const label = kind === "harvest" ? "Harvest" : "Fertilizer";
  const treeMode = Boolean(state.selectedTreeId);
  const selectedTree = treeMode ? await window.PalmVisionDB.getOne("trees", state.selectedTreeId) : null;

  // Refresh the local plan picture before deciding whether this block
  // already has an open plan to attach to. Without this, "open plans" below
  // (and findOrCreatePlan() at submit time) read whatever the last
  // periodic/foreground/reconnect sync happened to pull — which, moments
  // after a Manager creates a plan from the office dashboard and hands the
  // device to a field worker, is stale. findOrCreatePlan() can't see a plan
  // it doesn't know exists, so it silently creates a SEPARATE duplicate
  // Harvest/Fertilizer plan instead of attaching to the real one: the
  // Manager's plan then sits forever with no actual recorded against it
  // (never reaches Yield history), while the real field data lands on a
  // phantom plan nobody in the office ever sees. Reported directly: "make a
  // harvest plan, do the harvest plan in pwa, then back to dashboard the
  // harvest plan gone, not moved into yield history." Best-effort — offline
  // is a legitimate state this screen still has to work in, falling back to
  // whatever's already in IndexedDB, so a failed pull here is swallowed
  // rather than surfaced as a form error.
  if (navigator.onLine) {
    try {
      await window.PalmVisionSync.pullChanges();
    } catch (err) {
      console.warn("Pre-form sync failed (non-fatal)", err);
    }
  }

  let planChoiceHtml = "";
  if (!treeMode) {
    const planStore = kind === "harvest" ? "harvests" : "fertilizers";
    const openPlans = (await window.PalmVisionDB.getAll(planStore))
      .filter((p) => p.farm_block === state.selectedBlockId && p.status !== "done")
      .sort((a, b) => (a.assessment_date < b.assessment_date ? 1 : -1));
    const planOptions = openPlans
      .map((p) => {
        const assessed = kind === "harvest" && p.assessment_quantity != null ? ` — ${escapeHtml(p.assessment_quantity)} kg assessed` : "";
        return `<option value="${escapeHtml(p.id)}">${formatDateOrNever(p.assessment_date)}${assessed}</option>`;
      })
      .join("");
    planChoiceHtml = `
      <fieldset id="plan-choice">
        <legend>Log against</legend>
        <label class="radio-label">
          <input type="radio" name="plan_mode" value="none" checked>
          Without a plan
        </label>
        <label class="radio-label">
          <input type="radio" name="plan_mode" value="plan" ${openPlans.length ? "" : "disabled"}>
          An existing ${label.toLowerCase()} plan${openPlans.length ? "" : ` <span class="muted">(none open for this block)</span>`}
        </label>
        ${
          openPlans.length
            ? `<div id="plan-picker-fields" style="display:none;">
                 <label>Plan<select name="plan_id">${planOptions}</select></label>
                 <p class="muted">Every tree currently marked as needing ${kind} in this block will be marked ${kind === "harvest" ? "harvested" : "fertilized"} too.</p>
               </div>`
            : ""
        }
      </fieldset>
    `;
  }

  return `
    <div class="screen screen--form">
      <header class="screen-header">
        <button id="back-btn" class="btn-text btn-text--back">&larr; Block</button>
        <h1>Log ${label.toLowerCase()}${treeMode ? " (tree)" : " (block total)"}</h1>
      </header>
      ${selectedTree ? `<p class="pill pill--accent">For tree: <strong>${escapeHtml(treeLabel(selectedTree))}</strong></p>` : ""}
      <form id="actual-form" data-kind="${kind}">
        ${planChoiceHtml}
        <label>Date<input name="date" type="date" required max="${new Date().toISOString().slice(0, 10)}" value="${new Date().toISOString().slice(0, 10)}"></label>
        ${
          kind === "harvest"
            ? // Asked directly to swap primacy: weight (kg) is now the
              // authoritative harvest measurement (ActualHarvest.weight_kg/
              // HarvestTree.weight_kg — apps/operations/models.py),
              // required and first; bunch count is optional supplementary
              // detail, same field name (`quantity`) it's always had.
              `<label>Weight (kg)<input name="weight_kg" type="number" step="0.01" min="0" inputmode="decimal" required></label>
               <label>Bunch count (optional)<input name="quantity" type="number" step="0.01" min="0" inputmode="decimal"></label>`
            : '<label>Quantity (kg)<input name="quantity" type="number" step="0.01" min="0" inputmode="decimal" required></label>'
        }
        <button type="submit">Save</button>
      </form>
      ${state.error ? `<p class="error" role="alert">${escapeHtml(state.error)}</p>` : ""}
    </div>
  `;
}

// --- Items the server rejected --------------------------------------------

const OUTBOX_RESOURCE_LABELS = {
  trees: "New tree",
  harvests: "Harvest plan",
  actual_harvests: "Harvest log",
  harvest_trees: "Tree harvest entry",
  fertilizers: "Fertilizer plan",
  actual_fertilizers: "Fertilizer log",
  fertilizer_trees: "Tree fertilizer entry",
};

function describeOutboxEntry(entry) {
  const body = entry.body || {};
  const parts = [OUTBOX_RESOURCE_LABELS[entry.resource] || entry.resource];
  if (body.name) parts.push(body.name);
  if (body.date) parts.push(formatDateOrNever(body.date));
  if (body.weight_kg != null) parts.push(`${body.weight_kg} kg`);
  else if (body.quantity != null) parts.push(`${body.quantity} kg`);
  return parts.join(" · ");
}

/** One row per rejected write: what it was, the server's reason, and a
 * choice — try again (e.g. once a Manager has reopened the plan, or a
 * block reassignment is fixed) or discard it. Discarding also drops the
 * local copy the form saved optimistically, so the entry doesn't linger
 * in lists as if it had been saved.
 */
function syncIssuesScreen(failedEntries) {
  const rows = failedEntries
    .map(
      (entry) => `
      <li class="sync-issue">
        <div class="sync-issue__what">${escapeHtml(describeOutboxEntry(entry))}</div>
        <div class="sync-issue__why">${escapeHtml(entry.lastError || "Rejected by the server.")}</div>
        <div class="muted">Saved on this device ${escapeHtml(new Date(entry.createdAt).toLocaleString())}</div>
        <div class="action-row">
          <button type="button" class="sync-issue-retry" data-outbox-id="${escapeHtml(entry.outboxId)}">Try again</button>
          <button type="button" class="btn-text sync-issue-discard" data-outbox-id="${escapeHtml(entry.outboxId)}">Discard</button>
        </div>
      </li>`
    )
    .join("");
  return `
    <div class="screen screen--sync-issues">
      <header class="screen-header">
        <button id="back-btn" class="btn-text btn-text--back">&larr; Blocks</button>
        <h1>Couldn't be saved</h1>
      </header>
      ${
        failedEntries.length
          ? `<p class="muted">The server rejected these. Nothing here was saved yet — fix the cause and try again, or discard.</p>
             <ul class="sync-issue-list">${rows}</ul>`
          : `<div class="empty-state"><span class="empty-state__title">Nothing to review</span></div>`
      }
    </div>`;
}

async function wireSyncIssueButtons() {
  const entries = await window.PalmVisionDB.getOutboxEntries();
  const byId = (id) => entries.find((e) => String(e.outboxId) === id);

  document.querySelectorAll(".sync-issue-retry").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const entry = byId(btn.dataset.outboxId);
      if (!entry) return;
      await window.PalmVisionDB.updateOutboxEntry(entry.outboxId, { status: "pending", lastError: null });
      try {
        await window.PalmVisionSync.runSync();
      } catch (err) {
        console.error("Retry sync failed", err);
      }
      await render();
    });
  });

  document.querySelectorAll(".sync-issue-discard").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const entry = byId(btn.dataset.outboxId);
      if (!entry) return;
      if (!window.confirm(`Discard "${describeOutboxEntry(entry)}"? It will not be saved.`)) return;
      await window.PalmVisionDB.deleteOutboxEntry(entry.outboxId);
      if (entry.body && entry.body.id) await window.PalmVisionDB.deleteAll(entry.resource, [entry.body.id]);
      await render();
    });
  });
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
    case "map":
      body = await mapScreen();
      break;
    case "blockMap":
      body = await blockMapScreen();
      break;
    case "treeForm":
      body = treeFormScreen();
      break;
    case "treeDetail":
      body = await treeDetailScreen();
      break;
    case "harvestForm":
      body = await actualFormScreen("harvest");
      break;
    case "fertilizerForm":
      body = await actualFormScreen("fertilizer");
      break;
    case "syncIssues":
      body = syncIssuesScreen(outbox.filter((e) => e.status === "failed"));
      break;
    default:
      body = `<p>Unknown screen</p>`;
  }
  const chip = state.screen === "login" ? "" : renderSyncChip(window._lastSyncStatus || "idle", splitOutbox(outbox).pending);
  const banner = state.screen === "login" || state.screen === "syncIssues" ? "" : renderUnsyncedBanner(outbox);
  el.innerHTML = chip + banner + body;
  wireScreenEvents();
  wireSyncNowButton();
  if (state.screen === "syncIssues") await wireSyncIssueButtons();
}

// --- Event wiring ---------------------------------------------------------

/** Binds the mark-harvest-btn/mark-fertilizer-btn CLICK (present when
 * markBanner() is in its collapsed state) to expand the inline quantity
 * form in its place — see markBanner()'s docstring. `kind` is "harvest" or
 * "fertilizer"; wireMarkDoneForm() below handles what happens once that
 * form is actually submitted.
 */
function wireMarkExpandButton(kind) {
  const btn = document.getElementById(`mark-${kind}-btn`);
  if (!btn) return;
  btn.addEventListener("click", () => {
    state.quickMarkOpenKind = kind;
    render();
  });
}

/** Binds the mark-harvest-form/mark-fertilizer-form SUBMIT (present once
 * markBanner() is expanded) plus its Cancel button — see markBanner()'s
 * docstring and the two calls to this in wireScreenEvents() below.
 * `apiAction` is "mark_harvested" or "mark_fertilized", matching
 * TreeViewSet's own action names (apps/trees/views.py) exactly, so the
 * POST path is built the same way DRF's router would generate it.
 */
function wireMarkDoneForm(kind, apiAction) {
  const form = document.getElementById(`mark-${kind}-form`);
  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const treeId = form.dataset.treeId;
      const submitBtn = form.querySelector("button[type=submit]");
      const originalLabel = submitBtn.textContent;
      submitBtn.disabled = true;
      submitBtn.textContent = "Saving…";
      const quantityRaw = new FormData(form).get("quantity");
      const body = quantityRaw ? { quantity: quantityRaw } : {};
      try {
        const updated = await window.PalmVisionAPI.onlineOnlyWrite(`/api/trees/${treeId}/${apiAction}/`, "POST", body);
        await window.PalmVisionDB.putOne("trees", updated);
        state.quickMarkOpenKind = null;
        await render(); // stays on this same tree-detail screen, now reflecting the cleared marked_at
      } catch (err) {
        submitBtn.disabled = false;
        submitBtn.textContent = originalLabel;
        if (err.isNetworkError) {
          showError("You're offline — this needs a live connection. Nothing was saved; try again once you're back online.");
        } else {
          showError(err);
        }
      }
    });
  }

  const cancelBtn = document.getElementById(`mark-${kind}-cancel-btn`);
  if (cancelBtn) {
    cancelBtn.addEventListener("click", () => {
      state.quickMarkOpenKind = null;
      render();
    });
  }
}

/** POSTs the current blockMapScreen() selection to TreeViewSet's
 * bulk_mark_harvested/bulk_mark_fertilized action (apps/trees/views.py) —
 * the bulk counterpart to wireMarkDoneForm()'s single-tree onlineOnlyWrite()
 * call above, and deliberately the same online-only rule: this mutates
 * existing Tree rows, not append-only new records, so it follows
 * docs/05-offline-sync.md § Conflict handling the same way the single-tree
 * action already does (see that function's own comment).
 *
 * No per-tree quantity entry in this first cut — same reasoning
 * findLatestPlan() gives for skipping block-level plan-picker UI: a
 * reasonable follow-up, not required to prove out the core bulk-completion
 * gap this was built to close. A field worker who needs to capture a
 * specific quantity for one tree can still do that via the existing
 * single-tree "Mark harvested"/"Mark fertilized" form on that tree's own
 * detail screen.
 */
async function submitBulkMarkDone(apiAction) {
  const body = { tree_ids: state.blockMapSelectedTreeIds };
  const result = await window.PalmVisionAPI.onlineOnlyWrite(`/api/trees/${apiAction}/`, "POST", body);
  if (result.updated && result.updated.length) {
    await window.PalmVisionDB.putAll("trees", result.updated);
  }
  state.blockMapSelectedTreeIds = [];
  return result;
}

/** Binds one of the bulk-action-bar's "Mark harvested"/"Mark fertilized"
 * buttons (blockMapScreen()) — `kind` is only used for the button's own
 * disabled/label bookkeeping, `apiAction` is the actual
 * bulk_mark_harvested/bulk_mark_fertilized URL segment (see
 * submitBulkMarkDone()'s docstring).
 */
function wireBulkMarkButton(kind, btnId, apiAction) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const originalLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Saving…";
    try {
      const result = await submitBulkMarkDone(apiAction);
      if (result.skipped_tree_ids && result.skipped_tree_ids.length) {
        // Rare — e.g. a PIC Block reassignment landed between opening the
        // map and tapping the button — but the trees that WERE in scope
        // are already saved by this point, so this is a heads-up, not a
        // rollback of anything.
        showError(
          `${result.skipped_tree_ids.length} of the selected trees couldn't be marked — they may no longer be assigned to you. The rest were saved.`
        );
      } else {
        await render();
      }
    } catch (err) {
      btn.disabled = false;
      btn.textContent = originalLabel;
      if (err.isNetworkError) {
        showError("You're offline — this needs a live connection. Nothing was saved; try again once you're back online.");
      } else {
        showError(err);
      }
    }
  });
}

/** Zoom/pan/tap for blockMapScreen()'s SVG. A block can have 1,000+ trees in
 * a ~340px-wide drawing, so the old 15px per-pin tap targets were
 * unusable and there was no way to zoom. Now: pins keep a constant on-screen
 * size as you zoom, a tap selects the NEAREST pin within ~22px of the finger
 * (not whichever circle happens to be on top), drag pans, and +/−/⌂ zoom.
 * The view lives in state.blockMapView so it survives the re-render a
 * selection change triggers.
 */
function wireBlockMap() {
  const svg = document.querySelector(".map-svg--interactive");
  if (!svg) return;
  const fullW = Number(svg.dataset.fullW);
  const fullH = Number(svg.dataset.fullH);
  const getView = () => {
    const [x, y, w, h] = svg.getAttribute("viewBox").split(" ").map(Number);
    return { x, y, w, h };
  };
  const setView = (v) => svg.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
  const pxPerUnit = () => {
    const r = svg.getBoundingClientRect();
    const v = getView();
    return Math.min(r.width / v.w, r.height / v.h) || 1;
  };

  const pins = [...svg.querySelectorAll(".map-tree")].map((g) => {
    const pin = g.querySelector(".map-tree-pin");
    return { id: g.dataset.treeId, g, pin, ring: g.querySelector(".map-tree-selected-ring"), x: Number(pin.getAttribute("cx")), y: Number(pin.getAttribute("cy")) };
  });
  const sizePins = () => {
    const ppu = pxPerUnit();
    const r = Math.max(7, 4.5 / ppu);
    const stroke = Math.max(3, 1.6 / ppu);
    pins.forEach((p) => {
      p.pin.setAttribute("r", r);
      p.pin.setAttribute("stroke-width", stroke);
      if (p.ring) p.ring.setAttribute("r", r + 5 / ppu);
    });
  };
  sizePins();

  const commit = () => { state.blockMapView = getView(); };
  const zoomBy = (factor, cx, cy) => {
    const v = getView();
    const nw = Math.min(fullW, Math.max(fullW / 25, v.w / factor));
    const nh = nw * (fullH / fullW);
    const fx = cx === undefined ? v.x + v.w / 2 : cx;
    const fy = cy === undefined ? v.y + v.h / 2 : cy;
    setView({ x: fx - ((fx - v.x) / v.w) * nw, y: fy - ((fy - v.y) / v.h) * nh, w: nw, h: nh });
    sizePins();
    commit();
  };
  document.getElementById("map-zoom-in").addEventListener("click", () => zoomBy(1.8));
  document.getElementById("map-zoom-out").addEventListener("click", () => zoomBy(1 / 1.8));
  document.getElementById("map-zoom-reset").addEventListener("click", () => { state.blockMapView = null; render(); });
  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const pt = svg.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const loc = pt.matrixTransform(svg.getScreenCTM().inverse());
    zoomBy(e.deltaY < 0 ? 1.3 : 1 / 1.3, loc.x, loc.y);
  }, { passive: false });

  let drag = null;
  svg.addEventListener("pointerdown", (e) => {
    drag = { sx: e.clientX, sy: e.clientY, view: getView(), moved: false, ppu: pxPerUnit() };
    svg.setPointerCapture(e.pointerId);
  });
  svg.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.sx;
    const dy = e.clientY - drag.sy;
    if (!drag.moved && Math.hypot(dx, dy) < 8) return;
    drag.moved = true;
    setView({ ...drag.view, x: drag.view.x - dx / drag.ppu, y: drag.view.y - dy / drag.ppu });
  });
  svg.addEventListener("pointerup", (e) => {
    const d = drag;
    drag = null;
    if (!d) return;
    if (d.moved) { commit(); return; }
    // A tap: pick the nearest pin within ~22 screen px.
    const pt = svg.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const loc = pt.matrixTransform(svg.getScreenCTM().inverse());
    const ppu = pxPerUnit();
    let best = null;
    let bestD = 22 / ppu;
    pins.forEach((p) => {
      const dist = Math.hypot(p.x - loc.x, p.y - loc.y);
      if (dist <= bestD) { best = p; bestD = dist; }
    });
    if (!best) return;
    const already = state.blockMapSelectedTreeIds.includes(best.id);
    state.blockMapSelectedTreeIds = already
      ? state.blockMapSelectedTreeIds.filter((id) => id !== best.id)
      : [...state.blockMapSelectedTreeIds, best.id];
    commit();
    render();
  });
  svg.addEventListener("pointercancel", () => { drag = null; });
}

function wireScreenEvents() {
  const loginForm = document.getElementById("login-form");
  if (loginForm) {
    loginForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(loginForm);
      state.loginUsername = fd.get("username") || "";
      try {
        const previousUser = await window.PalmVisionDB.getMeta("username");
        await window.PalmVisionAPI.login(fd.get("username"), fd.get("password"));
        // Session expired / app closed without logging out, then a different
        // person signs in: drop the previous user's cache so they get a full pull.
        if (previousUser && previousUser !== fd.get("username")) await window.PalmVisionDB.clearUserData();
        await window.PalmVisionDB.setMeta("username", fd.get("username"));
        const synced = await window.PalmVisionSync.runSync();
        // A sync already in flight (started before the token existed) makes
        // runSync() skip; pull directly so the block list isn't left empty.
        if (synced && synced.skipped) await window.PalmVisionSync.pullChanges();
        setScreen("blocks");
      } catch (err) {
        showError(err);
      }
    });
  }

  const logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) logoutBtn.addEventListener("click", async () => {
    // Try to send anything queued before wiping it, and never discard
    // unsent field data silently.
    if (navigator.onLine) {
      try { await window.PalmVisionSync.flushOutbox(); } catch (err) { console.warn("Flush before logout failed", err); }
    }
    const left = (await window.PalmVisionDB.getOutboxEntries()).length;
    if (left && !window.confirm(`${left} item${left === 1 ? " hasn't" : "s haven't"} been sent to the server yet. Logging out will discard ${left === 1 ? "it" : "them"}. Log out anyway?`)) return;
    await window.PalmVisionAPI.logout();
    await window.PalmVisionDB.clearUserData();
    setScreen("login");
  });

  document.querySelectorAll(".block-item").forEach((li) => {
    li.addEventListener("click", () => setScreen("block", { selectedBlockId: li.dataset.blockId, blockTreeFilter: "marked", blockTreeQuery: "", blockTreeLimit: 100 }));
  });

  const mapBtn = document.getElementById("map-btn");
  if (mapBtn) mapBtn.addEventListener("click", () => setScreen("map"));

  const redownloadBtn = document.getElementById("redownload-btn");
  if (redownloadBtn) {
    redownloadBtn.addEventListener("click", async () => {
      if (!navigator.onLine) {
        showError("You're offline — re-downloading needs a connection.");
        return;
      }
      await window.PalmVisionSync.runSync().catch(() => {}); // send anything queued first
      const unsent = (await window.PalmVisionDB.getOutboxEntries()).length;
      if (unsent) {
        showError(`Send or review the ${unsent} unsent ${unsent === 1 ? "item" : "items"} first, so nothing saved on this device is lost.`);
        return;
      }
      redownloadBtn.disabled = true;
      redownloadBtn.textContent = "Downloading…";
      try {
        const { removed } = await window.PalmVisionSync.fullResync();
        state.error = null;
        await render();
        console.info(`Re-download complete; removed ${removed} out-of-date record(s).`);
      } catch (err) {
        showError(err);
      }
    });
  }

  document.querySelectorAll(".map-block").forEach((g) => {
    g.addEventListener("click", () => setScreen("block", { selectedBlockId: g.dataset.blockId, blockTreeFilter: "marked", blockTreeQuery: "", blockTreeLimit: 100 }));
  });

  const backBtn = document.getElementById("back-btn");
  if (backBtn) {
    backBtn.addEventListener("click", () => {
      if (state.screen === "block" || state.screen === "map" || state.screen === "syncIssues") setScreen("blocks");
      else if (state.screen === "blockMap") {
        setScreen("block", { selectedBlockId: state.selectedBlockId, blockMapSelectedTreeIds: [] });
      } else setScreen("block", { selectedTreeId: null });
    });
  }

  const blockMapBtn = document.getElementById("block-map-btn");
  if (blockMapBtn) {
    // Fresh selection every time the map is (re)opened — a stale
    // selection from a previous visit silently carrying over would be a
    // real hazard here (marking the wrong trees done without re-tapping).
    blockMapBtn.addEventListener("click", () => setScreen("blockMap", { blockMapSelectedTreeIds: [], blockMapView: null }));
  }

  // Tapping a pin toggles that tree into/out of the bulk-action-bar
  // selection — see blockMapScreen()'s own docstring for why map viewing
  // and bulk selection are deliberately the same tap, not two separate
  // modes a field worker has to switch between.
  wireBlockMap();

  const bulkClearBtn = document.getElementById("bulk-clear-btn");
  if (bulkClearBtn) {
    bulkClearBtn.addEventListener("click", () => {
      state.blockMapSelectedTreeIds = [];
      render();
    });
  }

  wireBulkMarkButton("harvest", "bulk-mark-harvested-btn", "bulk_mark_harvested");
  wireBulkMarkButton("fertilizer", "bulk-mark-fertilized-btn", "bulk_mark_fertilized");

  const treeSearch = document.getElementById("block-tree-search");
  if (treeSearch) {
    let searchTimer = null;
    treeSearch.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(async () => {
        state.blockTreeQuery = treeSearch.value;
        state.blockTreeLimit = 100;
        await render();
        const again = document.getElementById("block-tree-search");
        if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
      }, 200);
    });
  }
  const treeMoreBtn = document.getElementById("block-tree-more-btn");
  if (treeMoreBtn) treeMoreBtn.addEventListener("click", () => { state.blockTreeLimit += 100; render(); });

  const blockTreeFilterToggle = document.getElementById("block-tree-filter-toggle");
  if (blockTreeFilterToggle) {
    blockTreeFilterToggle.addEventListener("click", () => {
      state.blockTreeFilter = state.blockTreeFilter === "all" ? "marked" : "all";
      render();
    });
  }

  const addTreeBtn = document.getElementById("add-tree-btn");
  if (addTreeBtn) addTreeBtn.addEventListener("click", () => setScreen("treeForm"));

  const logHarvestBtn = document.getElementById("log-harvest-btn");
  if (logHarvestBtn) logHarvestBtn.addEventListener("click", () => setScreen("harvestForm", { selectedTreeId: null }));

  const logFertilizerBtn = document.getElementById("log-fertilizer-btn");
  if (logFertilizerBtn) logFertilizerBtn.addEventListener("click", () => setScreen("fertilizerForm", { selectedTreeId: null }));

  document.querySelectorAll(".proximity-choice").forEach((btn) => {
    btn.addEventListener("click", () => setScreen("harvestForm", { selectedTreeId: btn.dataset.treeId }));
  });

  // Tapping a tree in the block list opens treeDetailScreen() — see that
  // function's docstring for why this was previously a dead click (no
  // handler existed at all).
  document.querySelectorAll(".tree-item").forEach((li) => {
    li.addEventListener("click", () => setScreen("treeDetail", { selectedTreeId: li.dataset.treeId, quickMarkOpenKind: null }));
  });

  const logHarvestTreeBtn = document.getElementById("log-harvest-tree-btn");
  if (logHarvestTreeBtn) {
    logHarvestTreeBtn.addEventListener("click", () =>
      setScreen("harvestForm", { selectedTreeId: logHarvestTreeBtn.dataset.treeId })
    );
  }

  const logFertilizerTreeBtn = document.getElementById("log-fertilizer-tree-btn");
  if (logFertilizerTreeBtn) {
    logFertilizerTreeBtn.addEventListener("click", () =>
      setScreen("fertilizerForm", { selectedTreeId: logFertilizerTreeBtn.dataset.treeId })
    );
  }

  // Stage 2 completion — confirms a Manager-assigned "needs harvest/
  // fertilizer" flag is actually done (see markBanner()'s docstring above).
  // Deliberately onlineOnlyWrite(), same as the tree-edit-form below: this
  // mutates an existing Tree record's own fields, not an append-only new
  // record, so it follows docs/05-offline-sync.md § Conflict handling's
  // "editing an existing record is online-only" rule rather than being
  // queued like a fresh yield-log entry would be.
  wireMarkExpandButton("harvest");
  wireMarkExpandButton("fertilizer");
  wireMarkDoneForm("harvest", "mark_harvested");
  wireMarkDoneForm("fertilizer", "mark_fertilized");

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
        // is undefined and the request payload is stored instead. That
        // payload has no server-assigned fields, so supply the ones the
        // block list actually reads: without `current_health` the meta line
        // renders "undefined". The record is identified as still-queued by
        // the *absence* of `status` — see isActiveRecord().
        const stored = result.response || { ...payload, current_health: "unknown" };
        await window.PalmVisionDB.putOne("trees", stored);
        setScreen("block", { selectedBlockId: state.selectedBlockId });
      } catch (err) {
        showError(err);
      }
    });
  }

  // Editing an existing tree — deliberately NOT queueableWrite(), see
  // api.js's onlineOnlyWrite() docstring and treeDetailScreen()'s. A
  // network failure here must not be silently queued; it's shown
  // immediately so the field worker knows the edit did not save.
  const treeEditForm = document.getElementById("tree-edit-form");
  if (treeEditForm) {
    treeEditForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(treeEditForm);
      const treeId = state.selectedTreeId;
      const payload = { name: fd.get("name") || "", status: fd.get("status") || "active" };
      if (payload.status === "removed" && !window.confirm("Mark this tree as removed? It disappears from the block's list (you can set it back to Active later).")) return;
      if (fd.get("update_location") === "on" && state.location) {
        payload.location = { type: "Point", coordinates: [state.location.lon, state.location.lat] };
      }
      try {
        const updated = await window.PalmVisionAPI.onlineOnlyWrite(`/api/trees/${treeId}/`, "PATCH", payload);
        await window.PalmVisionDB.putOne("trees", updated);
        setScreen("block", { selectedBlockId: state.selectedBlockId, selectedTreeId: null });
      } catch (err) {
        if (err.isNetworkError) {
          showError("You're offline — editing needs a live connection. Nothing was saved; try again once you're back online.");
        } else {
          showError(err);
        }
      }
    });
  }

  // Toggles the plan <select> (actualFormScreen() above) — shown only
  // once "An existing ... plan" is chosen, hidden (not just disabled) the
  // rest of the time so "Without a plan" stays the obvious, low-friction
  // default a field worker doesn't have to look past.
  const planPickerFields = document.getElementById("plan-picker-fields");
  if (planPickerFields) {
    document.querySelectorAll('input[name="plan_mode"]').forEach((radio) => {
      radio.addEventListener("change", () => {
        planPickerFields.style.display = radio.value === "plan" && radio.checked ? "block" : "none";
      });
    });
  }

  const actualForm = document.getElementById("actual-form");
  if (actualForm) {
    actualForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(actualForm);
      const kind = actualForm.dataset.kind;
      // Per-tree sanity check (block totals have no fixed ceiling, so they
      // aren't checked): a single oil palm rarely yields >150 kg of fruit in
      // one harvest or takes >25 kg of fertilizer, so a value that high is
      // far more likely a typo (an extra digit, grams entered as kg) than real.
      if (state.selectedTreeId) {
        const entered = Number(fd.get(kind === "harvest" ? "weight_kg" : "quantity"));
        const limit = kind === "harvest" ? 150 : 25;
        if (entered > limit && !window.confirm(`${entered} kg for ONE tree is unusually high (more than ${limit} kg). Is that right?`)) return;
      }
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
 * actual to. This finds the block's own most recent still-open plan
 * (status not yet "done") rather than making the user pick — plan
 * selection UI is a reasonable follow-up, not required here.
 *
 * Update: this used to throw ("No plan found — create one from the office
 * dashboard first") when the block had no plan yet, forcing a PIC Block
 * user to wait on a Manager before they could log anything in the field —
 * reported directly. Now auto-creates one instead, mirroring
 * apps/dashboard/views.py's own `_get_open_or_new_plan()` (the office
 * dashboard's "Record actual harvest" gained the exact same one-click,
 * no-plan-required behavior earlier). `quantityHint` seeds the new plan's
 * `assessment_quantity` (Harvest only — Fertilizer has no such field) from
 * whatever the field worker is about to report, better than a bare 0 when
 * nothing else is available; it's still just a placeholder estimate, not
 * a real assessment — a Manager can correct it from the office later.
 *
 * Uses a client-generated UUID and queueableWrite() the same as every
 * other offline-writable record here, so this works fully offline too —
 * HarvestViewSet/FertilizerViewSet are now idempotent-UUID-create
 * (apps/operations/views.py) specifically so a plan-creation write that's
 * replayed on reconnect updates in place rather than erroring.
 */
async function findOrCreatePlan(kind, quantityHint) {
  const store = kind === "harvest" ? "harvests" : "fertilizers";
  const plans = (await window.PalmVisionDB.getAll(store)).filter(
    (p) => p.farm_block === state.selectedBlockId && p.status !== "done"
  );
  plans.sort((a, b) => (a.assessment_date < b.assessment_date ? 1 : -1));
  if (plans.length) return plans[0];

  const path = kind === "harvest" ? "/api/harvests/" : "/api/fertilizers/";
  const payload = {
    id: crypto.randomUUID(),
    farm_block: state.selectedBlockId,
    assessment_date: new Date().toISOString().slice(0, 10),
    ...(kind === "harvest" ? { assessment_quantity: quantityHint || 0 } : {}),
  };
  const result = await window.PalmVisionAPI.queueableWrite(store, path, "POST", payload);
  const plan = result.response || payload;
  await window.PalmVisionDB.putOne(store, plan);
  return plan;
}

/** Every active tree in the current block still carrying the stage-1
 * "needs harvest/fertilizer" flag (Tree.harvest_marked_at/
 * fertilizer_marked_at's help_text, apps/trees/models.py) — what
 * submitBlockLevelActual() below auto-completes when a Manager-created
 * plan is chosen instead of "without a plan."
 */
async function getMarkedTreesForBlock(kind) {
  const markedField = kind === "harvest" ? "harvest_marked_at" : "fertilizer_marked_at";
  const allTrees = await window.PalmVisionDB.getAll("trees");
  return allTrees.filter((t) => t.farm_block === state.selectedBlockId && isActiveRecord(t) && t[markedField]);
}

/** Completes stage 2 for every tree in `trees` — TreeViewSet's
 * bulk_mark_harvested/bulk_mark_fertilized (apps/trees/views.py), the
 * same bulk-completion action the block map's own selection bar uses.
 * Requested directly: choosing an existing plan when logging a block
 * total should auto-complete every tree a Manager had flagged as needing
 * that action, not leave them sitting on the to-do list next to a plan
 * that's now been executed against.
 *
 * Online-only, not queued (window.PalmVisionAPI.onlineOnlyWrite, not
 * queueableWrite) — same docs/05-offline-sync.md § Conflict handling rule
 * every other "edit an existing record" action here already follows,
 * since this mutates existing Tree rows rather than appending a new one.
 * Deliberately called BEFORE the ActualHarvest/ActualFertilizer write in
 * submitBlockLevelActual() below and allowed to throw straight through —
 * if the device is offline, the actual never gets queued either, so
 * "actual logged, marked trees left untouched" is never a state this can
 * land in; it's both or neither.
 *
 * `totalQuantity` is split evenly across `trees` (never copied verbatim
 * into every row — see BUILD_STATUS.md's "Bulk quantity" fix for exactly
 * that bug in the office dashboard) and written back into IndexedDB from
 * the server's own response immediately, so blockScreen() stops showing
 * these trees as marked without waiting for the next sync.
 */
async function bulkMarkTreesDone(kind, trees, totalQuantity, date) {
  const path = kind === "harvest" ? "/api/trees/bulk_mark_harvested/" : "/api/trees/bulk_mark_fertilized/";
  const perTree = Math.round((totalQuantity / trees.length) * 100) / 100;
  const quantities = {};
  trees.forEach((t) => { quantities[t.id] = perTree; });
  const result = await window.PalmVisionAPI.onlineOnlyWrite(path, "POST", {
    tree_ids: trees.map((t) => t.id),
    quantities,
    date,
  });
  for (const updated of result.updated || []) {
    await window.PalmVisionDB.putOne("trees", updated);
  }
  if (result.skipped_tree_ids && result.skipped_tree_ids.length) {
    // Rare — e.g. a reassignment landed between opening this form and
    // saving it — but the trees that WERE in scope are already marked by
    // this point, and the actual harvest/fertilizer write still proceeds
    // right after this returns; same non-fatal heads-up wireBulkMarkButton()
    // above gives for the identical case on the block map's own bulk action.
    showError(
      `${result.skipped_tree_ids.length} marked tree${result.skipped_tree_ids.length === 1 ? "" : "s"} couldn't be auto-completed — they may no longer be assigned to you.`
    );
  }
}

/** The authoritative reported value for this form submission — weight_kg
 * (required) for harvest, quantity (kg, required) for fertilizer. Asked
 * directly to move every harvest record from bunch count to kg:
 * `quantity` on the harvest side is now optional supplementary bunch
 * count, read separately below, never used for the plan/bulk-mark math.
 */
function primaryValueFromForm(kind, fd) {
  return Number(fd.get(kind === "harvest" ? "weight_kg" : "quantity"));
}

async function submitBlockLevelActual(kind, fd) {
  const value = primaryValueFromForm(kind, fd);
  const bunchCount = kind === "harvest" && fd.get("quantity") ? Number(fd.get("quantity")) : null;
  const usingExistingPlan = fd.get("plan_mode") === "plan" && fd.get("plan_id");
  let plan;
  if (usingExistingPlan) {
    const planStore = kind === "harvest" ? "harvests" : "fertilizers";
    plan = await window.PalmVisionDB.getOne(planStore, fd.get("plan_id"));
    const markedTrees = await getMarkedTreesForBlock(kind);
    if (markedTrees.length) await bulkMarkTreesDone(kind, markedTrees, value, fd.get("date"));
  } else {
    plan = await findOrCreatePlan(kind, value);
  }
  const resource = kind === "harvest" ? "actual_harvests" : "actual_fertilizers";
  const path = kind === "harvest" ? "/api/actual-harvests/" : "/api/actual-fertilizers/";
  const payload = {
    id: crypto.randomUUID(),
    [kind]: plan.id,
    date: fd.get("date"),
    ...(kind === "harvest" ? { weight_kg: value, quantity: bunchCount } : { quantity: value }),
  };
  const result = await window.PalmVisionAPI.queueableWrite(resource, path, "POST", payload);
  await window.PalmVisionDB.putOne(resource, result.response || payload);
}

async function submitTreeLevelActual(kind, fd) {
  const plan = await findOrCreatePlan(kind, primaryValueFromForm(kind, fd));
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
    // weight_kg is the authoritative per-tree value for harvest (required
    // on the form); quantity (bunch count) is optional supplementary
    // detail. Fertilizer is unchanged — quantity (kg) only, no weight_kg
    // field exists on FertilizerTree.
    ...(kind === "harvest"
      ? { weight_kg: Number(fd.get("weight_kg")), quantity: fd.get("quantity") ? Number(fd.get("quantity")) : null }
      : { quantity: Number(fd.get("quantity")) }),
    ...(kind === "harvest" && state.location
      ? { recorded_location: { type: "Point", coordinates: [state.location.lon, state.location.lat] } }
      : {}),
  };
  const treeResult = await window.PalmVisionAPI.queueableWrite(treeResource, treePath, "POST", treePayload);
  await window.PalmVisionDB.putOne(treeResource, treeResult.response || treePayload);
}

// --- GPS watch ---------------------------------------------------------------

let _lastGeoRender = { at: 0, lat: null, lon: null, accuracy: null };

/** Whether a new fix is worth redrawing the screen for: the first fix, a move
 * of more than ~4m, a clearly different accuracy, or 15s since the last
 * redraw. Palm spacing is ~9m, so smaller jitter changes nothing on screen
 * and just burns battery/CPU re-rendering a 1,000-row list.
 */
function geoFixWorthRendering(loc) {
  const last = _lastGeoRender;
  if (last.lat === null) return true;
  const metres = window.PalmVisionGeo.haversineDistanceMeters(last.lat, last.lon, loc.lat, loc.lon);
  return (
    metres > 4 ||
    Math.abs((loc.accuracyM || 0) - (last.accuracy || 0)) > 10 ||
    Date.now() - last.at > 15000
  );
}

function startGeoWatch() {
  if (state.watchId != null) return;
  state.watchId = window.PalmVisionGeo.watchLocation(
    (loc) => {
      const first = !state.location;
      state.location = loc;
      state.locationError = null;
      if (["blocks", "block", "map"].includes(state.screen) && (first || geoFixWorthRendering(loc))) {
        _lastGeoRender = { at: Date.now(), lat: loc.lat, lon: loc.lon, accuracy: loc.accuracyM };
        render();
      }
    },
    (err) => {
      console.warn("Geolocation unavailable", err);
      state.locationError = err && err.code === 1
        ? "Location permission is off — turn it on in your browser settings to see which block you're in and get nearby-tree suggestions."
        : "Couldn't get a GPS fix. Move to open sky and try again; you can still work without location.";
      if (state.screen === "map") render();
    }
  );
}

function stopGeoWatch() {
  window.PalmVisionGeo.stopWatchingLocation(state.watchId);
  state.watchId = null;
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

  // GPS runs only while the page is visible (docs/05-offline-sync.md: "only
  // while the relevant screen is open, to avoid draining battery"). It used
  // to run at high accuracy for the whole session, even with the phone in a
  // pocket, and redraw the screen on every fix.
  startGeoWatch();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") startGeoWatch();
    else stopGeoWatch();
  });

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
