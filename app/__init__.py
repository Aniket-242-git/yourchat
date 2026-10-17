"""
Couple Chat — privacy-first 1:1 chat for two people.

Flask application factory + all API routes.

Real-time strategy (cPanel-friendly, no WebSockets / no daemons):
  * Primary : Server-Sent Events (SSE) on GET /api/events
  * Fallback: short polling on POST /api/poll (client switches automatically)
Both read from the SAME fetch-and-delete event queue in the shared store, so
each queued event (message / nudge / pairing signal) is delivered at most once.

Privacy: message plaintext only passes through as an opaque string that is
deleted right after its single delivery. Locked messages are {salt, iv, ct}
base64 blobs — the server can never read them.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from functools import wraps

from flask import (Flask, Response, jsonify, render_template, request,
                   session)

import config
from app.push import notify_many
from app.store import get_store

log = logging.getLogger("couplechat")

# ---------------------------------------------------------------------------
# Event types relayed through the queue
# ---------------------------------------------------------------------------
EV_NUDGE = "nudge"            # someone wants to connect
EV_ACCEPT = "accept"          # your nudge was accepted -> open chat
EV_DECLINE = "decline"        # your nudge was declined
EV_MESSAGE = "message"        # plain chat message
EV_LOCKED = "locked"          # end-to-end encrypted chat message
EV_TYPING = "typing"          # ephemeral typing indicator
EV_PARTNER_LEFT = "partner_left"

_B64_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")


# ===========================================================================
# Small helpers
# ===========================================================================
def _username() -> str | None:
    """Currently logged-in (session) username or None."""
    return session.get("u")


def login_required_api(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not _username():
            return jsonify(ok=False, error="not_logged_in"), 401
        return fn(*a, **kw)
    return wrapper


def _client_ip() -> str:
    # Behind cPanel proxies (LiteSpeed/Nginx) the real client IP is forwarded.
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


# ===========================================================================
# App factory
# ===========================================================================
def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY

    # ---- secure, HttpOnly session cookie ---------------------------------
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.IS_PROD,   # HTTPS-only cookie in prod
        PERMANENT_SESSION_LIFETIME=86400,
        MAX_CONTENT_LENGTH=128 * 1024,          # bodies we ever need are tiny
    )

    # ---- privacy: no request-body logging -------------------------------
    # Werkzeug access logs contain only method/path/status (never bodies),
    # but we quiet them anyway; see README for disabling host-level access
    # logging of request data via .htaccess.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.logger.setLevel(logging.WARNING)

    _register_security_headers(app)
    _register_csrf(app)
    _register_routes(app)
    return app


# ===========================================================================
# Security headers & CSP
# ===========================================================================
def _register_security_headers(app: Flask) -> None:
    @app.after_request
    def set_headers(resp: Response) -> Response:
        csp = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "form-action 'self'"
        )
        resp.headers["Content-Security-Policy"] = csp
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        resp.headers["X-XSS-Protection"] = "0"        # CSP is the modern control
        if config.IS_PROD:
            resp.headers["Strict-Transport-Security"] = "max-age=2592000; includeSubDomains"
        # Service worker must be served from root scope with fresh checks.
        if request.path in ("/sw.js", "/manifest.webmanifest"):
            resp.headers["Cache-Control"] = "no-cache"
            resp.headers["Service-Worker-Allowed"] = "/"
        return resp


# ===========================================================================
# CSRF protection (signed-session token + header double-submit)
# ===========================================================================
def _register_csrf(app: Flask) -> None:
    @app.before_request
    def csrf_guard():
        # Always make sure a token exists (also on GETs, so the template can
        # embed it before the first POST).
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        session.permanent = True

        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        token = session.get("csrf")
        given = request.headers.get("X-CSRF-Token", "")
        if not token or not secrets.compare_digest(str(token), str(given)):
            return jsonify(ok=False, error="csrf_failed"), 400
        return None


# ===========================================================================
# Routes
# ===========================================================================
def _register_routes(app: Flask) -> None:

    # ---------------- pages ----------------
    @app.get("/")
    def page_index():
        return render_template(
            "index.html",
            vapid_public_key=config.VAPID_PUBLIC_KEY,
            push_enabled=config.PUSH_ENABLED,
            csrf=session.get("csrf", ""),
        )

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, backend=config.DB_BACKEND)

    # ---------------- auth / presence ----------------
    @app.post("/api/login")
    def api_login():
        s = get_store()
        data = request.get_json(silent=True) or {}
        name = s.norm_username(data.get("username", ""))
        if not name:
            return jsonify(ok=False, error="invalid_username",
                           message="2–24 chars: letters, numbers, . _ - only 💕"), 400
        if s.rate_limited(f"login:{_client_ip()}|{name}", *config.RL_LOGIN):
            return jsonify(ok=False, error="rate_limited",
                           message="Too many attempts — wait a minute 🙈"), 429
        if not s.register(name):
            msg = f"“{name}” is already here. Try another name? 💕"
            return jsonify(ok=False, error="taken", message=msg), 409
        session["u"] = name
        return jsonify(ok=True, username=name, csrf=session.get("csrf"))

    @app.post("/api/logout")
    @login_required_api
    def api_logout():
        s = get_store()
        me = _username()
        partner = s.unpair(me)
        if partner:
            s.emit(partner, EV_PARTNER_LEFT, {"by": me})
        s.clear_nudges_for(me)
        s.drop_push_subs(me)
        s.unregister(me)
        session.clear()
        return jsonify(ok=True)

    @app.post("/api/heartbeat")
    @login_required_api
    def api_heartbeat():
        s = get_store()
        me = _username()
        if not s.is_online(me):
            # Presence TTL lapsed (e.g. laptop slept). Re-claim if still free.
            if not s.register(me):
                return jsonify(ok=False, error="expired",
                               message="Session expired — please pick a name again."), 401
        s.heartbeat(me)
        partner = s.partner_of(me)
        return jsonify(ok=True, partner=partner,
                       partner_online=bool(partner and s.is_online(partner)))

    # ---------------- connect / nudge ----------------
    @app.post("/api/nudge")
    @login_required_api
    def api_nudge():
        s = get_store()
        me = _username()
        data = request.get_json(silent=True) or {}
        target = s.norm_username(data.get("target", ""))
        if not target:
            return jsonify(ok=False, error="invalid_username",
                           message="That name isn't valid 💕"), 400
        if target == me:
            return jsonify(ok=False, error="self_nudge",
                           message="You can't chat with yourself 😅"), 400
        if s.rate_limited(f"nudge:{me}", *config.RL_NUDGE):
            return jsonify(ok=False, error="rate_limited",
                           message="Too many nudges — wait a minute 💗"), 429
        # Rule: one username connects to ONE other username at a time.
        if s.partner_of(me):
            return jsonify(ok=False, error="already_paired_you",
                           message="You're already connected. Disconnect first 💔"), 409
        if s.partner_of(target):
            return jsonify(ok=False, error="already_paired_target",
                           message=f"{target} is already chatting with someone else 💔"), 409
        if not s.is_online(target):
            return jsonify(ok=False, error="offline",
                           message=f"{target} isn't online right now 🌙"), 404
        s.add_nudge(me, target)
        s.emit(target, EV_NUDGE, {"from": me})
        # Tab may be backgrounded/closed while presence is still fresh —
        # push covers the "browser open but tab asleep" case too.
        notify_many(s.push_subs(target), "Couple Chat 💌",
                    f"{me} wants to connect with you!", tag=f"nudge:{me}")
        return jsonify(ok=True, message=f"Nudge sent to {target} 👋")

    @app.post("/api/respond")
    @login_required_api
    def api_respond():
        """Accept or decline a pending nudge."""
        s = get_store()
        me = _username()
        data = request.get_json(silent=True) or {}
        frm = s.norm_username(data.get("from", ""))
        accept = bool(data.get("accept"))
        if not frm:
            return jsonify(ok=False, error="invalid_username"), 400

        if accept:
            # Serialise acceptance across Passenger processes/workers.
            if not s.lock(f"pair:{me}"):
                return jsonify(ok=False, error="busy",
                               message="Try again in a second 🙈"), 409
            try:
                if s.partner_of(me) or s.partner_of(frm):
                    return jsonify(ok=False, error="already_paired",
                                   message="One connection at a time — disconnect first 💔"), 409
                if not s.take_nudge(frm, me):
                    return jsonify(ok=False, error="stale",
                                   message="That request expired ⏳"), 410
                s.pair(me, frm)
                s.emit(frm, EV_ACCEPT, {"from": me})
                s.emit(me, EV_ACCEPT, {"from": frm})
            finally:
                s.unlock(f"pair:{me}")
            return jsonify(ok=True, partner=frm)

        s.take_nudge(frm, me)
        s.emit(frm, EV_DECLINE, {"from": me})
        return jsonify(ok=True)

    @app.post("/api/disconnect")
    @login_required_api
    def api_disconnect():
        s = get_store()
        me = _username()
        partner = s.unpair(me)      # frees BOTH users
        s.clear_nudges_for(me)
        if partner:
            s.emit(partner, EV_PARTNER_LEFT, {"by": me})
            notify_many(s.push_subs(partner), "Couple Chat",
                        "Your partner disconnected.", tag="left")
        return jsonify(ok=True, partner=partner)

    @app.get("/api/status")
    @login_required_api
    def api_status():
        s = get_store()
        me = _username()
        partner = s.partner_of(me)
        return jsonify(ok=True, username=me, partner=partner,
                       partner_online=bool(partner and s.is_online(partner)),
                       active=s.all_active_usernames())

    # ---------------- chat relay ----------------
    def _relay(ev_type: str, payload: dict, push_body: str):
        """Shared logic for /api/send and /api/send_locked."""
        s = get_store()
        me = _username()
        partner = s.partner_of(me)
        if not partner:
            return jsonify(ok=False, error="not_paired",
                           message="You're not connected to anyone right now."), 409
        if s.rate_limited(f"msg:{me}", *config.RL_MSG):
            return jsonify(ok=False, error="rate_limited",
                           message="Slow down a little 💨"), 429
        raw = json.dumps(payload, separators=(",", ":"))
        if len(raw.encode()) > config.MAX_EVENT_BYTES:
            return jsonify(ok=False, error="too_large",
                           message="Message too long."), 413
        payload["from"] = me
        payload["ts"] = int(time.time())
        s.emit(partner, ev_type, payload)          # queued, deleted on delivery
        if not s.is_online(partner):
            # Content-free notification — never includes the message text.
            notify_many(s.push_subs(partner), "Couple Chat 💌", push_body,
                        tag=f"msg:{me}")
        return jsonify(ok=True)

    @app.post("/api/send")
    @login_required_api
    def api_send():
        data = request.get_json(silent=True) or {}
        text = str(data.get("text", ""))[: config.MAX_MESSAGE_LEN]
        if not text.strip():
            return jsonify(ok=False, error="empty"), 400
        # Defensive control-char strip. The client renders with textContent
        # only, so HTML never executes regardless (XSS-safe by construction).
        text = "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)
        return _relay(EV_MESSAGE, {"text": text}, "You have a new message 💬")

    @app.post("/api/send_locked")
    @login_required_api
    def api_send_locked():
        """Relay an end-to-end encrypted message. The server sees ONLY three
        base64 strings (salt, iv, ciphertext); it cannot decrypt or read them."""
        data = request.get_json(silent=True) or {}
        enc = {k: str(data.get(k, "")) for k in ("salt", "iv", "ct")}
        for k, v in enc.items():
            if not (0 < len(v) <= 8192 and len(v) % 4 == 0 and _B64_OK.issuperset(v)):
                return jsonify(ok=False, error="bad_payload"), 400
        return _relay(EV_LOCKED, enc, "You have a new locked message 🔒")

    @app.post("/api/typing")
    @login_required_api
    def api_typing():
        s = get_store()
        me = _username()
        partner = s.partner_of(me)
        if partner and not s.rate_limited(f"type:{me}", 15, 60):
            s.emit(partner, EV_TYPING, {"from": me})
        return jsonify(ok=True)

    # ---------------- delivery: SSE + polling fallback ----------------
    @app.get("/api/events")
    @login_required_api
    def api_events():
        """SSE stream. The generator drains the shared fetch-and-delete queue
        every ~1s and forwards what it finds, so this works identically with
        one or many Passenger processes and needs no Redis/pubsub/daemon."""
        s = get_store()
        me = _username()

        def gen():
            last_yield = time.time()
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        events, _ = s.poll(me, 0)
                    except Exception:
                        events = []
                    for ev in events:
                        yield "event: %s\ndata: %s\n\n" % (
                            ev["type"], json.dumps(ev["payload"]))
                        last_yield = time.time()
                    if time.time() - last_yield > 15:
                        yield ": keep-alive\n\n"   # comment frame keeps proxies awake
                        last_yield = time.time()
                    time.sleep(1.0)
            except GeneratorExit:
                pass

        # If the host buffers/kills long responses, the client falls back to
        # POST /api/poll automatically (see static/js/app.js).
        resp = Response(gen(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"     # nginx/LiteSpeed: don't buffer
        return resp

    @app.post("/api/poll")
    @login_required_api
    def api_poll():
        """Short-polling fallback when SSE is buffered/blocked by the host."""
        s = get_store()
        events, _ = s.poll(_username(), 0)
        return jsonify(ok=True, events=events)

    # ---------------- web push subscriptions ----------------
    @app.get("/api/vapid-public-key")
    def api_vapid():
        return jsonify(ok=True, key=config.VAPID_PUBLIC_KEY or None)

    @app.post("/api/subscribe")
    @login_required_api
    def api_subscribe():
        if not config.PUSH_ENABLED:
            return jsonify(ok=False, error="push_disabled"), 503
        s = get_store()
        sub = request.get_json(silent=True) or {}
        ep = sub.get("endpoint", "")
        keys = sub.get("keys", {})
        if not (isinstance(ep, str) and ep.startswith("https://")
                and isinstance(keys.get("p256dh"), str) and len(keys["p256dh"]) <= 200
                and isinstance(keys.get("auth"), str) and len(keys["auth"]) <= 200):
            return jsonify(ok=False, error="bad_subscription"), 400
        s.save_push_sub(_username(), sub)
        return jsonify(ok=True)

    @app.post("/api/unsubscribe")
    @login_required_api
    def api_unsubscribe():
        get_store().drop_push_subs(_username())
        return jsonify(ok=True)

    # ---------------- service worker / manifest at root scope -------------
    @app.get("/sw.js")
    def service_worker():
        return app.send_static_file("js/sw.js")

    @app.get("/manifest.webmanifest")
    def manifest_route():
        return app.send_static_file("manifest.webmanifest")

    # ---------------- error handlers ----------------
    @app.errorhandler(404)
    def notfound(e):
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error="not_found"), 404
        return render_template("index.html"), 200

    @app.errorhandler(413)
    def too_big(e):
        return jsonify(ok=False, error="too_large"), 413
