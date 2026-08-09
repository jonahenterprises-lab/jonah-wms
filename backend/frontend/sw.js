// App-shell cache so the UI still loads with no signal — a worker mid-install
// with patchy mobile data can at least open the app and see a friendly error
// instead of a blank white screen. API calls always go straight to the network:
// this never serves stale attendance/report data, only the static shell.
const CACHE_NAME = "jonah-wms-shell-v1";
const SHELL_FILES = [
  "/",
  "/styles.css",
  "/manifest.json",
  "/js/main.js",
  "/js/api.js",
  "/js/utils.js",
  "/js/auth.js",
  "/js/admin.js",
  "/js/worker.js",
  "/vendor/leaflet/leaflet.js",
  "/vendor/leaflet/leaflet.css",
  "/icon-192.png",
  "/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_FILES))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/")) {
    return; // never intercept API calls — always live, never cached
  }
  // Network-first so a deploy is visible immediately when online; cache is
  // strictly a fallback for when the network fails.
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return res;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || caches.match("/")))
  );
});
