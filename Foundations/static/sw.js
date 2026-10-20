// The worker only handles push notifications. It does not intercept page loads:
// in the iPhone home-screen app, routing navigations through the worker made the
// dashboard fail to load, so pages always come straight from the network.
self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  // Clear caches left by earlier versions that served an offline page.
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil(
    self.registration.showNotification(data.title || "Shaked Hub", {
      body: data.body || "",
      icon: "/static/icons/icon-192.png",
      data: { url: data.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow(event.notification.data.url));
});
