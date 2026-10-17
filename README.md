# 💕 Couple Chat — privacy-first 1:1 chat for two people

A minimal real-time chat for **exactly two people**. No signup, no email, no
user database, **no permanent message storage**. Users are identified only by
a transient username. Romantic pink theme, mobile-first, deployable on
**cPanel shared hosting** (Phusion Passenger + WSGI).

---

## 1. Architecture & design choices

```
Browser A (alice)                         Browser B (bob)
 ┌─────────────────────────┐               ┌─────────────────────────┐
 │ app.js  (chat state in  │               │ app.js                  │
 │  JS memory ONLY)        │               │ crypto.js PBKDF2+AES-GCM│
 │ crypto.js               │               │ sw.js  (Web Push)       │
 │ EventSource /api/events │               │ EventSource /api/events │
 └───────────┬─────────────┘               └───────────┬─────────────┘
             │ HTTPS                                   │ HTTPS
     ┌───────▼─────────────────────────────────────────▼───────┐
     │ Flask (Passenger WSGI)  passenger_wsgi.py → app/        │
     │  • auth/presence  • nudge/pairing  • relay  • push      │
     └───────────────────────────┬─────────────────────────────┘
                                 ▼
                 Shared TTL store  (the ONLY server state)
                 MySQL when ENV=prod · SQLite otherwise
      (usernames · pairings · pending nudges · push subs · event queue)
```

Key decisions and *why*:

| Decision | Reason |
|---|---|
| **SSE + polling fallback** instead of WebSockets | cPanel/LiteSpeed usually buffers or kills long-lived WS handshakes; SSE is plain HTTP and the client auto-switches to `POST /api/poll` if SSE can't open within ~8 s. |
| **Shared store (MySQL/SQLite), never Python globals** | Passenger may run multiple processes; globals would fork reality. One facade (`app/store.py`) with identical semantics on both backends. |
| **Fetch-and-delete event queue** | Each queued event is delivered at most once and removed immediately → messages exist on the server microseconds-to-seconds, never as history. SSE and polling drain the same queue. |
| **TTL everywhere** | Presence (75 s), nudges (90 s), rate counters, push subs (24 h) all carry expiry, swept lazily + periodically. Disconnect/logout actively deletes rows. |
| **Locked messages = pure relay** | Client encrypts PBKDF2(210k)→AES-256-GCM; server validates only base64 shape and relays `{salt, iv, ct}`. Password/plaintext never touch the server. |
| **Notifications are content-free** | Push payload is literally `"You have a new message"` — never text, never secrets. |
| **Vanilla HTML/CSS/JS** | No Node.js build step anywhere; files served straight from `app/static`. |

### Storage rule: MySQL vs SQLite
* `ENV=prod` → **MySQL** (tables `kv`, `events` auto-created on first request).
* anything else → **SQLite** file at `tmp/couplechat.sqlite` (WAL mode so
  several dev processes share it safely).

### Privacy trade-offs you should know about
1. **Push subscriptions must be stored briefly.** Offline notifications are
   impossible unless the server keeps the subscription JSON (endpoint + keys)
   until the browser comes back. We store them max **24 h**, delete on
   logout/disconnect, and hash the endpoint key so the raw URL isn't kept.
2. **Relay needs a moment of server-side custody.** A message exists in the
   `events` table between send and single delivery (typically < 3 s, hard-capped
   at 180 s by sweeper + per-user queue bound). Unavoidable without P2P
   transport. For locked messages even that copy is opaque ciphertext.
3. **On shared hosting you trust your host's disk.** To minimise exposure, use
   a RAM-backed engine if your host allows (`ALTER TABLE events ENGINE=MEMORY;`)
   and keep `.htaccess` access logging off (see §6). Our code never archives
   plain messages.
4. **Usernames/pairings are visible to the server operator while active** —
   the minimum metadata needed to route chats at all.

---

## 2. Folder structure

```
couple-chat/
├── passenger_wsgi.py          # cPanel/Passenger entry point
├── run.py                     # local dev runner (loads .env)
├── config.py                  # all tunables via environment variables
├── requirements.txt
├── .env.example               # documented env vars (copy to .env locally)
├── .htaccess.example          # Apache/LiteSpeed hardening snippet
├── app/
│   ├── __init__.py            # Flask factory, routes, SSE, CSRF, headers
│   ├── store.py               # shared TTL store: MySQL(prod)/SQLite(dev)
│   ├── push.py                # pywebpush VAPID sender (content-free)
│   ├── templates/index.html   # single-page UI (all screens/modals)
│   └── static/
│       ├── css/style.css      # pink romantic theme, mobile-first
│       ├── js/app.js          # UI controller, SSE + poll fallback
│       ├── js/crypto.js       # PBKDF2 + AES-256-GCM (Web Crypto)
│       ├── js/sw.js           # Service Worker (push receiver)
│       ├── manifest.webmanifest
│       └── icons/heart*.png
├── scripts/gen_vapid.py       # VAPID key generator
├── tests/test_flow.py         # end-to-end backend flow test
└── tmp/                       # SQLite lives here (gitignored)
```

---

## 3. Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # optional: add SECRET_KEY / VAPID keys
python run.py                        # http://127.0.0.1:5000  (SQLite)
python tests/test_flow.py            # full flow test, no browser needed
```

Web Push only works over HTTPS or `localhost`, so test push on the deployed
site.

---

## 4. Generate VAPID keys

```bash
python scripts/gen_vapid.py
```

Copy the three printed lines into your cPanel app environment (§6, step 4) and
delete `vapid_keys.json`.

---

## 5. Testing checklist (manual, two browsers)

Use two different browsers/devices (e.g. Chrome desktop + phone, or Chrome +
Firefox private window).

| # | Scenario | Expected |
|---|---|---|
| 1 | Alice joins `alice`; Bob joins `bob`; third tab tries `alice` | "already here" friendly error, name rejected |
| 2 | Alice nudges `bob` | Bob sees Accept/Decline modal within ~1 s (SSE) or ~2.5 s (poll) |
| 3 | Bob accepts | Both screens open the chat; header shows partner + green dot |
| 4 | Eve (3rd user) nudges `alice` | Rejected: "already chatting with someone else 💔" |
| 5 | Alice sends "hi" | Appears instantly in Bob's chat |
| 6 | Alice refreshes | Chat empty (memory cleared); pairing survives; reconnect notice shown |
| 7 | 🔒 toggle → send locked msg with password `love123` | Bob sees dashed 🔒 bubble; server holds only base64 blobs |
| 8 | Bob enters wrong password | Error + tries-left counter; after 5 tries bubble permanently locks |
| 9 | Bob enters right password | Plaintext appears locally, auto-hides to "(opened)" after 30 s |
| 10 | Bob closes his tab; Alice sends a message | Bob's device gets push **"You have a new message 💬"** with no text |
| 11 | Bob offline; Alice nudges | Friendly "isn't online right now 🌙" |
| 12 | Either taps Disconnect | Both return to waiting screen; both free to pair again |
| 13 | Flood >30 msgs/min | 429 "Slow down a little 💨" |
| 14 | POST without CSRF header | 400 `csrf_failed` |
| 15 | Host buffers SSE (some LiteSpeed setups) | After ~8 s banner "using polling mode 🔄", chat still works |

Automated version of 1–14: `python tests/test_flow.py`.

---

## 6. cPanel deployment guide (step-by-step)

**Prereq:** cPanel with *Setup Python App* (Python 3.8+), own domain/subdomain
with AutoSSL.

1. **Files up** — Upload this whole folder to e.g. `~/couplechat`
   (File Manager or git). *Not* inside `public_html`.

2. **Create the Python app** — cPanel → *Setup Python App* → Create Application:
   * Python version: 3.10+
   * Application root: `/home/USER/couplechat`
   * Application URL: your subdomain (e.g. `chat.example.com`)
   * Application startup file: `passenger_wsgi.py`
   * Startup class: **leave blank** (Passenger uses the `application` object)

   cPanel creates a virtualenv at `~/.virtualenvs/<appname>`.

3. **Install dependencies** — click *Run pip…* in the UI, or via SSH:
   ```bash
   source ~/.virtualenvs/<appname>/bin/activate
   cd ~/couplechat && pip install -r requirements.txt
   ```
   All packages are pure-Python wheels (Flask, Werkzeug, PyMySQL, pywebpush;
   cryptography ships manylinux wheels that install fine on cPanel) — no
   compiler or root required.

4. **Environment variables** — in *Setup Python App → Add Environment Variable*:

   | Variable | Value |
   |---|---|
   | `ENV` | `prod` ← switches the store to **MySQL** |
   | `SECRET_KEY` | random hex (`python3 -c "import secrets;print(secrets.token_hex(32))"`) |
   | `DB_HOST` | `localhost` |
   | `DB_NAME` / `DB_USER` / `DB_PASSWORD` | the MySQL db you create next |
   | `VAPID_PUBLIC_KEY` `VAPID_PRIVATE_KEY` `VAPID_CLAIMS_EMAIL` | from `scripts/gen_vapid.py` |

5. **MySQL database** — cPanel → *MySQL® Databases*: create a database + user,
   grant ALL privileges. Tables `kv` and `events` are **auto-created** on the
   first request — nothing to import.

6. **HTTPS + hardening** — cPanel → *SSL/TLS Status* → Run AutoSSL (Web Push
   requires HTTPS). Copy `.htaccess.example` → `.htaccess` into the app root:
   forces HTTPS, blocks direct hits on `tmp/`, `scripts/`, `tests/`, `.env`,
   and quiets body logging where the server honours it.

7. **Restart the app** (button in Setup Python App), then open
   `https://chat.example.com/healthz` → `{"ok":true,"backend":"mysql"}`.

8. On both phones/desktops, visit the site and allow **Notifications**. Done 💕

> **SSE note:** If your host buffers `text/event-stream`, the UI automatically
> falls back to ~2.5 s polling — everything still works, just slightly less
> instant.

---

## 7. Security notes

* **XSS**: every dynamic string is rendered with `textContent` — HTML from
  usernames/messages can never execute. CSP additionally forbids inline
  scripts and all external origins.
* **Injection**: SQL fully parameterised (both backends); usernames
  whitelist-validated (`[A-Za-z0-9_.-]{2,24}`, lower-cased).
* **CSRF**: signed-session token echoed in `X-CSRF-Token` on every POST,
  compared with `secrets.compare_digest`.
* **Sessions**: HttpOnly, SameSite=Lax, Secure (prod), capped lifetime.
* **Headers**: CSP, X-Frame-Options DENY, nosniff, Referrer-Policy
  no-referrer, Permissions-Policy, HSTS in prod.
* **Rate limits**: nudges 5/min, messages 30/min, logins 10/min/IP+name,
  typing 15/min — sliding windows in the shared store (works across workers).
* **Payload caps**: 4 KB messages, 64 KB events, 128 KB request bodies.
* **Push hygiene**: zero message content in notifications; endpoints hashed;
  24 h TTL; deleted on logout.

---

## 8. Known limitations & future improvements

**Shared-hosting limitations**
* SSE may be buffered/killed by LiteSpeed/proxies → polling fallback adds ~2 s latency.
* No background workers → expired-row sweeping happens on request traffic (lazy);
  correctness still holds because every read checks expiry.
* There is no authentication ("no signup" UX): after your presence TTL lapses,
  anyone could claim your old username. Locked messages cover sensitive content.
* Push delivery depends on the browser keeping the SW alive; iOS requires the
  site to be added to the Home Screen (standard PWA-push caveat).
* Multiple Passenger processes share state through MySQL — perfectly fine for
  two users; Redis deliberately avoided.

**Possible improvements**
* WebAuthn passkeys for "remember me" identity without emails.
* Double-ratchet (Signal-style) E2E ratcheting for *all* messages, not just locked ones.
* Argon2id via WASM (`hash-wasm`) replacing PBKDF2 where available.
* Read receipts / recall as additional queue event types.
* WebRTC P2P with WebSocket signalling on a VPS for true zero-server-content.
* `ENGINE=MEMORY` tables / encrypted tmpfs to keep the relay queue off disk.

---

## 9. API reference (JSON; `X-CSRF-Token` required on every POST)

| Route | Purpose |
|---|---|
| `GET /` | SPA page (login/waiting/chat + modals) |
| `POST /api/login` / `logout` / `heartbeat` | presence lifecycle (TTL 75 s) |
| `POST /api/nudge` | send connect request (one-pair rules enforced) |
| `POST /api/respond` | accept/decline (`accept:true/false`) |
| `POST /api/disconnect` | frees both users |
| `GET /api/status` | me/partner/online list |
| `POST /api/send` | relay plain message |
| `POST /api/send_locked` | relay `{salt,iv,ct}` blob |
| `POST /api/typing` | ephemeral indicator |
| `GET /api/events` | **SSE** stream |
| `POST /api/poll` | **polling fallback** (drains same queue) |
| `POST /api/subscribe` / `unsubscribe`, `GET /api/vapid-public-key` | Web Push |
| `GET /sw.js`, `GET /manifest.webmanifest` | root-scope SW + PWA manifest |
