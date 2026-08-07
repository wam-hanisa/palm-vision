/**
 * App-shell service worker. Two jobs only:
 *  1. Cache the PWA's own static assets so the app shell loads offline.
 *  2. NEVER intercept /api/ or /admin/ requests — those must always hit
 *     the network (or fail visibly), since serving a stale cached API
 *     response would be actively wrong (e.g. showing old JWT-protected
 *     data as if it were current). All actual offline data access goes
 *     through IndexedDB (pwa/js/db.js), not the HTTP cache.
 *
 * Registered at the site root (see palmvision/urls.py — served from
 * /service-worker.js, not /pwa/service-worker.js) so its scope covers the
 * whole origin; the fetch handler below is what keeps that safe.
 */

const CACHE_NAME = "palmvision-shell-v1";
const APP_SHELL = [
  "/field/",
  "/pwa/css/app.css",
  "/pwa/js/db.js",
  "/pwa/js/api.js",
  "/pwa/js/sync.js",
  "/pwa/js/geo.js",
  "/pwa/js/app.js",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Never cache-intercept the API or Django admin — always go to network.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/admin/")) {
    return;
  }

  if (event.request.method !== "GET") return;

  // Cache-first for the app shell, falling back to network (and caching
  // what comes back) for anything else same-origin — e.g. Leaflet tiles
  // are cross-origin and untouched by this handler entirely.
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request)
        .then((response) => {
          if (response.ok && url.origin === self.location.origin) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => cached); // offline and not in cache: let it fail naturally
    })
  );
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
