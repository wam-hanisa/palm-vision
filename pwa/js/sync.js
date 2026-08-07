/**
 * Sync engine: pulling server changes into IndexedDB, and replaying the
 * outbox against the API. See docs/05-offline-sync.md steps 3–5 and
 * apps/sync/views.py (SyncPullView) for the server side this talks to.
 *
 * Triggers: called on login (full pull), on 'online' event, on page
 * visibility change, and periodically while the app is open. There is no
 * server-push — this is deliberately poll/trigger-based, matching
 * docs/05-offline-sync.md's "background sync fires ... replaying the
 * queue" without assuming the Background Sync API is available (it isn't,
 * on iOS Safari — see service-worker.js for the registration attempt and
 * why this file doesn't depend on it succeeding).
 */

const SYNC_KEY_TO_STORE = {
  farm_blocks: "farm_blocks",
  trees: "trees",
  harvests: "harvests",
  actual_harvests: "actual_harvests",
  harvest_trees: "harvest_trees",
  fertilizers: "fertilizers",
  actual_fertilizers: "actual_fertilizers",
  fertilizer_trees: "fertilizer_trees",
};

let _syncing = false;
const _listeners = [];

/** A stable per-browser identifier, generated once and cached in
 * IndexedDB's meta store — this is what lets the server tell "device A
 * still has 12 pending items" apart from "device B just checked in with
 * zero." Not tied to a user login (a shared field tablet could see
 * multiple users), which is fine — apps/sync/models.py's SyncCheckIn keys
 * on (user, device_id) together, not device_id alone.
 */
async function getOrCreateDeviceId() {
  let id = await window.PalmVisionDB.getMeta("device_id");
  if (!id) {
    id = crypto.randomUUID();
    await window.PalmVisionDB.setMeta("device_id", id);
  }
  return id;
}

/** Best-effort report of outbox size to the server — see
 * apps/sync/models.py's SyncCheckIn docstring for why this exists at all
 * (the server otherwise has zero visibility into a client's local outbox).
 * Deliberately fire-and-forget: a failed check-in shouldn't block or fail
 * the sync cycle it's reporting on — worst case, the server's "stalled
 * sync" signal for this device is just as stale as it would've been
 * without this call succeeding.
 */
async function reportCheckIn(pendingCount) {
  try {
    const deviceId = await getOrCreateDeviceId();
    await window.PalmVisionAPI.apiFetch("/api/sync/checkin/", {
      method: "POST",
      body: JSON.stringify({ device_id: deviceId, pending_count: pendingCount }),
    });
  } catch (err) {
    console.warn("Sync check-in failed (non-fatal)", err);
  }
}

function onSyncStatusChange(fn) {
  _listeners.push(fn);
}

function _notify(status) {
  for (const fn of _listeners) fn(status);
}

/** Pull everything changed since the last successful pull (or everything,
 * on first run) and write it into IndexedDB. Farms deliberately omitted
 * from local storage here — the field client only ever needs blocks/trees/
 * plans for navigation, not the Farm record itself.
 */
async function pullChanges() {
  const since = await window.PalmVisionDB.getMeta("sync_cursor");
  const query = since ? `?since=${encodeURIComponent(since)}` : "";
  const response = await window.PalmVisionAPI.apiFetch(`/api/sync/pull/${query}`);
  if (!response.ok) {
    throw new Error(`Pull failed with status ${response.status}`);
  }
  const data = await response.json();

  for (const [key, storeName] of Object.entries(SYNC_KEY_TO_STORE)) {
    const records = data[key] || [];
    if (records.length) await window.PalmVisionDB.putAll(storeName, records);
  }

  await window.PalmVisionDB.setMeta("sync_cursor", data.server_time);
  await window.PalmVisionDB.setMeta("last_synced_at", new Date().toISOString());
  return data;
}

/** Replay queued outbox writes, oldest first, stopping on the first
 * network failure (later entries likely depend on network too, so there's
 * no point burning through them one by one — try again on the next
 * trigger). A queued entry that comes back as a real validation error
 * (4xx) is left in the outbox with the error attached rather than
 * silently dropped, per docs/05-offline-sync.md step 5: "failures ...
 * surface to the user ... never silently."
 */
async function flushOutbox() {
  const entries = await window.PalmVisionDB.getOutboxEntries();
  let synced = 0;
  let failed = 0;

  for (const entry of entries) {
    if (entry.status === "failed") continue; // needs user attention, not auto-retried
    try {
      const response = await window.PalmVisionAPI.apiFetch(entry.path, {
        method: entry.method,
        body: JSON.stringify(entry.body),
      });
      if (response.ok) {
        await window.PalmVisionDB.deleteOutboxEntry(entry.outboxId);
        synced += 1;
      } else if (response.status >= 500) {
        // Transient — leave as pending, try again next trigger.
        await window.PalmVisionDB.updateOutboxEntry(entry.outboxId, {
          attempts: entry.attempts + 1,
          lastError: `Server error ${response.status}`,
        });
        break; // stop here, likely to keep failing for the rest of the batch too
      } else {
        const body = await response.json().catch(() => ({}));
        await window.PalmVisionDB.updateOutboxEntry(entry.outboxId, {
          status: "failed",
          attempts: entry.attempts + 1,
          lastError: JSON.stringify(body),
        });
        failed += 1;
      }
    } catch (err) {
      if (err.isNetworkError) break; // offline again — stop, don't mark as failed
      await window.PalmVisionDB.updateOutboxEntry(entry.outboxId, {
        status: "failed",
        attempts: entry.attempts + 1,
        lastError: String(err),
      });
      failed += 1;
    }
  }

  return { synced, failed, remaining: (await window.PalmVisionDB.getOutboxEntries()).length };
}

/** The single entry point everything else calls: flush queued writes
 * first (so the subsequent pull reflects what this device just sent, not
 * a stale server view), then pull. Guarded against overlapping runs.
 */
async function runSync() {
  if (_syncing) return { skipped: true };
  if (!navigator.onLine) return { skipped: true, reason: "offline" };

  _syncing = true;
  _notify("syncing");
  try {
    const flushResult = await flushOutbox();
    const pullResult = await pullChanges();
    _notify("idle");
    await reportCheckIn(flushResult.remaining);
    return { flushResult, pullResult };
  } catch (err) {
    _notify("error");
    // Still check in even on failure — a device stuck offline with a
    // growing outbox is exactly the case the server-side alert needs to
    // catch, and this call itself only succeeds if we're online anyway.
    const remaining = (await window.PalmVisionDB.getOutboxEntries()).length;
    await reportCheckIn(remaining);
    throw err;
  } finally {
    _syncing = false;
  }
}

window.PalmVisionSync = { runSync, pullChanges, flushOutbox, onSyncStatusChange };

window.addEventListener("online", () => {
  window.PalmVisionSync.runSync().catch((err) => console.error("Sync on reconnect failed", err));
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    window.PalmVisionSync.runSync().catch((err) => console.error("Sync on foreground failed", err));
  }
});

// service-worker.js's 'sync' event handler (Background Sync API, where
// supported) posts this message to trigger a flush from here — the
// service worker itself never touches IndexedDB or the API directly.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.addEventListener("message", (event) => {
    if (event.data && event.data.type === "FLUSH_OUTBOX") {
      window.PalmVisionSync.runSync().catch((err) => console.error("Sync on background-sync trigger failed", err));
    }
  });
}
