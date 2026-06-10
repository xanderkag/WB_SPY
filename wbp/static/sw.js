// WB Watch — простой service worker.
// Кэшируем оболочку (html, манифест, иконку) для оффлайн-показа.
// Динамика (API) идёт network-first с фоллбэком на cache.

const VERSION = 'wb-watch-v2';
const SHELL = [
  '/',
  '/static/manifest.webmanifest',
  '/static/icon.svg',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== VERSION).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;

  // API — network first, кэш как запасной вариант (показать последнее что было).
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(e.request)
        .then(r => {
          const copy = r.clone();
          caches.open(VERSION).then(c => c.put(e.request, copy));
          return r;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // shell + статика — cache first, потом сеть.
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request).then(resp => {
      if (resp.ok && resp.type === 'basic') {
        const copy = resp.clone();
        caches.open(VERSION).then(c => c.put(e.request, copy));
      }
      return resp;
    }).catch(() => caches.match('/')))
  );
});
