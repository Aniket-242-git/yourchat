"""
DRF API views — the whole Couple Chat REST surface.

Real-time strategy (cPanel-friendly, no WebSockets / no daemons):
  * Primary : Server-Sent Events (SSE) on GET /api/events   (chat/views.py)
  * Fallback: short polling on POST /api/poll (client switches automatically)
Both read from the SAME fetch-and-delete event queue in the shared store, so
each queued event (message / nudge / pairing signal) is delivered at most once.

Privacy: message plaintext only passes through as an opaque string that is
deleted right after its single delivery. Locked messages are {salt, iv, ct}
base64 blobs — the server can never read them.

Auth model: there is NO Django User. "Authentication" = a session key
``cc_username`` set after a successful /api/login claim. CSRF protection is
provided by Django's CsrfViewMiddleware; the SPA sends the token in the
X-CSRF-Token header (double-submit against the signed session).
"""
from __future__ import annotations

import json
import time

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from chat.serializers import (
    LockedMessageSerializer,
    LoginSerializer,
    MessageSerializer,
    NudgeSerializer,
    PushSubscriptionSerializer,
    RespondSerializer,
)
from core import settings as core_config
from core.push import notify_many
from core.store import get_store

SESSION_KEY = "cc_username"


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


# ===========================================================================
# Helpers
# ===========================================================================
def current_username(request) -> str | None:
    """The claimed username stored in the server-side session, if any."""
    return request.session.get(SESSION_KEY)


class SessionClaimRequired(BasePermission):
    """The server-side session must hold a claimed username."""
    message = {"error": "not_logged_in", "message": "Please pick a name first 💕"}

    def has_permission(self, request, view):
        name = current_username(request)
        if not name:
            return False
        request.cc_user = name
        return True


class LivePresenceRequired(BasePermission):
    """On top of a claim: the username's presence row must still be alive in
    the shared store (a lapsed TTL means the browser was gone too long and
    another client may have taken the name — force a re-login)."""

    def has_permission(self, request, view):
        if not SessionClaimRequired().has_permission(request, view):
            return False
        s = get_store()
        if not s.is_online(request.cc_user):
            request.session.pop(SESSION_KEY, None)
            return False
        return True


class RateLimited(APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = "Slow down a little 💨"
    default_code = "rate_limited"


def api_exception_handler(exc, context):
    """Keep DRF's error bodies in the same {ok:false,error,message} shape the
    frontend understands, including validation errors."""
    from rest_framework.views import exception_handler

    resp = exception_handler(exc, context)
    if resp is None:
        return None
    data = resp.data
    if isinstance(data, dict) and "detail" in data:
        detail = data["detail"]
        code = getattr(detail, "code", str(detail))
        resp.data = {"ok": False, "error": str(code), "message": str(detail)}
    elif isinstance(data, dict):
        # Serializer errors: our fields raise dicts like
        # {"username": {"error": ..., "message": ...}} — flatten the first one.
        for field, errs in data.items():
            if isinstance(errs, list) and errs:
                first = errs[0]
                if isinstance(first, dict) and "error" in first:
                    resp.data = {"ok": False, **first}
                    break
                msg = first if isinstance(first, str) else str(first)
                resp.data = {"ok": False, "error": f"invalid_{field}", "message": msg}
                break
        else:
            resp.data = {"ok": False, "error": "bad_request", "message": "Bad request."}
    return resp


def client_ip(request) -> str:
    """Real client IP behind cPanel's LiteSpeed/Apache proxy."""
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown") or "unknown"


# ===========================================================================
# Views
# ===========================================================================
class LoginView(APIView):
    """Claim a username (no password — this app has no accounts)."""
    authentication_classes = []          # CSRF handled by Django middleware
    permission_classes = [AllowAny]

    def post(self, request):
        ser = LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        name = ser.validated_data["username"]

        s = get_store()
        if s.rate_limited(f"login:{client_ip(request)}|{name}", *core_config.RL_LOGIN):
            return Response({"ok": False, "error": "rate_limited",
                             "message": "Too many attempts — wait a minute 🙈"},
                            status=429)
        if not s.register(name):
            return Response({"ok": False, "error": "taken",
                             "message": f"“{name}” is already here. Try another name? 💕"},
                            status=409)
        request.session[SESSION_KEY] = name
        request.session.cycle_key()      # prevent session fixation
        return Response({"ok": True, "username": name})


class LogoutView(APIView):
    authentication_classes = []
    permission_classes = [LivePresenceRequired]

    def post(self, request):
        s = get_store()
        me = request.cc_user
        partner = s.unpair(me)
        if partner:
            s.emit(partner, EV_PARTNER_LEFT, {"by": me})
        s.clear_nudges_for(me)
        s.drop_push_subs(me)             # privacy: forget push endpoints now
        s.unregister(me)
        request.session.pop(SESSION_KEY, None)
        request.session.flush()
        return Response({"ok": True})


class HeartbeatView(APIView):
    """Refresh presence TTL; tells the client whether it is still paired."""
    authentication_classes = []
    permission_classes = [SessionClaimRequired]

    def post(self, request):
        s = get_store()
        me = request.cc_user
        if not s.is_online(me):
            # Presence TTL lapsed (e.g. laptop slept). Re-claim the name if it
            # is still free; otherwise the session is dead -> pick a new name.
            if not s.register(me):
                request.session.pop(SESSION_KEY, None)
                return Response({"ok": False, "error": "expired",
                                 "message": "Session expired — please pick a name again."},
                                status=401)
        s.heartbeat(me)
        partner = s.partner_of(me)
        return Response({"ok": True, "partner": partner,
                         "partner_online": bool(partner and s.is_online(partner))})


class StatusView(APIView):
    authentication_classes = []
    permission_classes = [SessionClaimRequired]

    def get(self, request):
        s = get_store()
        me = request.cc_user
        partner = s.partner_of(me)
        return Response({"ok": True, "username": me, "partner": partner,
                         "partner_online": bool(partner and s.is_online(partner)),
                         "active": s.all_active_usernames()})


class NudgeView(APIView):
    """Send a connect request ("poke") to another online username."""
    authentication_classes = []
    permission_classes = [LivePresenceRequired]

    def post(self, request):
        ser = NudgeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        target = ser.validated_data["target"]

        s = get_store()
        me = request.cc_user
        if target == me:
            return Response({"ok": False, "error": "self_nudge",
                             "message": "You can't chat with yourself 😅"}, status=400)
        if s.rate_limited(f"nudge:{me}", *core_config.RL_NUDGE):
            return Response({"ok": False, "error": "rate_limited",
                             "message": "Too many nudges — wait a minute 💗"}, status=429)
        # Rule: one username connects to ONE other username at a time.
        if s.partner_of(me):
            return Response({"ok": False, "error": "already_paired_you",
                             "message": "You're already connected. Disconnect first 💔"},
                            status=409)
        if s.partner_of(target):
            return Response({"ok": False, "error": "already_paired_target",
                             "message": f"{target} is already chatting with someone else 💔"},
                            status=409)
        if not s.is_online(target):
            return Response({"ok": False, "error": "offline",
                             "message": f"{target} isn't online right now 🌙"}, status=404)

        s.add_nudge(me, target)
        s.emit(target, EV_NUDGE, {"from": me})
        # Tab may be backgrounded/closed while presence is still fresh —
        # push covers the "browser open but tab asleep" case too.
        notify_many(s.push_subs(target), "Couple Chat 💌",
                    f"{me} wants to connect with you!", tag=f"nudge:{me}")
        return Response({"ok": True, "message": f"Nudge sent to {target} 👋"})


class RespondView(APIView):
    """Accept or decline a pending nudge."""
    authentication_classes = []
    permission_classes = [LivePresenceRequired]

    def post(self, request):
        ser = RespondSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        frm = ser.validated_data["frm"]
        accept = ser.validated_data["accept"]

        s = get_store()
        me = request.cc_user

        if accept:
            # Serialise acceptance across Passenger processes/workers.
            if not s.lock(f"pair:{me}"):
                return Response({"ok": False, "error": "busy",
                                 "message": "Try again in a second 🙈"}, status=409)
            try:
                if s.partner_of(me) or s.partner_of(frm):
                    return Response({"ok": False, "error": "already_paired",
                                     "message": "One connection at a time — disconnect first 💔"},
                                    status=409)
                if not s.take_nudge(frm, me):
                    return Response({"ok": False, "error": "stale",
                                     "message": "That request expired ⏳"}, status=410)
                s.pair(me, frm)
                s.emit(frm, EV_ACCEPT, {"from": me})
                s.emit(me, EV_ACCEPT, {"from": frm})
            finally:
                s.unlock(f"pair:{me}")
            return Response({"ok": True, "partner": frm})

        s.take_nudge(frm, me)
        s.emit(frm, EV_DECLINE, {"from": me})
        return Response({"ok": True})


class DisconnectView(APIView):
    authentication_classes = []
    permission_classes = [LivePresenceRequired]

    def post(self, request):
        s = get_store()
        me = request.cc_user
        partner = s.unpair(me)          # frees BOTH users
        s.clear_nudges_for(me)
        if partner:
            s.emit(partner, EV_PARTNER_LEFT, {"by": me})
            notify_many(s.push_subs(partner), "Couple Chat",
                        "Your partner disconnected.", tag="left")
        return Response({"ok": True, "partner": partner})


# --------------------------- chat relay ------------------------------------
def _relay(request, ev_type: str, payload: dict, push_body: str) -> Response:
    """Shared logic for SendView and SendLockedView."""
    s = get_store()
    me = request.cc_user
    partner = s.partner_of(me)
    if not partner:
        return Response({"ok": False, "error": "not_paired",
                         "message": "You're not connected to anyone right now."},
                        status=409)
    if s.rate_limited(f"msg:{me}", *core_config.RL_MSG):
        return Response({"ok": False, "error": "rate_limited",
                         "message": "Slow down a little 💨"}, status=429)
    raw = json.dumps(payload, separators=(",", ":"))
    if len(raw.encode()) > core_config.MAX_EVENT_BYTES:
        return Response({"ok": False, "error": "too_large",
                         "message": "Message too long."}, status=413)
    payload["from"] = me
    payload["ts"] = int(time.time())
    s.emit(partner, ev_type, payload)          # queued, deleted on delivery
    if not s.is_online(partner):
        # Content-free notification — never includes the message text.
        notify_many(s.push_subs(partner), "Couple Chat 💌", push_body,
                    tag=f"msg:{me}")
    return Response({"ok": True})


class SendView(APIView):
    authentication_classes = []
    permission_classes = [LivePresenceRequired]

    def post(self, request):
        ser = MessageSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return _relay(request, EV_MESSAGE,
                      {"text": ser.validated_data["text"]},
                      "You have a new message 💬")


class SendLockedView(APIView):
    """Relay an end-to-end encrypted message. The server sees ONLY three
    base64 strings (salt, iv, ciphertext); it cannot decrypt or read them."""
    authentication_classes = []
    permission_classes = [LivePresenceRequired]

    def post(self, request):
        ser = LockedMessageSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        pack = {k: ser.validated_data[k] for k in ("salt", "iv", "ct")}
        return _relay(request, EV_LOCKED, pack, "You have a new locked message 🔒")


class TypingView(APIView):
    authentication_classes = []
    permission_classes = [LivePresenceRequired]

    def post(self, request):
        s = get_store()
        me = request.cc_user
        partner = s.partner_of(me)
        if partner and not s.rate_limited(f"type:{me}", *core_config.RL_TYPING):
            s.emit(partner, EV_TYPING, {"from": me})
        return Response({"ok": True})


class PollView(APIView):
    """Short-polling fallback when SSE is buffered/blocked by the host."""
    authentication_classes = []
    permission_classes = [LivePresenceRequired]

    def post(self, request):
        s = get_store()
        events, _ = s.poll(request.cc_user, 0)
        return Response({"ok": True, "events": events})


# --------------------------- web push --------------------------------------
class VapidPublicKeyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"ok": True, "key": core_config.VAPID_PUBLIC_KEY or None})


class SubscribeView(APIView):
    authentication_classes = []
    permission_classes = [LivePresenceRequired]

    def post(self, request):
        if not core_config.PUSH_ENABLED:
            return Response({"ok": False, "error": "push_disabled"}, status=503)
        ser = PushSubscriptionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        get_store().save_push_sub(request.cc_user, ser.validated_data)
        return Response({"ok": True})


class UnsubscribeView(APIView):
    authentication_classes = []
    permission_classes = [LivePresenceRequired]

    def post(self, request):
        get_store().drop_push_subs(request.cc_user)
        return Response({"ok": True})
