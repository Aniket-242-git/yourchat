/* ==========================================================================
 * Couple Chat — frontend controller (vanilla JS, no build step).
 *
 * State lives ONLY in this JS memory: refreshing the page clears everything.
 * Delivery: SSE primary + automatic short-polling fallback.
 * ========================================================================== */
"use strict";

// ---------- tiny DOM / util helpers ----------
const $ = (sel) => document.querySelector(sel);
const csrf = () => $('meta[name="csrf-token"]').content;
const escapeText = (node, text) => { node.textContent = text; }; // XSS-safe by construction

function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), ms);
}

function showScreen(id) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  $(id).classList.add("active");
}

async function api(path, body) {
  const opts = {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
    credentials: "same-origin",
    body: JSON.stringify(body || {}),
  };
  const r = await fetch(path, opts);
  let j = {};
  try { j = await r.json(); } catch (e) { /* non-JSON error page */ }
  if (r.status === 401 && j.error === "not_logged_in" && STATE.me) {
    // session lost (server restart / cookie gone)
    hardReset("Your session expired — pick a name again 💕");
  }
  return { status: r.status, data: j };
}

// ---------- application state (memory only!) ----------
const STATE = {
  me: null,
  partner: null,
  lockMode: false,       // 🔒 toggle in header
  transport: "sse",      // "sse" | "poll"
  es: null,              // EventSource
  pollTimer: null,
  hbTimer: null,
  pendingNudgeFrom: null,
  unlockTarget: null,    // bubble element awaiting a password
  unlockTries: 5,
};

function hardReset(msg) {
  stopRealtime();
  clearInterval(STATE.hbTimer);
  STATE.me = null; STATE.partner = null;
  $("#messages").innerHTML = "";
  showScreen("#screen-login");
  if (msg) toast(msg);
}

// ======================= realtime delivery (SSE + poll) ====================
function startRealtime() {
  stopRealtime();
  trySSE();
}

function stopRealtime() {
  if (STATE.es) { try { STATE.es.close(); } catch (e) {} STATE.es = null; }
  clearInterval(STATE.pollTimer);
  STATE.pollTimer = null;
}

function trySSE() {
  if (typeof EventSource === "undefined") return startPolling();
  const es = new EventSource("/api/events", { withCredentials: true });
  STATE.es = es;
  let opened = false;
  // If SSE never opens within 8s (buffered/blocked host), switch to polling.
  const watchdog = setTimeout(() => {
    if (!opened) { try { es.close(); } catch (e) {} startPolling(); }
  }, 8000);

  es.onopen = () => { opened = true; clearTimeout(watchdog); STATE.transport = "sse"; setConnDot(); };
  es.onerror = () => {
    // EventSource auto-retries while CONNECTING/OPEN; only fall back when dead.
    if (es.readyState === EventSource.CLOSED) {
      clearTimeout(watchdog);
      if (STATE.es === es) startPolling();
    }
  };

  const bind = (name) => es.addEventListener(name, (ev) => handleEvent(name, JSON.parse(ev.data)));
  ["nudge", "accept", "decline", "message", "locked", "typing", "partner_left"].forEach(bind);
}

function startPolling() {
  STATE.transport = "poll";
  setConnDot();
  toast("Live stream blocked by host — using polling mode 🔄", 4000);
  clearInterval(STATE.pollTimer);
  STATE.pollTimer = setInterval(async () => {
    const { data } = await api("/api/poll", {});
    if (data && data.events) data.events.forEach((e) => handleEvent(e.type, e.payload));
  }, 2500);
}

// Route every incoming event through one handler (shared by SSE & poll).
function handleEvent(type, p) {
  switch (type) {
    case "nudge":        onIncomingNudge(p.from); break;
    case "accept":       onAccepted(p.from); break;
    case "decline":      toast(`${p.from} declined 😔`); $("#nudge-status").textContent = ""; break;
    case "message":      addBubble(p.text, "them"); break;
    case "locked":       addLockedBubble(p); break;
    case "typing":       flashTyping(); break;
    case "partner_left": onPartnerLeft(p.by); break;
  }
}

// ============================ heartbeat ===================================
function startHeartbeat() {
  clearInterval(STATE.hbTimer);
  STATE.hbTimer = setInterval(async () => {
    const { data } = await api("/api/heartbeat", {});
    if (data && data.ok) {
      setConnDot(data.partner_online);
      if ($("#screen-waiting").classList.contains("active")) {
        const st = await fetch("/api/status", { credentials: "same-origin" }).then(r => r.json()).catch(() => null);
        if (st && st.active) $("#waiting-online").textContent = "online now: " + st.active.join(", ");
      }
    }
  }, 25000);
}

function setConnDot(online) {
  const dot = $("#chat-conn");
  if (!dot) return;
  let cls = "dot-off";
  if (online === true) cls = "dot-on";
  else if (online === false) cls = "dot-bad";
  else if (STATE.transport === "sse" && STATE.es) cls = "dot-on";
  dot.className = cls;
  dot.title = STATE.transport === "sse" ? "live (SSE)" : "live (polling)";
}

// ============================== login =====================================
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#login-error");
  err.textContent = "";
  const username = $("#login-username").value.trim();
  const { data } = await api("/api/login", { username });
  if (!data.ok) {
    err.textContent = data.message || "Couldn't sign you in 😢";
    return;
  }
  STATE.me = data.username;
  $("#waiting-me").textContent = STATE.me;
  showScreen("#screen-waiting");
  startRealtime();
  startHeartbeat();
  registerPushIfWanted();          // ask once, politely
});

$("#logout-btn").addEventListener("click", async () => {
  await api("/api/logout", {});
  unregisterPush();
  hardReset("Bye! 👋");
});

// ============================ nudge / connect ==============================
$("#nudge-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const target = $("#nudge-target").value.trim();
  const st = $("#nudge-status");
  st.textContent = "sending…";
  const { data } = await api("/api/nudge", { target });
  if (!data.ok) {
    st.textContent = data.message || "Couldn't send that nudge 😅";
    return;
  }
  st.textContent = data.message || "Nudge sent! Waiting for them to accept… 💗";
});

function onIncomingNudge(from) {
  if (STATE.partner) {            // already paired -> auto-decline politely
    api("/api/respond", { from, accept: false });
    return;
  }
  STATE.pendingNudgeFrom = from;
  escapeText($("#nudge-text strong"), from);
  $("#modal-nudge").classList.remove("hidden");
}

$("#nudge-accept").addEventListener("click", async () => {
  const from = STATE.pendingNudgeFrom;
  $("#modal-nudge").classList.add("hidden");
  const { data } = await api("/api/respond", { from, accept: true });
  if (!data.ok) { toast(data.message || "Couldn't connect 😅"); return; }
  // both sides receive an "accept" event which opens the chat
});

$("#nudge-decline").addEventListener("click", async () => {
  const from = STATE.pendingNudgeFrom;
  $("#modal-nudge").classList.add("hidden");
  await api("/api/respond", { from, accept: false });
});

function onAccepted(from) {
  if (!STATE.me) return;
  STATE.partner = from;
  $("#chat-partner").textContent = "💗 " + from;
  $("#messages").innerHTML = "";
  $("#nudge-status").textContent = "";
  showScreen("#screen-chat");
  $("#msg-input").focus();
  meta(`You and ${from} are connected ✨ (this chat is never saved)`);
}

function onPartnerLeft(by) {
  if (by !== STATE.partner) return;
  meta(`${by} disconnected 💔`);
  STATE.partner = null;
  setTimeout(() => showScreen("#screen-waiting"), 1200);
}

$("#disconnect-btn").addEventListener("click", async () => {
  await api("/api/disconnect", {});
  STATE.partner = null;
  $("#messages").innerHTML = "";
  showScreen("#screen-waiting");
  toast("Disconnected — you're both free again 🕊️");
});

// ============================== messaging ==================================
$("#send-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#msg-input");
  const text = input.value.trim();
  if (!text || !STATE.partner) return;
  if (STATE.lockMode) {           // locked flow uses its own modal
    openLockSendModal(text);
    input.value = "";
    return;
  }
  input.value = "";
  addBubble(text, "me");
  const { data } = await api("/api/send", { text });
  if (!data.ok) {
    const err = $("#chat-error");
    err.textContent = data.message || "Failed to send 😢";
    setTimeout(() => (err.textContent = ""), 3000);
  }
});

// typing indicator (throttled client-side too)
let typingSentAt = 0;
$("#msg-input").addEventListener("input", () => {
  const now = Date.now();
  if (STATE.partner && now - typingSentAt > 3000) {
    typingSentAt = now;
    api("/api/typing", {});
  }
});

function flashTyping() {
  const el = $("#typing-indicator");
  el.classList.remove("hidden");
  clearTimeout(el._h);
  el._h = setTimeout(() => el.classList.add("hidden"), 2500);
}

// ---------- bubbles (textContent only → XSS-safe) ----------
function addBubble(text, who) {
  const box = $("#messages");
  const hint = box.querySelector(".empty-hint");
  if (hint) hint.remove();
  const div = document.createElement("div");
  div.className = "bubble " + who;
  escapeText(div, text);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function meta(text) {
  const box = $("#messages");
  const div = document.createElement("div");
  div.className = "bubble meta";
  escapeText(div, text);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// ====================== locked messages (E2E) ==============================
$("#lock-toggle").addEventListener("click", () => {
  STATE.lockMode = !STATE.lockMode;
  $("#lock-toggle").setAttribute("aria-pressed", String(STATE.lockMode));
  $("#msg-input").placeholder = STATE.lockMode
    ? "Type a secret message 🔒…" : "Type a message…";
});

function openLockSendModal(text) {
  $("#lock-msg").value = text;
  $("#lock-send-error").textContent = "";
  $("#modal-lock-send").classList.remove("hidden");
  $("#lock-pass").focus();
}

$("#lock-send-cancel").addEventListener("click", () =>
  $("#modal-lock-send").classList.add("hidden"));

$("#lock-send-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#lock-send-error");
  const text = $("#lock-msg").value.trim();
  const p1 = $("#lock-pass").value, p2 = $("#lock-pass2").value;
  if (p1 !== p2) { err.textContent = "Passwords don't match 💔"; return; }
  if (p1.length < 4) { err.textContent = "Use at least 4 characters."; return; }
  err.textContent = "Encrypting… (210k PBKDF2 rounds, ~1s)";
  let pack;
  try {
    pack = await CoupleCrypto.encryptMessage(text, p1);
  } catch (ex) {
    err.textContent = "Encryption failed: " + ex.message; return;
  }
  const { data } = await api("/api/send_locked", pack);
  $("#modal-lock-send").classList.add("hidden");
  // wipe the sensitive fields from the DOM immediately
  $("#lock-msg").value = ""; $("#lock-pass").value = ""; $("#lock-pass2").value = "";
  if (!data.ok) { toast(data.message || "Couldn't send locked message 😢"); return; }
  addBubble("🔒 " + text, "me");   // sender keeps their own plaintext in memory
});

function addLockedBubble(pack) {
  const box = $("#messages");
  const hint = box.querySelector(".empty-hint");
  if (hint) hint.remove();
  const div = document.createElement("div");
  div.className = "bubble them locked";
  div.textContent = "🔒 Locked message — tap to unlock";
  div.dataset.pack = JSON.stringify(pack);   // stays in JS memory only
  div.addEventListener("click", () => openUnlockModal(div));
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function openUnlockModal(bubbleEl) {
  STATE.unlockTarget = bubbleEl;
  STATE.unlockTries = 5;
  $("#unlock-tries").textContent = String(STATE.unlockTries);
  $("#lock-open-error").textContent = "";
  $("#unlock-pass").value = "";
  $("#modal-lock-open").classList.remove("hidden");
  $("#unlock-pass").focus();
}

$("#lock-open-cancel").addEventListener("click", () =>
  $("#modal-lock-open").classList.add("hidden"));

$("#lock-open-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const bubble = STATE.unlockTarget;
  if (!bubble) return;
  const pass = $("#unlock-pass").value;
  const err = $("#lock-open-error");
  let pack;
  try { pack = JSON.parse(bubble.dataset.pack); }
  catch (ex) { closeUnlock(); return; }         // already consumed/hidden

  err.textContent = "Decrypting…";
  try {
    const plain = await CoupleCrypto.decryptMessage(pack, pass);
    closeUnlock();
    // replace bubble with plaintext, then auto-hide after 30s (never stored)
    bubble.classList.remove("locked");
    bubble.textContent = plain;
    bubble.style.cursor = "default";
    delete bubble.dataset.pack;
    setTimeout(() => {
      bubble.classList.add("fading");
      setTimeout(() => {
        bubble.textContent = "🔒 (opened — hidden for privacy)";
        bubble.className = "bubble them meta";
      }, 1000);
    }, 30000);
  } catch (ex) {
    STATE.unlockTries -= 1;
    $("#unlock-tries").textContent = String(Math.max(STATE.unlockTries, 0));
    if (STATE.unlockTries <= 0) {
      closeUnlock();
      bubble.textContent = "🔒 Too many wrong attempts — locked away forever";
      delete bubble.dataset.pack;   // drop ciphertext so no more retries
      toast("Wrong password too many times 😞");
    } else {
      err.textContent = `Wrong password 💔 (${STATE.unlockTries} tries left)`;
    }
  }
  $("#unlock-pass").value = "";
});

function closeUnlock() {
  $("#modal-lock-open").classList.add("hidden");
  $("#unlock-pass").value = "";
  STATE.unlockTarget = null;
}

// ============================ web push ======================================
function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const s = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(s);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; ++i) out[i] = raw.charCodeAt(i);
  return out;
}

async function registerPushIfWanted() {
  const enabled = $('meta[name="push-enabled"]').content === "1";
  if (!enabled || !("serviceWorker" in navigator) || !("PushManager" in window)) return;
  let reg;
  try { reg = await navigator.serviceWorker.getRegistration(); } catch (e) { return; }
  if (!reg) return;
  const perm = Notification.permission;
  if (perm === "denied") return;
  if (perm === "default") {
    // Ask on the next user gesture instead of nagging immediately.
    document.addEventListener("click", requestPushOnce, { once: true });
    return;
  }
  await doSubscribe(reg);
}

async function requestPushOnce() {
  try {
    const perm = await Notification.requestPermission();
    if (perm === "granted") {
      const reg = await navigator.serviceWorker.getRegistration();
      if (reg) await doSubscribe(reg);
    }
  } catch (e) { /* ignore */ }
}

async function doSubscribe(reg) {
  const vapidKey = $('meta[name="vapid-public-key"]').content;
  if (!vapidKey) return;
  try {
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidKey),
      });
    }
    await api("/api/subscribe", sub.toJSON());
  } catch (e) { /* push optional — fail silently */ }
}

async function unregisterPush() {
  try {
    const reg = await navigator.serviceWorker.getRegistration();
    if (reg) {
      const sub = await reg.pushManager.getSubscription();
      if (sub) await sub.unsubscribe();
    }
    await api("/api/unsubscribe", {});
  } catch (e) { /* ignore */ }
}

// ============================== boot ========================================
(async function boot() {
  // Register the service worker (root scope) for Web Push.
  if ("serviceWorker" in navigator) {
    try { await navigator.serviceWorker.register("/sw.js", { scope: "/" }); }
    catch (e) { console.warn("SW registration failed:", e); }
  }
  // Restore session if the cookie still maps to a live username.
  try {
    const r = await fetch("/api/status", { credentials: "same-origin" });
    const data = await r.json().catch(() => ({}));
    if (data && data.ok) {
      STATE.me = data.username;
      $("#waiting-me").textContent = STATE.me;
      if (data.partner) {
        STATE.partner = data.partner;
        $("#chat-partner").textContent = "💗 " + data.partner;
        showScreen("#screen-chat");
        meta("Reconnected ✨ (previous messages were not saved)");
      } else {
        showScreen("#screen-waiting");
      }
      startRealtime();
      startHeartbeat();
      registerPushIfWanted();
    }
  } catch (e) { /* first visit or server down — stay on login screen */ }
})();
