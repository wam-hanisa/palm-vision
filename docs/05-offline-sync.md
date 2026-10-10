# Offline field access & geo-navigation

## Why a PWA, not a native app

A Progressive Web App, not Flutter/React Native, for the field client:

- No app store submission/approval process — installs via "Add to Home Screen" directly from a browser link.
- One codebase for Android and iOS browsers.
- Genuinely works offline via a service worker (caches the app, intercepts network requests) plus IndexedDB (local storage) — the same pattern used by Google Maps, Twitter/X, and other offline-capable web apps. The service worker only handles the field app's own files (`/field/`, `/pwa/*`, the manifest), network-first with the cached copy as the offline fallback; it never touches `/api/`, the office dashboard, or `/media/`, and data never comes from its cache (that's IndexedDB's job).

**Trade-off to know about:** a native app gives smoother camera/GPS integration. Start with the PWA; only move to native if it proves genuinely insufficient in practice — building native first is more expensive up front for a benefit that might not be needed.

**Note on the offline logic itself:** the service worker, local database, and sync queue must run as JavaScript inside the phone's browser — that's a constraint of how browsers work, not a technology choice. The backend stays 100% Python/Django.

## How offline capture works

1. Before heading to the field (while online), the PWA pre-caches the PIC Block user's assigned block(s): trees, boundaries, open harvest/fertilizer plans, into IndexedDB.
2. In the field, all data entry writes to IndexedDB immediately and shows in the UI as normal — the user doesn't need to know they're offline.
3. Each local write also gets added to a sync queue (an ordered list of pending changes, stored locally).
4. When connectivity returns, the service worker's background sync fires automatically, replaying the queue against the API in order.
5. Successfully synced items are marked complete and removed; failures (e.g. a validation error) surface to the user the next time they open the app — never silently.

Photos/imagery captured in the field are stored locally and uploaded as part of the same queue once online — large files just take longer, they don't block the rest of the queue.

## Conflict handling

This domain is naturally sync-friendly: "actual harvest" and "actual fertilizer" records are append-only (each field visit creates a new record), and each `FarmBlock` has one PIC Block user. The practical rule:

- Every synced record carries a client-generated UUID, so two offline devices never collide on ID.
- The server treats sync as "append this record," not "merge this edit."
- **Editing an existing record is an online-only action**, not part of the offline queue — this deliberately avoids the harder class of conflict-resolution problems rather than half-solving them.
- `Harvest`/`Fertilizer` plan *creation* is queueable too, same as the append-only records above (`HarvestViewSet`/`FertilizerViewSet` are idempotent-UUID-create, apps/operations/views.py) — see "Logging without an existing plan" below for why a PIC Block's device needs this.

## Farm → Block → Tree geo-navigation

Three-tier drill-down using the phone's GPS (browser Geolocation API):

1. **Farm level** — map of all blocks as polygons. The user's live position is shown; if it falls inside a block's boundary, that block is highlighted as "you are here" and offered as the obvious next tap (any block can still be selected manually).
2. **Block level** — a plain list by default (`blockScreen()`), plus an optional "Map view" (`blockMapScreen()`) showing the selected block's boundary and every tree in it as a pin, colored/ringed by health and harvest/fertilizer stage — from pre-cached offline data, live GPS marker continues updating. Tapping a pin on the map also toggles that tree into a bulk-selection set; once at least one tree is selected, a bottom action bar offers "Mark harvested"/"Mark fertilized" against the whole selection in one request (`TreeViewSet.bulk_mark_harvested`/`bulk_mark_fertilized`, apps/trees/views.py) — the field-app counterpart to the office dashboard's own bulk "Mark trees harvested/fertilized" action. Like the single-tree mark_harvested/mark_fertilized actions, this is online-only, not queued (see "Editing an existing record" above). The plain list (`blockScreen()`) sorts trees with a stage-1 "needs harvest/fertilizer" flag first, alphabetical within each group — requested directly, so a block's to-do trees aren't buried among however many others the block has.
3. **Tree level** — tapping a tree in the list (or via the proximity prompt below) opens that tree's detail/yield-entry screen.

## Logging without an existing plan

Block-level and tree-level yield entry both attach to an open `Harvest`/`Fertilizer` plan (`findOrCreatePlan()`, pwa/js/app.js) — originally this required a Manager to have already created one from the office dashboard, blocking a PIC Block from logging anything against a brand-new block until office staff caught up. Reported directly. Now: if the block has no still-open plan (status not yet `done`), the field app creates a minimal one itself — client-generated UUID, `assessment_date` today, `assessment_quantity` seeded from whatever quantity the field worker is about to report (Harvest only; `Fertilizer` has no such field) rather than a bare 0 — the same one-click, no-plan-required behavior the office dashboard's own "Record actual harvest" already has (apps/dashboard/views.py's `_get_open_or_new_plan()`). This is a placeholder estimate, not a real assessment; a Manager can still create/edit the plan's own fields from the office afterward, same as any auto-created plan.

## Logging against an existing plan auto-completes its marked trees

Block-level entry (not tree-level — that flow already targets one specific tree by hand) also offers a choice: log without a plan (the auto-create behavior above), or against one of the block's own open plans, picked from a dropdown. Requested directly: choosing an existing plan should auto-complete every tree in the block still carrying the stage-1 "needs harvest/fertilizer" flag, not leave a Manager's to-do list untouched next to a plan that's now genuinely been executed against.

Mechanically, `submitBlockLevelActual()` (pwa/js/app.js) calls `TreeViewSet.bulk_mark_harvested`/`bulk_mark_fertilized` (apps/trees/views.py) for those trees — **online-only, not queued**, same as every other "edit an existing record" action in this app (see "Conflict handling" above), since this mutates existing `Tree` rows rather than appending a new one. It's called *before* the `ActualHarvest`/`ActualFertilizer` write and allowed to throw straight through on failure (including being offline) — the actual is never even queued in that case, so "actual logged, marked trees left untouched" is never a state this can land in; it's both or neither. The block-level total is split evenly across whichever trees get auto-completed (`total ÷ tree count`, rounded to 2 decimals) — never the same figure copied verbatim into every tree, the exact bug BUILD_STATUS.md documents fixing for the office dashboard's own bulk-quantity field.

## Proximity prompt for yield entry

While a field worker is on the block-level screen with an open harvest plan, the app compares live GPS position against cached tree locations. Within a threshold distance of a specific tree, a lightweight prompt surfaces: "Near Tree #245 — log yield?"

**Hard rules, not suggestions:**

- Phone GPS accuracy is typically 3–10 meters in open conditions, worse under palm canopy (signal multipath). This is close to or larger than typical tree spacing, so a single "nearest tree" guess can genuinely be wrong.
- **Never auto-submit against a guessed tree.** Show the nearest 2–3 candidates and require the worker to confirm which tree they're actually at before saving anything.
- Manual search/selection must always remain available as a fallback.
- `watchPosition` (continuous GPS polling) should only run while the relevant screen is open — turn it off on navigation away, to avoid draining battery.

## Yield granularity: block level and tree level, both supported

`HarvestTree` (see `03-data-model.md`) carries `quantity`, `weight_kg`, `recorded_location`, `recorded_by`, `recorded_at` per tree. `ActualHarvest.quantity` stays as a manually-entered block-level total when no tree-level rows exist for that harvest event; when tree-level rows do exist, the total is derived (summed), never entered separately — so the two modes can't silently disagree. Which mode applies to a given harvest event is just a fact about whether `HarvestTree` rows carry values, not a separate flag to maintain.
