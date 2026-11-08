// Minimal service worker: required for PWA installability. Network-first, no caching
// of the live stream or API; we only need it present and handling fetch.
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => self.clients.claim());
self.addEventListener('fetch', (e) => { /* pass through to network */ });
