// Quiet Morning service worker: shows push notifications; caches nothing that could be stale or private.
self.addEventListener("install", function (e) { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener("push", function (event) {
  var d = {title: "Quiet Morning", body: "Please check in.", url: "/"};
  try { d = Object.assign(d, event.data.json()); } catch (e) {}
  event.waitUntil(self.registration.showNotification(d.title, {body: d.body, data: {url: d.url}, tag: "quiet-morning", renotify: true, requireInteraction: true, icon: "/static/watch/icon-192.png"}));
});
self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  event.waitUntil(self.clients.openWindow((event.notification.data && event.notification.data.url) || "/"));
});
