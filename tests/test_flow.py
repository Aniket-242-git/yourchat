"""End-to-end flow test using Flask's test clients (three simulated browsers).

Run from the project root:   python3 tests/test_flow.py
Uses a throw-away SQLite file — no server or MySQL needed.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# fresh sqlite db for each run (must be set BEFORE importing config)
os.environ["SQLITE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.sqlite")
os.environ.pop("ENV", None)  # force dev/SQLite path

import config  # noqa: E402
config.DB_BACKEND = "sqlite"

from app import create_app  # noqa: E402

app = create_app()
app.config["TESTING"] = True


def client():
    return app.test_client()


alice, bob, eve = client(), client(), client()
for c in (alice, bob, eve):
    c._csrf = None


def post(c, url, body):
    """POST with CSRF bootstrap: first GET / stores the token in the session."""
    if not getattr(c, "_csrf", None):
        r = c.get("/")
        m = re.search(rb'name="csrf-token" content="([^"]+)"', r.data)
        c._csrf = m.group(1).decode()
    return c.post(url, json=body, headers={"X-CSRF-Token": c._csrf})


# 1. login ---------------------------------------------------------------
r = post(alice, "/api/login", {"username": "Alice"}); assert r.status_code == 200, r.data
r = post(bob, "/api/login", {"username": "bob"});     assert r.status_code == 200, r.data
print("✓ logins ok")

r2 = client(); r2._csrf = None
r = post(r2, "/api/login", {"username": "alice"})
assert r.status_code == 409 and r.get_json()["error"] == "taken", r.data
print("✓ duplicate username rejected:", r.get_json()["message"])

r = post(eve, "/api/login", {"username": "<script>x</script>"})
assert r.status_code == 400, r.data
print("✓ invalid username rejected")

r = alice.post("/api/heartbeat", json={})
assert r.status_code == 400 and r.get_json()["error"] == "csrf_failed"
print("✓ CSRF enforced")

# 2. nudge ----------------------------------------------------------------
r = post(alice, "/api/nudge", {"target": "bob"})
assert r.status_code == 200, r.data
print("✓ nudge sent:", r.get_json()["message"])

evs = post(bob, "/api/poll", {}).get_json()["events"]
assert any(e["type"] == "nudge" and e["payload"]["from"] == "alice" for e in evs), evs
print("✓ bob received nudge event")

r = post(alice, "/api/nudge", {"target": "nobody"})
assert r.status_code == 404, r.data
print("✓ offline target rejected")

r = post(alice, "/api/nudge", {"target": "alice"})
assert r.status_code == 400, r.data
print("✓ self-nudge rejected")

# 3. accept -> pairing ------------------------------------------------------
r = post(bob, "/api/respond", {"from": "alice", "accept": True})
assert r.status_code == 200 and r.get_json()["partner"] == "alice", r.data
evs = post(alice, "/api/poll", {}).get_json()["events"]
assert any(e["type"] == "accept" for e in evs), evs
print("✓ pairing works; alice got accept event")

r = post(eve, "/api/login", {"username": "eve"}); assert r.status_code == 200
r = post(eve, "/api/nudge", {"target": "alice"})
assert r.status_code == 409 and r.get_json()["error"] == "already_paired_target", r.data
print("✓ already-paired target rejected:", r.get_json()["message"])

r = post(alice, "/api/nudge", {"target": "eve"})
assert r.status_code == 409 and r.get_json()["error"] == "already_paired_you", r.data
print("✓ already-paired sender rejected")

# 4. messages --------------------------------------------------------------
r = post(alice, "/api/send", {"text": "hey 💕 <b>not html</b>"})
assert r.status_code == 200, r.data
evs = post(bob, "/api/poll", {}).get_json()["events"]
msg = [e for e in evs if e["type"] == "message"][-1]
assert msg["payload"]["text"].startswith("hey 💕"), msg
print("✓ plain message relayed once (fetch-and-delete)")

evs2 = post(bob, "/api/poll", {}).get_json()["events"]
assert not [e for e in evs2 if e["type"] == "message"], evs2
print("✓ events deleted after delivery (no replay)")

pack = {"salt": "c2FsdHNhbHRzYWx0c2Ex", "iv": "aXZpdm9uMTIzNDU2Nzg=", "ct": "ZW5jcnlwdGVkYmxvYg=="}
r = post(alice, "/api/send_locked", pack); assert r.status_code == 200, r.data
evs = post(bob, "/api/poll", {}).get_json()["events"]
lock = [e for e in evs if e["type"] == "locked"][-1]
assert lock["payload"]["ct"] == pack["ct"] and lock["payload"]["from"] == "alice"
print("✓ locked blob relayed untouched (server never sees plaintext/password)")

r = post(alice, "/api/send_locked", {"salt": "!!", "iv": pack["iv"], "ct": pack["ct"]})
assert r.status_code == 400, r.data
print("✓ malformed encryption payload rejected")

r = post(alice, "/api/typing", {}); assert r.status_code == 200
evs = post(bob, "/api/poll", {}).get_json()["events"]
assert any(e["type"] == "typing" for e in evs)
print("✓ typing indicator delivered")

r = post(eve, "/api/send", {"text": "hi"})
assert r.status_code == 409, r.data
print("✓ unpaired send rejected")

# 5. rate limiting ----------------------------------------------------------
ok = fail = 0
for i in range(40):
    r = post(alice, "/api/send", {"text": f"flood {i}"})
    if r.status_code == 200: ok += 1
    elif r.status_code == 429: fail += 1
assert fail > 0, "rate limiter did not kick in"
print(f"✓ message flood limited ({ok} accepted, {fail} rejected)")

# 6. SSE smoke test -----------------------------------------------------------
r = alice.get("/api/events")
chunk = b""
for piece in r.response:
    chunk += piece
    if b"retry" in chunk:
        break
r.close()
assert b"retry: 3000" in chunk, chunk[:120]
print("✓ SSE stream opens with retry directive")

# 7. disconnect frees both ----------------------------------------------------
r = post(alice, "/api/disconnect", {}); assert r.status_code == 200
evs = post(bob, "/api/poll", {}).get_json()["events"]
assert any(e["type"] == "partner_left" for e in evs), evs
print("✓ disconnect frees both users")

# 8. logout releases username + clears subs -----------------------------------
r = post(alice, "/api/logout", {}); assert r.status_code == 200
r = post(bob, "/api/nudge", {"target": "alice"})
assert r.status_code == 404, r.data
print("✓ logout releases username")

print("\nALL BACKEND FLOW TESTS PASSED ❤️")
