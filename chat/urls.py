"""URL map for the Couple Chat app.

Pages are plain Django views; every /api/* endpoint is a DRF view except the
SSE stream, which is a raw WSGI-friendly StreamingHttpResponse view.
"""
from django.urls import path

from chat import api, views

app_name = "chat"

urlpatterns = [
    # ---------------- pages (Django UI) ----------------
    path("", views.index, name="index"),
    path("healthz", views.healthz, name="healthz"),
    path("sw.js", views.service_worker, name="sw"),                    # root scope!
    path("manifest.webmanifest", views.manifest, name="manifest"),

    # ---------------- REST API (DRF) ----------------
    path("api/login", api.LoginView.as_view(), name="api-login"),
    path("api/logout", api.LogoutView.as_view(), name="api-logout"),
    path("api/heartbeat", api.HeartbeatView.as_view(), name="api-heartbeat"),
    path("api/status", api.StatusView.as_view(), name="api-status"),
    path("api/nudge", api.NudgeView.as_view(), name="api-nudge"),
    path("api/respond", api.RespondView.as_view(), name="api-respond"),
    path("api/disconnect", api.DisconnectView.as_view(), name="api-disconnect"),
    path("api/send", api.SendView.as_view(), name="api-send"),
    path("api/send_locked", api.SendLockedView.as_view(), name="api-send-locked"),
    path("api/typing", api.TypingView.as_view(), name="api-typing"),
    path("api/poll", api.PollView.as_view(), name="api-poll"),
    path("api/vapid-public-key", api.VapidPublicKeyView.as_view(), name="api-vapid"),
    path("api/subscribe", api.SubscribeView.as_view(), name="api-subscribe"),
    path("api/unsubscribe", api.UnsubscribeView.as_view(), name="api-unsubscribe"),

    # ---------------- realtime (SSE) ----------------
    path("api/events", views.events_sse, name="api-events"),
]
