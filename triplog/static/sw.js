/*
 * IDmate Service Worker — 12.13
 *
 * SAFE strategy. The app is under active development, so an aggressive cache
 * would ship stale builds. Rules:
 *   - HTML / navigations: NETWORK-FIRST. Always hit the network; only fall
 *     back to a tiny inline offline page if the network truly fails. HTML is
 *     never served from cache while online.
 *   - /api/* and non-GET: never touched, always straight to the network.
 *   - A minimal set of static assets is cached NETWORK-FIRST: fresh from the
 *     server whenever online, the cached copy only as offline fallback. The
 *     cache name carries the app version (app.py substitutes it), so every
 *     build is a new worker that sweeps the old caches on activate. Until
 *     1.2.154 these assets were cache-first under a fixed name and a
 *     stylesheet change never reached the phone.
 *
 * Scope note: this file is served from /static, so its scope is /static/ —
 * it does NOT control top-level app navigations (/, /trips, ...). It is kept
 * deliberately harmless: within scope it provides an offline fallback and may
 * speed up the listed static assets; outside scope it does nothing. It cannot
 * break the app.
 */

const CACHE = 'idmate-__VERSION__';   // Version setzt app.py beim Ausliefern ein

// Minimal, non-critical, versioned-by-build static assets. All optional —
// failures during precache are swallowed so install never breaks.
const STATIC_ASSETS = [
  '/static/idmate.css',
  '/static/idmate.js',
  '/static/manifest.webmanifest',
  '/static/logo.svg',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

const OFFLINE_HTML =
  '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width, initial-scale=1.0">' +
  '<meta name="color-scheme" content="dark"><title>Offline</title>' +
  '<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;' +
  'background:#0d1117;color:#e6edf3;display:flex;align-items:center;justify-content:center;' +
  'min-height:100vh;margin:0;text-align:center;padding:1rem}' +
  'h1{font-size:1.1rem;font-weight:600;color:#58a6ff}p{color:#8b949e;font-size:0.9rem}</style>' +
  '</head><body><div><h1>IDmate</h1><p>You are offline. Reconnect and try again.</p></div></body></html>';

function offlineResponse() {
  return new Response(OFFLINE_HTML, {
    status: 503,
    statusText: 'Offline',
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      // Precache static assets individually so one 404 doesn't abort install.
      await Promise.all(
        STATIC_ASSETS.map((url) =>
          cache.add(url).catch(() => {
            /* ignore missing/changed asset */
          })
        )
      );
      await self.skipWaiting();
    })()
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => (k === CACHE ? null : caches.delete(k))));
      await self.clients.claim();
    })()
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // Only handle GET; everything else (POST/etc.) goes straight to the network.
  if (req.method !== 'GET') return;

  let url;
  try {
    url = new URL(req.url);
  } catch (e) {
    return;
  }

  // Same-origin only.
  if (url.origin !== self.location.origin) return;

  // Never cache or intercept API traffic.
  if (url.pathname.startsWith('/api/')) return;

  // NETWORK-FIRST for navigations / HTML documents.
  const isNavigation =
    req.mode === 'navigate' ||
    (req.headers.get('accept') || '').includes('text/html');

  if (isNavigation) {
    event.respondWith(
      (async () => {
        try {
          return await fetch(req);
        } catch (e) {
          return offlineResponse();
        }
      })()
    );
    return;
  }

  // NETWORK-FIRST for the small set of known static assets; cache = offline fallback.
  if (STATIC_ASSETS.includes(url.pathname)) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(CACHE);
        try {
          const res = await fetch(req);
          if (res && res.ok) cache.put(req, res.clone());
          return res;
        } catch (e) {
          const cached = await cache.match(req, { ignoreSearch: true });
          return cached || Response.error();
        }
      })()
    );
    return;
  }

  // Everything else: let the network handle it (no SW involvement).
});
