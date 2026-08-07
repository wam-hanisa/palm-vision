/**
 * IndexedDB wrapper — the offline datastore described in
 * docs/05-offline-sync.md: "all data entry writes to IndexedDB immediately
 * ... each local write also gets added to a sync queue."
 *
 * Object stores mirror the offline-relevant server resources 1:1 (see
 * apps/sync/views.py _SYNC_RESOURCES for the server-side equivalent list),
 * plus two PWA-only stores: `meta` (auth tokens, sync cursor) and `outbox`
 * (queued writes awaiting replay).
 *
 * No framework, no IndexedDB wrapper library — CLAUDE.md is explicit that
 * the field client is vanilla JS. This file is the only place raw
 * `indexedDB.*` calls should appear; everything else in pwa/js/ goes
 * through the functions here.
 */

const DB_NAME = "palmvision";
const DB_VERSION = 1;

const RECORD_STORES = [
  "farm_blocks",
  "trees",
  "harvests",
  "actual_harvests",
  "harvest_trees",
  "fertilizers",
  "actual_fertilizers",
  "fertilizer_trees",
];

let _dbPromise = null;

function openDb() {
  if (_dbPromise) return _dbPromise;
  _dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);

    request.onupgradeneeded = (event) => {
      const db = event.target.result;

      for (const storeName of RECORD_STORES) {
        if (!db.objectStoreNames.contains(storeName)) {
          db.createObjectStore(storeName, { keyPath: "id" });
        }
      }

      if (!db.objectStoreNames.contains("meta")) {
        db.createObjectStore("meta", { keyPath: "key" });
      }

      if (!db.objectStoreNames.contains("outbox")) {
        // Auto-incrementing key, NOT the record's own UUID — several queued
        // writes can target the same record UUID over time (e.g. a tree
        // created, then edited, while still offline), and outbox order is
        // what preserves "replay in the order they happened."
        const outbox = db.createObjectStore("outbox", { keyPath: "outboxId", autoIncrement: true });
        outbox.createIndex("status", "status", { unique: false });
      }
    };

    request.onsuccess = (event) => resolve(event.target.result);
    request.onerror = (event) => reject(event.target.error);
  });
  return _dbPromise;
}

function runTx(storeNames, mode, work) {
  return openDb().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(storeNames, mode);
        let result;
        tx.oncomplete = () => resolve(result);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error || new Error("IndexedDB transaction aborted"));
        Promise.resolve(work(tx)).then((r) => {
          result = r;
        }, reject);
      })
  );
}

/** Replace every record in `storeName` matching the given ids with fresh
 * server data — used by sync.js when applying a pull response. Records not
 * mentioned aren't touched (an incremental pull only lists what changed).
 */
function putAll(storeName, records) {
  return runTx([storeName], "readwrite", (tx) => {
    const store = tx.objectStore(storeName);
    for (const record of records) store.put(record);
  });
}

function getAll(storeName) {
  return runTx([storeName], "readonly", (tx) => {
    return new Promise((resolve, reject) => {
      const req = tx.objectStore(storeName).getAll();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  });
}

function getOne(storeName, id) {
  return runTx([storeName], "readonly", (tx) => {
    return new Promise((resolve, reject) => {
      const req = tx.objectStore(storeName).get(id);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  });
}

function putOne(storeName, record) {
  return runTx([storeName], "readwrite", (tx) => tx.objectStore(storeName).put(record));
}

function getMeta(key) {
  return runTx(["meta"], "readonly", (tx) => {
    return new Promise((resolve, reject) => {
      const req = tx.objectStore("meta").get(key);
      req.onsuccess = () => resolve(req.result ? req.result.value : null);
      req.onerror = () => reject(req.error);
    });
  });
}

function setMeta(key, value) {
  return runTx(["meta"], "readwrite", (tx) => tx.objectStore("meta").put({ key, value }));
}

/** Queue a write for replay. `method`/`path`/`body` mirror a fetch() call
 * against the Django API. Returns the assigned outboxId.
 */
function enqueueOutbox(entry) {
  return runTx(["outbox"], "readwrite", (tx) => {
    return new Promise((resolve, reject) => {
      const req = tx.objectStore("outbox").add({
        ...entry,
        status: "pending",
        createdAt: new Date().toISOString(),
        attempts: 0,
        lastError: null,
      });
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  });
}

function getOutboxEntries() {
  return runTx(["outbox"], "readonly", (tx) => {
    return new Promise((resolve, reject) => {
      const req = tx.objectStore("outbox").getAll();
      req.onsuccess = () => resolve(req.result.sort((a, b) => a.outboxId - b.outboxId));
      req.onerror = () => reject(req.error);
    });
  });
}

function updateOutboxEntry(outboxId, patch) {
  return runTx(["outbox"], "readwrite", (tx) => {
    return new Promise((resolve, reject) => {
      const store = tx.objectStore("outbox");
      const getReq = store.get(outboxId);
      getReq.onsuccess = () => {
        const existing = getReq.result;
        if (!existing) return resolve(null);
        store.put({ ...existing, ...patch });
        resolve(null);
      };
      getReq.onerror = () => reject(getReq.error);
    });
  });
}

function deleteOutboxEntry(outboxId) {
  return runTx(["outbox"], "readwrite", (tx) => tx.objectStore("outbox").delete(outboxId));
}

window.PalmVisionDB = {
  RECORD_STORES,
  putAll,
  getAll,
  getOne,
  putOne,
  getMeta,
  setMeta,
  enqueueOutbox,
  getOutboxEntries,
  updateOutboxEntry,
  deleteOutboxEntry,
};
