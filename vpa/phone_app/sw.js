/* VPA phone - offline shell.

   Keeps the app's own files (not the data - that lives in IndexedDB) so VPA
   opens with no signal. Runs only on a secure address (HTTPS, or localhost):
   browsers do not allow service workers on plain http. The laptop fills in
   BUILD with a hash of the app files, so any change to them is a new worker
   that replaces this one on the next open. /api/ is never touched: sync
   always asks the laptop itself.

   Listing photos: a photo the app has shown is kept here (its own cache,
   which survives app updates, oldest dropped past PHOTO_MAX), so a car
   already looked at shows its photo with no signal. Nothing is fetched
   that the app did not put on screen. */
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
const PHOTOS = "vpa-photos", PHOTO_MAX = 600;
const isPhoto = url => /^[a-z]{3,20}\.pxcrush\.net$/.test(url.hostname) && url.protocol === "https:";
async function photo(req) {
  const c = await caches.open(PHOTOS), hit = await c.match(req.url);
  if (hit) return hit;
  let res;
  try {               // a CORS copy: stored at its real size, not padded like an opaque one
    res = await fetch(req.url, { mode: "cors", credentials: "omit", referrerPolicy: "no-referrer" });
  } catch (err) {
    return fetch(req);
  }
  if (res.ok) {
    await c.put(req.url, res.clone());
    const ks = await c.keys();
    for (const k of ks.slice(0, Math.max(0, ks.length - PHOTO_MAX))) await c.delete(k);
  }
  return res;
}
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method === "GET" && isPhoto(url)) { e.respondWith(photo(e.request)); return; }
  if (e.request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")
      || url.pathname === "/ping") return;
  const key = e.request.mode === "navigate" ? "/" : url.pathname;
  e.respondWith(caches.open(CACHE).then(c => c.match(key).then(hit => hit || fetch(e.request))));
});
