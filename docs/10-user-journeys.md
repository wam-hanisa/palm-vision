# User journeys

Two journeys, covering the two fundamentally different ways this system gets used. No Super Admin journey — user/role/farm administration is comparatively straightforward CRUD without the same design tension as the other two.

## PIC Block — logging a harvest in the field

1. **Pre-trip sync (office, online).** Opens the PWA before leaving; pre-caches assigned block's trees, boundaries, and open plans into local storage. This is the one step needing a live connection.
2. **Travel to the block (offline).** No signal needed — farm-level map shows live GPS position against block boundaries.
3. **Near a tree (offline).** Proximity prompt surfaces the nearest 2–3 trees, not a guess — worker confirms which tree they're at.
4. **Logging yield (offline).** Quantity and weight, saved locally, queued with a client-generated UUID.
5. **Spots a tree not in the system.** Adds it directly — full tree CRUD, scoped to their own block, works offline the same way yield logging does.
6. **Back in range (online).** Background sync fires automatically; failures surface clearly rather than vanishing.

**What this replaces:** paper logs or memory-based reporting, where errors and delays currently creep in — the worker has a synced record the moment they're back in range, not at end of day or week.

## Manager — approvals, oversight, and compliance

1. **Morning digest.** Notification summarizing pending approvals, health anomalies, compliance expiry — pull becomes push.
2. **Approves a harvest plan.** Reviews tree-level detail behind the total, approves — a real, enforced authorization check runs here.
3. **Reviews the detection queue.** Confirms genuine new plantings, rejects false positives from a recent drone flight.
4. **Spots a health trend.** From classification history across several flights, not a single snapshot.
5. **Renews a compliance document.** Uploads a renewal before it lapses, not after an audit catches it.
6. **Exports a report.** Pulls a monthly production report to share with ownership or an auditor — a real file, not a dashboard screenshot.

**What this replaces:** a dashboard with no approval enforcement and no proactive signal — this surfaces problems before they compound, and makes approval a real checkpoint.
