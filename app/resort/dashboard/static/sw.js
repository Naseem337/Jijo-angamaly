/* Resort Desk service worker: lets the app install and open fast. Pages always come from the server
   (bookings change all the time); only the look (css, js, icons) is kept for offline and speed. */
const CACHE = "resort-desk-v3";
const SHELL = ["static/app.css", "static/frappe.css", "static/app.js", "static/icon-192.png", "static/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL.map((p) => new URL(p, self.location)))).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (req.mode === "navigate") {  // pages: network first, a small offline note when there is no signal
    e.respondWith(fetch(req).catch(() => new Response(
      "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><body style='font-family:system-ui;padding:40px;text-align:center'>" +
      "<h2>You are offline</h2><p>Check your internet and open the app again.</p>", { headers: { "Content-Type": "text/html" } })));
    return;
  }
  if (url.pathname.includes("/static/")) {  // look and feel: always the newest from the server, the saved copy only when offline
    e.respondWith(fetch(req).then((r) => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      return r;
    }).catch(() => caches.match(req, { ignoreSearch: true })));
  }
});
