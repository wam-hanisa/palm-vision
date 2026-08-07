# Offline field access & geo-navigation

## Why a PWA, not a native app

A Progressive Web App, not Flutter/React Native, for the field client:

- No app store submission/approval process — installs via "Add to Home Screen" directly from a browser link.
- One codebase for Android and iOS browsers.
- Genuinely works offline via a service worker (caches the app, intercepts network requests) plus IndexedDB (local storage) — the same pattern used by Google Maps, Twitter/X, and other offline-capable web apps.

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

## Farm → Block → Tree geo-navigation

Three-tier drill-down using the phone's GPS (browser Geolocation API):

1. **Farm level** — map of all blocks as polygons. The user's live position is shown; if it falls inside a block's boundary, that block is highlighted as "you are here" and offered as the obvious next tap (any block can still be selected manually).
2. **Block level** — the selected block's boundary plus every tree in it, from pre-cached offline data. Live GPS marker continues updating.
3. **Tree level** — tapping a tree (or via the proximity prompt below) opens that tree's detail/yield-entry screen.

## Proximity prompt for yield entry

While a field worker is on the block-level screen with an open harvest plan, the app compares live GPS position against cached tree locations. Within a threshold distance of a specific tree, a lightweight prompt surfaces: "Near Tree #245 — log yield?"

**Hard rules, not suggestions:**

- Phone GPS accuracy is typically 3–10 meters in open conditions, worse under palm canopy (signal multipath). This is close to or larger than typical tree spacing, so a single "nearest tree" guess can genuinely be wrong.
- **Never auto-submit against a guessed tree.** Show the nearest 2–3 candidates and require the worker to confirm which tree they're actually at before saving anything.
- Manual search/selection must always remain available as a fallback.
- `watchPosition` (continuous GPS polling) should only run while the relevant screen is open — turn it off on navigation away, to avoid draining battery.

## Yield granularity: block level and tree level, both supported

`HarvestTree` (see `03-data-model.md`) carries `quantity`, `weight_kg`, `recorded_location`, `recorded_by`, `recorded_at` per tree. `ActualHarvest.quantity` stays as a manually-entered block-level total when no tree-level rows exist for that harvest event; when tree-level rows do exist, the total is derived (summed), never entered separately — so the two modes can't silently disagree. Which mode applies to a given harvest event is just a fact about whether `HarvestTree` rows carry values, not a separate flag to maintain.
