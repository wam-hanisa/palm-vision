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

/** Every other string this app interpolates into markup goes into a text
 * node (never HTML-escaped elsewhere in this file, deliberately — an
 * internal field-crew tool, not public-internet content), which is safe
 * from *breaking* the surrounding markup even though it isn't XSS-safe. An
 * HTML attribute value is different: an unescaped literal `"` in a tree's
 * name (a real possibility — free-text field data) would terminate the
 * attribute early and corrupt the tag, not just render oddly. Used only
 * where a value is interpolated inside a quoted attribute — see
 * treeDetailScreen()'s edit form.
 */
function escapeAttr(value) {
  return String(value).replace(/&/g, "&amp;").replace(/"/g, "&quot;");
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

function sortTrees(trees) {
  return [...trees].sort((a, b) => TREE_COLLATOR.compare(a.name || "", b.name || ""));
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
function renderUnsyncedBanner(outboxCount) {
  if (!outboxCount) return "";
  const itemWord = outboxCount === 1 ? "item" : "items";
  const offlineNote = navigator.onLine ? "" : " — will send automatically once you're back online";
  return `
    <div class="unsynced-banner" id="unsynced-banner">
      <span>${outboxCount} ${itemWord} waiting to sync${offlineNote}.</span>
      <button type="button" id="sync-now-btn" ${navigator.onLine ? "" : "disabled"}>Sync now</button>
    </div>`;
}

async function updateSyncChip() {
  const chip = document.getElementById("sync-chip");
  const outbox = await window.PalmVisionDB.getOutboxEntries();
  if (chip) chip.outerHTML = renderSyncChip(window._lastSyncStatus || "idle", outbox.length);

  const banner = document.getElementById("unsynced-banner");
  const newBannerHtml = renderUnsyncedBanner(outbox.length);
  if (banner) {
    if (newBannerHtml) banner.outerHTML = newBannerHtml;
    else banner.remove(); // queue just cleared — don't leave a stale "0 items" banner behind
  } else if (newBannerHtml && chip) {
    chip.insertAdjacentHTML("afterend", newBannerHtml);
  }
  wireSyncNowButton();
}

function wireSyncNowButton() {
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
      ${state.error ? `<p class="error" role="alert">${state.error}</p>` : ""}
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
function buildMapProjection(blocks, location) {
  const points = [];
  blocks.forEach((block) => {
    if (block.boundary && block.boundary.coordinates && block.boundary.coordinates[0]) {
      points.push(...block.boundary.coordinates[0]);
    }
  });
  if (location) points.push([location.lon, location.lat]);
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
          <g class="map-block" data-block-id="${block.id}">
            <path class="map-block-shape ${isHere ? "map-block-shape--here" : ""}" d="M ${pathPoints} Z"></path>
            <text class="map-block-label" x="${labelX}" y="${labelY - 4}">${block.name}</text>
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
          ? `<p class="pill pill--accent">You are in: ${here.name}</p>`
          : state.location
          ? `<p class="muted">You're not currently inside a mapped block boundary.</p>`
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
  const trees = sortTrees(allTrees.filter((t) => t.farm_block === block.id && isActiveRecord(t)));

  const proximity = state.location ? window.PalmVisionGeo.proximityCandidate(state.location.lat, state.location.lon, trees) : null;

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
          ${needsHarvest.length ? `<span class="pill pill--accent">${needsHarvest.length} tree${needsHarvest.length === 1 ? "" : "s"} need harvest</span>` : ""}
          ${needsFertilizer.length ? `<span class="pill pill--accent">${needsFertilizer.length} tree${needsFertilizer.length === 1 ? "" : "s"} need fertilizer</span>` : ""}
        </div>`
      : "";

  const treeItems = trees
    .map((tree) => {
      const badges = [
        tree.harvest_marked_at ? '<span class="pill pill--accent">Needs harvest</span>' : "",
        tree.fertilizer_marked_at ? '<span class="pill pill--accent">Needs fertilizer</span>' : "",
      ].join("");
      return `
      <li class="tree-item" data-tree-id="${tree.id}">
        <div class="tree-item__name">${tree.name || "Unnamed tree"}</div>
        <div class="tree-item__meta">${tree.current_health} ${badges}</div>
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
        <h1>${block.name}</h1>
      </header>

      ${attentionBanner}

      ${
        proximity
          ? `<div class="proximity-prompt" data-tree-id="${proximity.tree.id}">
               <p class="proximity-prompt__text">You are ${Math.round(proximity.distanceM)}m from <strong>${proximity.tree.name || "a tree"}</strong>. Log a harvest against it?</p>
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
        treeItems
          ? `<ul class="tree-list">${treeItems}</ul>`
          : `<div class="empty-state">
               <span class="empty-state__title">No trees in this block yet</span>
               <p class="empty-state__hint">Use “+ Add tree” above to record the first one. It saves on this device straight away, even with no signal.</p>
             </div>`
      }
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
      ${state.error ? `<p class="error" role="alert">${state.error}</p>` : ""}
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
  return isoString ? new Date(isoString).toLocaleDateString() : "never";
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
  const when = new Date(isoDate).toLocaleDateString();
  return quantity === null || quantity === undefined ? when : `${when} (${quantity} ${unit})`;
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
  const unit = kind === "harvest" ? "bunches" : "kg";
  const assignedLine = `<span class="pill pill--accent">Needs ${label} — assigned ${formatDateOrNever(markedAt)}</span>`;

  if (state.quickMarkOpenKind !== kind) {
    return `
      <div class="mark-banner">
        ${assignedLine}
        <button id="mark-${kind}-btn" data-tree-id="${tree.id}" ${navigator.onLine ? "" : "disabled"}>Mark ${doneVerb}</button>
      </div>`;
  }

  return `
    <div class="mark-banner mark-banner--open">
      ${assignedLine}
      <form id="mark-${kind}-form" data-tree-id="${tree.id}" data-kind="${kind}">
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
  const healthLabels = { unknown: "Unknown", healthy: "Healthy", small: "Small", yellow: "Yellow" };
  const needsAnyAction = Boolean(tree.harvest_marked_at || tree.fertilizer_marked_at);
  const offlineNotice = !navigator.onLine
    ? `<p class="pill pill--warning">You appear to be offline — saving${needsAnyAction ? " or marking as done" : " an edit"} needs a live connection (nothing here is queued, so this doesn't risk overwriting a change made elsewhere while you were offline).</p>`
    : "";

  return `
    <div class="screen screen--tree-detail">
      <header class="screen-header">
        <button id="back-btn" class="btn-text btn-text--back">&larr; Block</button>
        <h1>${tree.name || "Unnamed tree"}</h1>
      </header>

      <p class="pill ${tree.status === "removed" ? "pill--warning" : "pill--accent"}">${tree.status === "removed" ? "Removed" : "Active"}</p>
      <p class="muted">Health: ${healthLabels[tree.current_health] || tree.current_health} &middot; Source: ${tree.source === "drone_detected" ? "Drone detected" : "Manual"}</p>
      <p class="muted">Pin: ${lat.toFixed(6)}, ${lon.toFixed(6)}</p>
      <p class="muted">Last harvested: ${formatLastDone(tree.last_harvested_at, tree.last_harvest_quantity, "bunches")} &middot; Last fertilized: ${formatLastDone(tree.last_fertilized_at, tree.last_fertilizer_quantity, "kg")}</p>

      ${offlineNotice}
      ${markBanner(tree, "harvest")}
      ${markBanner(tree, "fertilizer")}

      <div class="action-row">
        <button id="log-harvest-tree-btn" data-tree-id="${tree.id}">Log harvest here</button>
        <button id="log-fertilizer-tree-btn" data-tree-id="${tree.id}">Log fertilizer here</button>
      </div>

      <h2>Edit</h2>
      <form id="tree-edit-form">
        <label>Name<input name="name" value="${escapeAttr(tree.name || "")}"></label>
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
      ${state.error ? `<p class="error" role="alert">${state.error}</p>` : ""}
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
        <label>Quantity<input name="quantity" type="number" step="0.01" inputmode="decimal" required></label>
        ${kind === "harvest" ? '<label>Weight (kg, optional)<input name="weight_kg" type="number" step="0.01" inputmode="decimal"></label>' : ""}
        <button type="submit">Save</button>
      </form>
      ${state.error ? `<p class="error" role="alert">${state.error}</p>` : ""}
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
    case "map":
      body = await mapScreen();
      break;
    case "treeForm":
      body = treeFormScreen();
      break;
    case "treeDetail":
      body = await treeDetailScreen();
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
  const chip = state.screen === "login" ? "" : renderSyncChip(window._lastSyncStatus || "idle", outbox.length);
  const banner = state.screen === "login" ? "" : renderUnsyncedBanner(outbox.length);
  el.innerHTML = chip + banner + body;
  wireScreenEvents();
  wireSyncNowButton();
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

  document.querySelectorAll(".block-item").forEach((li) => {
    li.addEventListener("click", () => setScreen("block", { selectedBlockId: li.dataset.blockId }));
  });

  const mapBtn = document.getElementById("map-btn");
  if (mapBtn) mapBtn.addEventListener("click", () => setScreen("map"));

  document.querySelectorAll(".map-block").forEach((g) => {
    g.addEventListener("click", () => setScreen("block", { selectedBlockId: g.dataset.blockId }));
  });

  const backBtn = document.getElementById("back-btn");
  if (backBtn) {
    backBtn.addEventListener("click", () => {
      if (state.screen === "block" || state.screen === "map") setScreen("blocks");
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
      if (["blocks", "block", "map"].includes(state.screen)) render();
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
