"""
DRF serializers — input validation for every API endpoint.

Usernames are validated with the exact same regex/normalisation the store
uses (single source of truth: core.store.Store.norm_username), so nothing can
sneak through one path and not the other. Locked-message fields are checked
as strict base64 — the server never parses or decodes them further.
"""
from __future__ import annotations

import re

from rest_framework import serializers

from core.store import Store

_B64_RE = re.compile(r"^[A-Za-z0-9+/]{4,8192}={0,3}$")


class UsernameField(serializers.CharField):
    """CharField that normalises + validates like the store does."""

    def to_internal_value(self, data):
        raw = super().to_internal_value(data)
        name = Store.norm_username(raw)
        if name is None:
            raise serializers.ValidationError(
                {"error": "invalid_username",
                 "message": "2–24 chars: letters, numbers, . _ - only 💕"}
            )
        return name


class LoginSerializer(serializers.Serializer):
    username = UsernameField(max_length=24)


class NudgeSerializer(serializers.Serializer):
    target = UsernameField(max_length=24)


class RespondSerializer(serializers.Serializer):
    from_user = UsernameField(max_length=24, source="frm")
    accept = serializers.BooleanField()


class MessageSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=4000, trim_whitespace=False)

    def validate_text(self, value: str) -> str:
        # Defensive control-char strip. The client renders with textContent
        # only, so HTML never executes regardless (XSS-safe by construction).
        cleaned = "".join(ch for ch in value if ch == "\n" or ord(ch) >= 32)
        if not cleaned.strip():
            raise serializers.ValidationError({"error": "empty", "message": "Empty message."})
        return cleaned


class LockedMessageSerializer(serializers.Serializer):
    """The three opaque base64 fields produced by the browser's Web Crypto."""
    salt = serializers.RegexField(_B64_RE, max_length=8192)
    iv = serializers.RegexField(_B64_RE, max_length=8192)
    ct = serializers.RegexField(_B64_RE, max_length=8192)


class PushSubscriptionSerializer(serializers.Serializer):
    """Minimal shape check for a PushSubscription JSON blob."""
    endpoint = serializers.URLField(max_length=2048)
    keys = serializers.DictField(child=serializers.CharField(max_length=200))

    def validate_endpoint(self, value: str) -> str:
        if not value.startswith("https://"):
            raise serializers.ValidationError("endpoint must be https")
        return value

    def validate_keys(self, value: dict) -> dict:
        if not isinstance(value.get("p256dh"), str) or not value.get("p256dh"):
            raise serializers.ValidationError("missing p256dh")
        if not isinstance(value.get("auth"), str) or not value.get("auth"):
            raise serializers.ValidationError("missing auth")
        return value
