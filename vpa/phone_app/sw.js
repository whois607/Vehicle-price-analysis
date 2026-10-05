/* VPA phone - offline shell.

   Keeps the app's own files (not the data - that lives in IndexedDB) so VPA
   opens with no signal. Runs only on a secure address (HTTPS, or localhost):
   browsers do not allow service workers on plain http. The laptop fills in
   BUILD with a hash of the app files, so any change to them is a new worker
   that replaces this one on the next open. /api/ is never touched: sync
   always asks the laptop itself. */
const BUILD = "__BUILD__";
const CACHE = "vpa-shell-" + BUILD;
const SHELL = ["/", "/app.js", "/stats.js", "/app.css", "/manifest.webmanifest",
               "/icon-192.png", "/icon-512.png", "/icon.svg"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL.map(u => new Request(u, { cache: "reload" }))))
    .then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k.startsWith("vpa-shell-") && k !== CACHE)
    .map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")
      || url.pathname === "/ping") return;
  const key = e.request.mode === "navigate" ? "/" : url.pathname;
  e.respondWith(caches.open(CACHE).then(c => c.match(key).then(hit => hit || fetch(e.request))));
});
