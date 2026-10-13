/**
 * App-shell service worker for the PWA field client. Two jobs only:
 *  1. Keep the field app's own files (/field/, /pwa/*, the manifest)
 *     available offline.
 *  2. Leave every other request alone — /api/ (live data must never come
 *     from an HTTP cache; offline data lives in IndexedDB, pwa/js/db.js),
 *     the office dashboard, /admin/, /media/.
 *
 * Registered at the site root (palmvision/urls.py serves it from
 * /service-worker.js), so its scope covers the whole origin, dashboard
 * included. That's why job 2 is an explicit allowlist below rather than a
 * short denylist: an earlier version intercepted every same-origin GET
 * except /api/ and /admin/ cache-first, which silently cached dashboard
 * pages (with their embedded data and CSRF token) and served them stale
 * forever.
 *
 * Network-first, not cache-first: online, every load revalidates against
 * the server (a cheap 304 when unchanged), so a deployed fix reaches field
 * devices on the next load instead of never — cache-first with a fixed
 * cache name meant new app.js/sync.js versions were never picked up.
 * Offline, or when the network is too slow to be usable in the field, the
 * cached copy is served.
 */

const CACHE_NAME = "palmvision-shell-v3";
const APP_SHELL = [
  "/field/",
  "/pwa/css/app.css",
  "/pwa/js/db.js",
  "/pwa/js/api.js",
  "/pwa/js/sync.js",
  "/pwa/js/geo.js",
  "/pwa/js/app.js",
  // Vendored webfont — must be precached, or the app falls back to a system
  // font in exactly the offline case this PWA is built for.
  "/pwa/fonts/public-sans-latin.woff2",
  "/manifest.webmanifest",
];
const NETWORK_TIMEOUT_MS = 4000;

function isFieldAppFile(url) {
  if (url.origin !== self.location.origin) return false;
  return (
    url.pathname.startsWith("/field/") ||
    url.pathname.startsWith("/pwa/") ||
    url.pathname === "/manifest.webmanifest"
  );
}

function fetchFresh(url) {
  // cache: "no-cache" makes the browser revalidate with the server instead
  // of trusting its own HTTP cache's guess at freshness.
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("network timeout")), NETWORK_TIMEOUT_MS);
    fetch(url, { cache: "no-cache", credentials: "same-origin" }).then(
      (response) => {
        clearTimeout(timer);
        resolve(response);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      }
    );
  });
}

async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetchFresh(request.url);
    if (response.ok) await cache.put(request.url, response.clone());
    return response;
  } catch (err) {
    const cached = await cache.match(request.url);
    if (cached) return cached;
    throw err;
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL.map((url) => new Request(url, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  // Deleting every other cache also purges the old "palmvision-shell-v2"
  // cache, which holds stale copies of dashboard pages.
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  if (!isFieldAppFile(new URL(event.request.url))) return;
  event.respondWith(networkFirst(event.request));
});

/**
 * Background Sync registration attempt — best-effort only. Safari (iOS and
 * macOS) does not implement the Background Sync API at all, so this event
 * simply never fires there; pwa/js/sync.js's 'online'/visibilitychange/
 * periodic triggers are the mechanism that actually has to work
 * everywhere. Where this DOES fire (Chrome/Edge on Android), it's a nice
 * latency improvement, not a load-bearing dependency.
 */
self.addEventListener("sync", (event) => {
  if (event.tag === "palmvision-outbox-flush") {
    event.waitUntil(
      self.clients.matchAll().then((clients) => {
        for (const client of clients) client.postMessage({ type: "FLUSH_OUTBOX" });
      })
    );
  }
});
