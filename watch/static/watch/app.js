// Progressive enhancement only: the app works without any of this.
(function () {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(function () {});
  // Live-ish dashboard: reload when the state changes (no reload while typing).
  var el = document.querySelector("[data-status-url]");
  if (el) {
    var last = null;
    setInterval(function () {
      if (document.hidden || /INPUT|SELECT|TEXTAREA/.test((document.activeElement || {}).tagName || "")) return;
      fetch(el.dataset.statusUrl, {credentials: "same-origin"}).then(function (r) { return r.json(); }).then(function (j) {
        var s = JSON.stringify(j); if (last && s !== last) location.reload(); last = s;
      }).catch(function () {});
    }, 60000);
  }
  var btn = document.getElementById("enable-push");
  if (btn) btn.addEventListener("click", function () {
    var out = document.getElementById("push-status");
    if (!("PushManager" in window) || !("serviceWorker" in navigator)) { out.textContent = "This browser does not support web push."; return; }
    var key = btn.dataset.vapid;
    if (!key) { out.textContent = "The server has no VAPID key configured yet (see docs/deploy-aws.md)."; return; }
    function b64(s) { s = (s + "=".repeat((4 - s.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/"); var r = atob(s); return Uint8Array.from(r, function (c) { return c.charCodeAt(0); }); }
    Notification.requestPermission().then(function (p) {
      if (p !== "granted") { out.textContent = "Permission was not granted."; return; }
      return navigator.serviceWorker.ready.then(function (reg) {
        return reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64(key)});
      }).then(function (sub) {
        return fetch(btn.dataset.url, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-CSRFToken": btn.dataset.csrf}, body: JSON.stringify(sub.toJSON())});
      }).then(function (r) { out.textContent = r.ok ? "Push is on for this device." : "Could not save the subscription."; });
    }).catch(function (e) { out.textContent = "Push failed: " + e.message; });
  });
})();
