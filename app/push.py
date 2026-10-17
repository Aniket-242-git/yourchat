"""
Web Push helper (VAPID via pywebpush).

Notifications are deliberately content-free: they only ever say something like
"You have a new message" — never the message text, sender password or any
plaintext. If push isn't configured (no VAPID keys) we silently no-op.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import config

log = logging.getLogger("couplechat.push")

# Payload size guard for browsers (Chrome caps ~4KB); ours are tiny.
_TITLE_DEFAULT = "Couple Chat 💌"


def send_push(subscription: dict, title: str, body: str, tag: Optional[str] = None) -> bool:
    """Send one push notification to a stored subscription dict.

    ``subscription`` is the raw PushSubscription JSON produced by
    ``registration.pushManager.subscribe()`` on the client.
    Returns True on success. Never raises.
    """
    if not config.PUSH_ENABLED:
        return False
    try:
        from pywebpush import webpush, WebPushException

        webpush(
            subscription_info=subscription,
            data=json.dumps({"title": title, "body": body, "tag": tag or "couplechat"}),
            vapid_private_key=config.VAPID_PRIVATE_KEY,
            vapid_claims={
                "sub": config.VAPID_CLAIMS_EMAIL,
                # iat must be within the last 24h
                "iat": int(__import__("time").time()),
            },
            ttl=30,          # don't let brokers queue stale notifications
            timeout=10,
        )
        return True
    except Exception as exc:  # expired endpoint, network hiccup etc.
        # Log ONLY exception class + endpoint host — never tokens/keys.
        log.warning("push failed (%s): %s", type(exc).__name__, _safe_endpoint(subscription))
        return False


def notify_many(subs: list[dict], title: str, body: str, tag: Optional[str] = None) -> int:
    ok = 0
    for s in subs:
        if send_push(s, title, body, tag):
            ok += 1
    return ok


def _safe_endpoint(sub: dict) -> str:
    """Reduce a push endpoint to its hostname for safe logging."""
    ep = sub.get("endpoint", "") if isinstance(sub, dict) else ""
    try:
        from urllib.parse import urlparse
        return urlparse(ep).netloc or "(invalid)"
    except Exception:
        return "(unparseable)"
