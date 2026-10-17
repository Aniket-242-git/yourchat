/* ==========================================================================
 * Couple Chat — Service Worker (Web Push receiver).
 *
 * Notifications are content-free by design: the payload only ever contains a
 * generic title/body like "You have a new message". No plaintext, no keys.
 * ========================================================================== */
"use strict";

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: "Couple Chat 💌", body: "You have something new." };
  }
  const title = data.title || "Couple Chat 💌";
  const options = {
    body: data.body || "You have a new message",
    tag: data.tag || "couplechat",
    icon: "/static/icons/heart.png",
    badge: "/static/icons/heart.png",
    data: { url: self.registration.scope },
    vibrate: [120, 60, 120],
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ("focus" in client) return client.focus();
      }
      return self.clients.openWindow(event.notification.data.url || "/");
    })
  );
});
