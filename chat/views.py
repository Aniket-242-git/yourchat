"""
Django UI views + the SSE realtime endpoint.

Pages are rendered by Django templates (requirement #6: "Django for UI").
The single-page app switches screens client-side; Django injects only the
CSRF token and VAPID public key into the template.

The SSE view is a plain Django StreamingHttpResponse generator that drains
the shared fetch-and-delete queue every second — identical semantics to the
DRF polling fallback, no ASGI/channels/Redis needed.
"""
from __future__ import annotations

import json
import time

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from chat.api import SESSION_KEY, current_username  # noqa: F401 (re-export)
from core import settings as config
from core.store import get_store


# ===========================================================================
# Pages
# ===========================================================================
def index(request):
    """The one HTML page of the app (all screens live inside it)."""
    return render(request, "index.html", {
        "vapid_public_key": config.VAPID_PUBLIC_KEY,
        "push_enabled": config.PUSH_ENABLED,
        # Django's CSRF machinery puts the token in the cookie *and* we pass
        # it to the template so the JS can echo it in the X-CSRF-Token header.
        "csrf_cookie_name": "csrftoken",
    })


@require_GET
def healthz(request):
    """Tiny ops endpoint: liveness + which storage backend is active."""
    return JsonResponse({"ok": True, "backend": config.DB_BACKEND})


@require_GET
def service_worker(request):
    """Serve /sw.js at ROOT scope (Web Push requires root-scope SW)."""
    resp = HttpResponse(content_type="application/javascript")
    with open(_static_path("js/sw.js"), "rb") as fh:
        resp.write(fh.read())
    resp["Cache-Control"] = "no-cache"
    resp["Service-Worker-Allowed"] = "/"
    return resp


@require_GET
def manifest(request):
    resp = HttpResponse(content_type="application/manifest+json")
    with open(_static_path("manifest.webmanifest"), "rb") as fh:
        resp.write(fh.read())
    resp["Cache-Control"] = "no-cache"
    return resp


def _static_path(rel: str):
    from pathlib import Path
    from django.conf import settings
    return Path(settings.BASE_DIR) / "static" / rel


# ===========================================================================
# SSE stream (primary realtime transport)
# ===========================================================================
def events_sse(request):
    """Server-Sent Events on GET /api/events.

    The generator drains the shared fetch-and-delete queue every ~1s and
    forwards what it finds, so this works identically with one or many
    Passenger processes and needs no Redis/pubsub/daemon.

    If the host buffers/kills long responses, the client falls back to
    POST /api/poll automatically (see static/js/app.js).
    """
    me = current_username(request)
    if not me or not get_store().is_online(me):
        return JsonResponse({"ok": False, "error": "not_logged_in"}, status=401)

    def gen():
        s = get_store()
        last_yield = time.time()
        yield "retry: 3000\n\n"
        while True:
            try:
                events, _ = s.poll(me, 0)
            except Exception:
                events = []          # transient DB hiccup — keep the stream alive
            for ev in events:
                yield "event: %s\ndata: %s\n\n" % (
                    ev["type"], json.dumps(ev["payload"]))
                last_yield = time.time()
            if time.time() - last_yield > 15:
                yield ": keep-alive\n\n"   # comment frame keeps proxies awake
                last_yield = time.time()
            time.sleep(1.0)

    resp = HttpResponse(gen(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"       # nginx/LiteSpeed: don't buffer
    resp["Connection"] = "keep-alive"
    return resp
