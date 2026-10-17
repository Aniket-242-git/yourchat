"""
End-to-end tests for Couple Chat (Django test client + core store).

Mirrors the original Flask-era suite, adapted to Django + DRF:

  * username validation / duplicate claim / case-insensitive canonicalisation
  * nudge -> accept -> paired roundtrip; one-pairing-at-a-time rule
  * plain message relay via the poll fallback (SSE and poll share one queue)
  * fetch-and-delete semantics (no double delivery)
  * end-to-end-locked message: server relays ONLY opaque base64 {salt,iv,ct}
    — asserted with real AES-256-GCM crypto in Python
  * disconnect frees both users
  * CSRF: POST without X-CSRF-Token is rejected
  * presence TTL expiry re-claims a free name on next heartbeat
  * rate limiting on /api/send
  * UI pages: index renders csrf meta, /sw.js at root scope, security headers

Run:  python manage.py test tests -v 2
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from unittest.mock import patch

# --- force an isolated SQLite state store BEFORE django/core imports --------
os.environ["ENV"] = "dev"
os.environ.setdefault("DJANGO_SECRET_KEY", "test-key")

import django  # noqa: E402
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "couplechat.settings")
django.setup()

from django.test import Client, TestCase  # noqa: E402

from core import settings as config       # noqa: E402
from core.store import get_store          # noqa: E402


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hashlib_pbkdf2(password: str, salt: bytes) -> bytes:
    """Same parameters as the browser (PBKDF2-SHA256, 210k iterations)."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 210_000, dklen=32)


class ApiTestBase(TestCase):
    """Fresh state store + two logged-in clients per test."""

    def setUp(self):
        # Point the core store at a throwaway sqlite file for this test run.
        tmpdb = os.path.join(os.path.dirname(config.SQLITE_PATH) or ".", "test_state.sqlite")
        self._tmpdb = tmpdb
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(tmpdb + suffix)
            except OSError:
                pass
        import core.store as store_mod
        store_mod._store_singleton = None
        self._orig_path = config.SQLITE_PATH
        config.SQLITE_PATH = tmpdb

        self.ana = Client()
        self.bob = Client()
        self._login(self.ana, "ana")
        self._login(self.bob, "bob")

    def tearDown(self):
        import core.store as store_mod
        store_mod._store_singleton = None
        config.SQLITE_PATH = self._orig_path
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._tmpdb + suffix)
            except OSError:
                pass

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _csrf(client) -> str:
        c = client.cookies.get("csrftoken")
        if c is None:
            return ""
        return c.value

    def post(self, client, path, data=None):
        return client.post(path, json.dumps(data or {}),
                           content_type="application/json",
                           HTTP_X_CSRFTOKEN=self._csrf(client))

    def j(self, resp):
        return json.loads(resp.content.decode())

    def _login(self, client, name):
        r = self.post(client, "/api/login", {"username": name})
        assert r.status_code == 200, (r.status_code, r.content)
        assert self.j(r)["ok"]

    def drain(self, client):
        """Poll once and return the event list (delivery channel used by tests)."""
        r = self.post(client, "/api/poll", {})
        assert r.status_code == 200, r.content
        return self.j(r)["events"]

    def connect(self):
        """ana nudges bob, bob accepts; both queues drained."""
        r = self.post(self.ana, "/api/nudge", {"target": "bob"})
        assert self.j(r)["ok"], r.content
        evs = self.drain(self.bob)
        assert [e["type"] for e in evs] == ["nudge"], evs
        r = self.post(self.bob, "/api/respond", {"from_user": "ana", "accept": True})
        assert self.j(r)["ok"], r.content
        assert {e["type"] for e in self.drain(self.bob)} == {"accept"}
        assert {e["type"] for e in self.drain(self.ana)} == {"accept"}


class LoginTests(ApiTestBase):
    def test_valid_login_and_status(self):
        st = self.j(self.ana.get("/api/status"))
        self.assertTrue(st["ok"])
        self.assertEqual(st["username"], "ana")
        self.assertIn("ana", st["active"])
        self.assertIn("bob", st["active"])

    def test_invalid_username_rejected(self):
        r = self.post(Client(), "/api/login", {"username": "a b!"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.j(r)["ok"])

    def test_duplicate_claim_conflict(self):
        carol = Client()
        r = self.post(carol, "/api/login", {"username": "ANA"})   # case-insensitive
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.j(r)["error"], "taken")

    def test_case_insensitive_canonicalisation(self):
        dave = Client()
        r = self.post(dave, "/api/login", {"username": "Dave_"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.j(r)["username"], "dave_")

    def test_csrf_required_on_post(self):
        c = Client()
        tok = self._csrf(c)
        r = c.post("/api/login", json.dumps({"username": "eve"}),
                   content_type="application/json")               # no header at all
        self.assertEqual(r.status_code, 403)
        r2 = c.post("/api/login", json.dumps({"username": "eve"}),
                    content_type="application/json", HTTP_X_CSRFTOKEN=tok)
        self.assertEqual(r2.status_code, 200)


class PairingTests(ApiTestBase):
    def test_nudge_accept_roundtrip(self):
        self.connect()
        s = get_store()
        self.assertEqual(s.partner_of("ana"), "bob")
        self.assertEqual(s.partner_of("bob"), "ana")

    def test_nudge_offline_target(self):
        r = self.post(self.ana, "/api/nudge", {"target": "ghost"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.j(r)["error"], "offline")

    def test_self_nudge_rejected(self):
        r = self.post(self.ana, "/api/nudge", {"target": "Ana"})
        self.assertEqual(r.status_code, 400)

    def test_decline_flow(self):
        self.post(self.ana, "/api/nudge", {"target": "bob"})
        self.drain(self.bob)
        r = self.post(self.bob, "/api/respond", {"from_user": "ana", "accept": False})
        self.assertTrue(self.j(r)["ok"])
        self.assertIsNone(get_store().partner_of("ana"))
        evs = self.drain(self.ana)
        self.assertEqual([e["type"] for e in evs], ["decline"])

    def test_one_pairing_at_a_time(self):
        self.connect()
        carol = Client()
        self._login(carol, "carol")
        r = self.post(carol, "/api/nudge", {"target": "ana"})
        self.assertEqual(r.status_code, 409)                 # ana already paired
        self.assertEqual(self.j(r)["error"], "already_paired_target")

    def test_disconnect_frees_both(self):
        self.connect()
        r = self.post(self.ana, "/api/disconnect", {})
        self.assertTrue(self.j(r)["ok"])
        s = get_store()
        self.assertIsNone(s.partner_of("ana"))
        self.assertIsNone(s.partner_of("bob"))
        evs = self.drain(self.bob)
        self.assertEqual([e["type"] for e in evs], ["partner_left"])

    def test_logout_clears_everything(self):
        self.connect()
        r = self.post(self.ana, "/api/logout", {})
        self.assertTrue(self.j(r)["ok"])
        s = get_store()
        self.assertFalse(s.is_online("ana"))
        self.assertIsNone(s.partner_of("ana"))
        self.assertEqual(s.push_subs("ana"), [])


class MessageTests(ApiTestBase):
    def test_plain_message_delivered_once(self):
        self.connect()
        r = self.post(self.ana, "/api/send", {"text": "hi bob 💗"})
        self.assertTrue(self.j(r)["ok"])
        evs = self.drain(self.bob)
        msgs = [e for e in evs if e["type"] == "message"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["payload"]["text"], "hi bob 💗")
        self.assertEqual(msgs[0]["payload"]["from"], "ana")
        # fetch-and-delete: second poll must be empty
        self.assertEqual(self.drain(self.bob), [])

    def test_send_without_pairing_fails(self):
        r = self.post(self.ana, "/api/send", {"text": "hello?"})
        self.assertEqual(r.status_code, 409)

    def test_empty_message_rejected(self):
        self.connect()
        r = self.post(self.ana, "/api/send", {"text": "   "})
        self.assertEqual(r.status_code, 400)

    def test_typing_indicator(self):
        self.connect()
        self.post(self.ana, "/api/typing", {})
        evs = [e["type"] for e in self.drain(self.bob)]
        self.assertIn("typing", evs)

    def test_rate_limit_messages(self):
        self.connect()
        with patch.object(config, "RL_MSG", (3, 60)):
            codes = [self.post(self.ana, "/api/send", {"text": f"m{i}"}).status_code
                     for i in range(5)]
        self.assertIn(429, codes[-2:])


class LockedMessageTests(ApiTestBase):
    """The server must only ever see opaque base64 blobs — never plaintext."""

    def _encrypt(self, text: str, password: str) -> dict:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        salt = os.urandom(16)
        iv = os.urandom(12)
        key = hashlib_pbkdf2(password, salt)
        ct = AESGCM(key).encrypt(iv, text.encode(), None)
        return {"salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}

    def test_locked_relay_is_opaque_and_decryptable_by_partner(self):
        self.connect()
        pack = self._encrypt("secret love note", "p@ss1234")
        r = self.post(self.ana, "/api/send_locked", pack)
        self.assertTrue(self.j(r)["ok"])

        evs = self.drain(self.bob)
        locked = [e for e in evs if e["type"] == "locked"]
        self.assertEqual(len(locked), 1)
        payload = locked[0]["payload"]
        # server-relayed payload contains ONLY the three base64 fields (+meta)
        self.assertEqual(set(payload) - {"from", "ts"}, {"salt", "iv", "ct"})
        self.assertNotIn("secret love note", json.dumps(evs))   # plaintext never stored

        # partner decrypts locally with the shared password
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = hashlib_pbkdf2("p@ss1234", base64.b64decode(payload["salt"]))
        plain = AESGCM(key).decrypt(base64.b64decode(payload["iv"]),
                                    base64.b64decode(payload["ct"]), None)
        self.assertEqual(plain.decode(), "secret love note")

        # wrong password => GCM auth failure (what the UI counts as a "try")
        bad = hashlib_pbkdf2("wrongpass", base64.b64decode(payload["salt"]))
        with self.assertRaises(Exception):
            AESGCM(bad).decrypt(base64.b64decode(payload["iv"]),
                                base64.b64decode(payload["ct"]), None)

    def test_malformed_lock_payload_rejected(self):
        self.connect()
        r = self.post(self.ana, "/api/send_locked",
                      {"salt": "!!notb64!!", "iv": "AAAAAAAA", "ct": "AAAAAAAA"})
        self.assertEqual(r.status_code, 400)


class PresenceTtlTests(ApiTestBase):
    def test_expired_presence_reclaimed_or_logged_out(self):
        self.connect()
        s = get_store()
        # Simulate TTL lapse: expire ana's presence row directly.
        s._be.kv_set("user:ana", {"since": 0}, ttl=-1)
        self.assertFalse(s.is_online("ana"))
        # Name still free -> heartbeat silently re-claims it.
        r = self.post(self.ana, "/api/heartbeat", {})
        self.assertTrue(self.j(r)["ok"])
        self.assertTrue(s.is_online("ana"))
        # Now the name is taken while ana's presence lapses again:
        s._be.kv_set("user:ana", {"since": 0}, ttl=-1)
        carol = Client()
        self._login(carol, "ana")                        # carol steals the name
        r = self.post(self.ana, "/api/heartbeat", {})
        self.assertEqual(r.status_code, 401)             # old session invalidated
        self.assertEqual(self.j(r)["error"], "expired")


class PushTests(ApiTestBase):
    SUB = {
        "endpoint": "https://updates.example.com/abc",
        "keys": {"p256dh": "BP" + "A" * 84, "auth": "AAAA"},
    }

    def test_subscribe_validation_and_ttl_storage(self):
        with patch.object(config, "PUSH_ENABLED", True):
            r = self.post(self.ana, "/api/subscribe", self.SUB)
        self.assertTrue(self.j(r)["ok"])
        subs = get_store().push_subs("ana")
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["endpoint"], self.SUB["endpoint"])

    def test_bad_subscription_rejected(self):
        with patch.object(config, "PUSH_ENABLED", True):
            r = self.post(self.ana, "/api/subscribe",
                          {"endpoint": "http://insecure", "keys": {}})
        self.assertEqual(r.status_code, 400)

    def test_unsubscribe_drops_all(self):
        with patch.object(config, "PUSH_ENABLED", True):
            self.post(self.ana, "/api/subscribe", self.SUB)
        r = self.post(self.ana, "/api/unsubscribe", {})
        self.assertTrue(self.j(r)["ok"])
        self.assertEqual(get_store().push_subs("ana"), [])

    def test_push_notify_called_for_offline_partner(self):
        self.connect()
        get_store().unregister("bob")                          # pretend bob went away
        with patch("chat.api.notify_many", return_value=0) as m:
            r = self.post(self.ana, "/api/send", {"text": "where are you?"})
        self.assertTrue(self.j(r)["ok"])
        m.assert_called_once()
        args = m.call_args[0]
        self.assertEqual(args[1], "Couple Chat 💌")
        self.assertNotIn("where are you?", args[2])            # content-free body


class UiTests(TestCase):
    def test_index_renders_with_csrf_meta(self):
        r = Client().get("/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('name="csrf-token"', html)
        self.assertIn("Couple Chat", html)

    def test_sw_served_at_root_scope(self):
        r = Client().get("/sw.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/javascript")
        self.assertEqual(r["Service-Worker-Allowed"], "/")

    def test_manifest(self):
        r = Client().get("/manifest.webmanifest")
        self.assertEqual(r.status_code, 200)

    def test_security_headers(self):
        r = Client().get("/")
        self.assertIn("Content-Security-Policy", r)
        self.assertIn("script-src 'self'", r["Content-Security-Policy"])
        self.assertEqual(r["X-Frame-Options"], "DENY")

    def test_healthz(self):
        r = Client().get("/healthz")
        self.assertEqual(json.loads(r.content.decode())["backend"], "sqlite")
