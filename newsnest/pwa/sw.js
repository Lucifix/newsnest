/* NewsNest service worker. __VERSION__ changes with every deploy, so the browser installs the new worker. */
const VERSION = "__VERSION__";
const SHELL = `newsnest-shell-${VERSION}`;
const PAGES = "newsnest-pages";
const MAX_PAGES = 30;
const ICONS = "newsnest-icons";
const MAX_ICONS = 300;
const PRECACHE = [
  "/offline",
  "/static/app.css?v=__VERSION__",
  "/static/app.js?v=__VERSION__",
  "/static/icons/icon-192.png",
  "/static/icons/apple-touch-icon.png",
  "/static/fonts/space-grotesk-latin.woff2",
  "/static/fonts/space-grotesk-latin-ext.woff2",
  "/static/fonts/jetbrains-mono-latin.woff2",
  "/static/fonts/jetbrains-mono-latin-ext.woff2",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      const old = key.startsWith("newsnest-shell-") && key !== SHELL;
      // caches of the pre-rename worker (radar-*) would otherwise stay on the device for good
      if (old || key.startsWith("radar-")) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

async function trimPages() {
  const cache = await caches.open(PAGES);
  const keys = await cache.keys();
  for (const key of keys.slice(0, Math.max(0, keys.length - MAX_PAGES))) await cache.delete(key);
}

async function trimIcons() {
  const cache = await caches.open(ICONS);
  const keys = await cache.keys();
  for (const key of keys.slice(0, Math.max(0, keys.length - MAX_ICONS))) await cache.delete(key);
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  // Actions and redirects always go to the network, uncached
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/go/") || url.pathname === "/sw.js" ||
      url.pathname === "/health" || url.pathname.startsWith("/feed/more")) return;

  // Fingerprinted assets and icons: cache first (the address changes when the content does)
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request).then((response) => {
      if (response.ok) { const copy = response.clone(); caches.open(SHELL).then((c) => c.put(request, copy)); }
      return response;
    })));
    return;
  }

  // Source icons: the address carries the icon's hash, so a cached copy is never stale
  if (url.pathname.startsWith("/icon/")) {
    event.respondWith(caches.open(ICONS).then((cache) => cache.match(request).then((hit) => hit || fetch(request).then((response) => {
      if (response.ok) cache.put(request, response.clone()).then(trimIcons);
      return response;
    }))));
    return;
  }

  // Pages: network first, so the feed is always fresh; offline falls back to the last copy
  if (request.mode === "navigate") {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.ok && response.type === "basic") {
          const copy = response.clone();
          caches.open(PAGES).then((c) => c.put(request, copy)).then(trimPages);
        }
        return response;
      } catch {
        return (await caches.match(request, { cacheName: PAGES })) || (await caches.match("/offline")) || Response.error();
      }
    })());
  }
});
