"""
Shared, TTL-expiring state store for Couple Chat.

Why this exists
---------------
Phusion Passenger may spawn *multiple* worker processes for the same Django
app, so Python globals cannot hold shared state (usernames, pairings,
queues...). Everything goes through this small key/value + queue store which
is backed by:

* MySQL   when ENV == "prod"   (cPanel shared hosting)
* SQLite  otherwise            (local development; file-based so several
                                processes/threads can still share it)

IMPORTANT — these tables are managed with raw SQL by this module itself
(``kv`` and ``events``).  They are deliberately NOT Django models: Django's
migrations/ORM must not touch them because they are a transient operational
cache, not application data.

Privacy contract
----------------
Only the minimum operational data is stored, every row carries an expiry and
is swept constantly:

* kv table : usernames (presence), pairings, pending nudges, push subscriptions,
             sliding-window rate-limit counters
* events   : transient relay queue for SSE/polling — message payloads live here
             only until they are fetched once (fetch-and-delete), then are
             removed; anything undelivered is purged after QUEUE_EVENT_TTL.
             Nothing about chat content persists beyond that short window and
             locked messages only ever contain {salt, iv, ciphertext} blobs
             which are meaningless without the sender-chosen password.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import threading
import time
from typing import Any, Optional

from core import settings as config

TTL_PRESENCE = config.PRESENCE_TTL
TTL_NUDGE = config.NUDGE_TTL
TTL_PAIR = 30 * 24 * 3600  # pairings live until explicitly disconnected / TTL

_json_dumps = lambda v: json.dumps(v, separators=(",", ":"))  # noqa: E731
_json_loads = lambda s: json.loads(s) if s is not None else None  # noqa: E731

_USERNAME_RE = re.compile(r"[A-Za-z0-9_.\-]{2,24}")


# ===========================================================================
# SQLite backend (development)
# ===========================================================================
class SqliteBackend:
    """Thread-safe SQLite store. WAL mode lets several Passenger processes
    share the same file safely."""

    def __init__(self, path: str):
        import os
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL,
                    expires_at REAL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_events_user ON events(username, id);
                """
            )

    # -- kv ---------------------------------------------------------------
    def kv_get(self, key: str) -> Optional[Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT v, expires_at FROM kv WHERE k = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            v, exp = row
            if exp is not None and exp < time.time():
                self._conn.execute("DELETE FROM kv WHERE k = ?", (key,))
                self._conn.commit()
                return None
            return _json_loads(v)

    def kv_set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        exp = time.time() + ttl if ttl else None
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv(k, v, expires_at) VALUES(?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, expires_at=excluded.expires_at",
                (key, _json_dumps(value), exp),
            )

    def kv_delete(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM kv WHERE k = ?", (key,))

    def kv_list_prefix(self, prefix: str) -> list[tuple[str, Any]]:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT k, v FROM kv WHERE k LIKE ? AND (expires_at IS NULL OR expires_at > ?)",
                (prefix + "%", now),
            ).fetchall()
            stale = self._conn.execute(
                "SELECT k FROM kv WHERE k LIKE ? AND expires_at IS NOT NULL AND expires_at <= ?",
                (prefix + "%", now),
            ).fetchall()
            if stale:
                self._conn.executemany("DELETE FROM kv WHERE k = ?", [(r[0],) for r in stale])
                self._conn.commit()
        return [(k, _json_loads(v)) for k, v in rows]

    # -- event queue --------------------------------------------------------
    def enqueue(self, username: str, etype: str, payload: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events(username, type, payload, created_at) VALUES(?,?,?,?)",
                (username, etype, _json_dumps(payload), time.time()),
            )
            cnt = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE username = ?", (username,)
            ).fetchone()[0]
            if cnt > config.QUEUE_MAX_PER_USER:
                self._conn.execute(
                    "DELETE FROM events WHERE id IN ("
                    " SELECT id FROM events WHERE username = ? ORDER BY id LIMIT ?)",
                    (username, cnt - config.QUEUE_MAX_PER_USER),
                )

    def fetch_new(self, username: str, after_id: int) -> tuple[list[dict], int]:
        """Fetch-and-delete: each event is delivered at most once."""
        now = time.time()
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, type, payload FROM events "
                "WHERE username = ? AND id > ? ORDER BY id LIMIT 100",
                (username, after_id),
            ).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                qmarks = ",".join("?" * len(ids))
                self._conn.execute(f"DELETE FROM events WHERE id IN ({qmarks})", ids)
            # safety sweep: drop anything undelivered for too long
            self._conn.execute("DELETE FROM events WHERE created_at < ?",
                                (now - config.QUEUE_EVENT_TTL,))
        out = [{"id": i, "type": t, "payload": _json_loads(p)} for i, t, p in rows]
        last = out[-1]["id"] if out else after_id
        return out, last

    def purge_user(self, username: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM events WHERE username = ?", (username,))

    def sweep(self) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM kv WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
            )
            self._conn.execute("DELETE FROM events WHERE created_at < ?",
                               (now - config.QUEUE_EVENT_TTL,))


# ===========================================================================
# MySQL backend (production)
# ===========================================================================
class MysqlBackend:
    """PyMySQL-backed store mirroring the SQLite API. Uses INSERT ... ON
    DUPLICATE KEY UPDATE for upserts and GET_LOCK for cross-process atomicity
    where needed (nudge acceptance)."""

    def __init__(self, cfg: dict):
        import pymysql  # imported lazily so dev environments don't need it
        self._pymysql = pymysql
        self._cfg = dict(cfg, autocommit=True, charset="utf8mb4",
                         connect_timeout=10, read_timeout=15, write_timeout=15)
        self._local = threading.local()
        self._init_schema()

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._pymysql.connect(**self._cfg)
            self._local.conn = c
        try:
            c.ping(reconnect=True)
        except Exception:
            c = self._pymysql.connect(**self._cfg)
            self._local.conn = c
        return c

    def _init_schema(self) -> None:
        cur = self._conn().cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS kv (
                 k VARCHAR(191) PRIMARY KEY,
                 v TEXT NOT NULL,
                 expires_at DOUBLE NULL,
                 INDEX ix_kv_expires (expires_at)
               ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS events (
                 id BIGINT AUTO_INCREMENT PRIMARY KEY,
                 username VARCHAR(64) NOT NULL,
                 type VARCHAR(32) NOT NULL,
                 payload TEXT NOT NULL,
                 created_at DOUBLE NOT NULL,
                 INDEX ix_events_user (username, id),
                 INDEX ix_events_created (created_at)
               ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
        )
        cur.close()

    # -- kv ---------------------------------------------------------------
    def kv_get(self, key: str) -> Optional[Any]:
        cur = self._conn().cursor()
        cur.execute("SELECT v, expires_at FROM kv WHERE k = %s", (key,))
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None
        v, exp = row
        if exp is not None and exp < time.time():
            self.kv_delete(key)
            return None
        return _json_loads(v)

    def kv_set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        exp = time.time() + ttl if ttl else None
        cur = self._conn().cursor()
        cur.execute(
            "INSERT INTO kv(k,v,expires_at) VALUES(%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE v=VALUES(v), expires_at=VALUES(expires_at)",
            (key, _json_dumps(value), exp),
        )
        cur.close()

    def kv_delete(self, key: str) -> None:
        cur = self._conn().cursor()
        cur.execute("DELETE FROM kv WHERE k = %s", (key,))
        cur.close()

    def kv_list_prefix(self, prefix: str) -> list[tuple[str, Any]]:
        now = time.time()
        cur = self._conn().cursor()
        cur.execute(
            "SELECT k, v FROM kv WHERE k LIKE %s AND (expires_at IS NULL OR expires_at > %s)",
            (prefix + "%", now),
        )
        rows = cur.fetchall()
        cur.execute(
            "DELETE FROM kv WHERE k LIKE %s AND expires_at IS NOT NULL AND expires_at <= %s",
            (prefix + "%", now),
        )
        cur.close()
        return [(k, _json_loads(v)) for k, v in rows]

    # -- event queue --------------------------------------------------------
    def enqueue(self, username: str, etype: str, payload: dict) -> None:
        cur = self._conn().cursor()
        cur.execute(
            "INSERT INTO events(username,type,payload,created_at) VALUES(%s,%s,%s,%s)",
            (username, etype, _json_dumps(payload), time.time()),
        )
        cur.execute("SELECT COUNT(*) FROM events WHERE username=%s", (username,))
        cnt = cur.fetchone()[0]
        if cnt > config.QUEUE_MAX_PER_USER:
            cur.execute(
                "DELETE e FROM events e JOIN ("
                "  SELECT id FROM events WHERE username=%s ORDER BY id LIMIT %s"
                ") d ON e.id = d.id",
                (username, cnt - config.QUEUE_MAX_PER_USER),
            )
        cur.close()

    def fetch_new(self, username: str, after_id: int) -> tuple[list[dict], int]:
        cur = self._conn().cursor()
        cur.execute(
            "SELECT id, type, payload FROM events "
            "WHERE username = %s AND id > %s ORDER BY id LIMIT 100",
            (username, after_id),
        )
        rows = cur.fetchall()
        ids = [r[0] for r in rows]
        if ids:
            qmarks = ",".join(["%s"] * len(ids))
            cur.execute(f"DELETE FROM events WHERE id IN ({qmarks})", ids)
        cur.execute("DELETE FROM events WHERE created_at < %s",
                    (time.time() - config.QUEUE_EVENT_TTL,))
        cur.close()
        out = [{"id": i, "type": t, "payload": _json_loads(p)} for i, t, p in rows]
        last = out[-1]["id"] if out else after_id
        return out, last

    def purge_user(self, username: str) -> None:
        cur = self._conn().cursor()
        cur.execute("DELETE FROM events WHERE username = %s", (username,))
        cur.close()

    def sweep(self) -> None:
        now = time.time()
        cur = self._conn().cursor()
        cur.execute("DELETE FROM kv WHERE expires_at IS NOT NULL AND expires_at <= %s", (now,))
        cur.execute("DELETE FROM events WHERE created_at < %s", (now - config.QUEUE_EVENT_TTL,))
        cur.close()

    # -- distributed lock (used for nudge accept race-safety) ---------------
    def get_lock(self, name: str, timeout: int = 3) -> bool:
        cur = self._conn().cursor()
        cur.execute("SELECT GET_LOCK(%s, %s)", (name, timeout))
        ok = cur.fetchone()[0] == 1
        cur.close()
        return ok

    def release_lock(self, name: str) -> None:
        cur = self._conn().cursor()
        cur.execute("SELECT RELEASE_LOCK(%s)", (name,))
        cur.fetchone()
        cur.close()


# ===========================================================================
# Unified Store facade — domain helpers on top of the raw backends
# ===========================================================================
class Store:
    def __init__(self):
        self._write_counter = 0
        if config.DB_BACKEND == "mysql":
            if not config.MYSQL_CONFIG["database"]:
                raise RuntimeError(
                    "ENV=prod requires DB_NAME/DB_USER/DB_PASSWORD env vars (MySQL)."
                )
            self._be = MysqlBackend(config.MYSQL_CONFIG)
        else:
            self._be = SqliteBackend(config.SQLITE_PATH)

    # ----- cross-process locking (no-op on SQLite: WAL already serialises) --
    def lock(self, name: str) -> bool:
        if isinstance(self._be, MysqlBackend):
            return self._be.get_lock("cc_" + name)
        return True

    def unlock(self, name: str) -> None:
        if isinstance(self._be, MysqlBackend):
            self._be.release_lock("cc_" + name)

    def _maybe_sweep(self) -> None:
        self._write_counter += 1
        if self._write_counter % config.SUB_SWEEP_EVERY == 0:
            try:
                self._be.sweep()
            except Exception:
                pass  # best-effort; lazy expiry keeps correctness anyway

    # ----- usernames / presence --------------------------------------------
    @staticmethod
    def norm_username(raw: str) -> Optional[str]:
        """Validate + normalise a username. Returns None when invalid.
        Allowed: letters, numbers, underscore, hyphen, dot; 2-24 chars.
        Stored case-insensitively (lower-cased)."""
        if not isinstance(raw, str):
            return None
        name = raw.strip()
        if not _USERNAME_RE.fullmatch(name):
            return None
        return name.lower()

    def is_online(self, username: str) -> bool:
        return self._be.kv_get(f"user:{username}") is not None

    def register(self, username: str) -> bool:
        """Claim a username. Fails if currently taken."""
        if self._be.kv_get(f"user:{username}") is not None:
            return False
        self._be.kv_set(f"user:{username}", {"since": time.time()}, TTL_PRESENCE)
        self._maybe_sweep()
        return True

    def heartbeat(self, username: str) -> None:
        self._be.kv_set(f"user:{username}", {"since": time.time()}, TTL_PRESENCE)

    def unregister(self, username: str) -> None:
        self._be.kv_delete(f"user:{username}")
        self._be.purge_user(username)

    def all_active_usernames(self) -> list[str]:
        return sorted(k.split(":", 1)[1] for k, _ in self._be.kv_list_prefix("user:"))

    # ----- pairings ----------------------------------------------------------
    def partner_of(self, username: str) -> Optional[str]:
        return self._be.kv_get(f"pair:{username}")

    def pair(self, a: str, b: str) -> None:
        self._be.kv_set(f"pair:{a}", b, TTL_PAIR)
        self._be.kv_set(f"pair:{b}", a, TTL_PAIR)

    def unpair(self, username: str) -> Optional[str]:
        partner = self.partner_of(username)
        self._be.kv_delete(f"pair:{username}")
        if partner:
            self._be.kv_delete(f"pair:{partner}")
            self._be.purge_user(partner)
        self._be.purge_user(username)
        return partner

    # ----- nudges -------------------------------------------------------------
    def add_nudge(self, frm: str, to: str) -> None:
        self._be.kv_set(f"nudge:{to}:{frm}", {"from": frm, "ts": time.time()}, TTL_NUDGE)
        self._maybe_sweep()

    def take_nudge(self, frm: str, to: str) -> bool:
        """Atomically consume a pending nudge. False if absent/expired."""
        key = f"nudge:{to}:{frm}"
        val = self._be.kv_get(key)
        if val is None:
            return False
        self._be.kv_delete(key)
        return True

    def clear_nudges_for(self, username: str) -> None:
        """Remove nudges addressed to *and* sent by this user."""
        for k, _ in self._be.kv_list_prefix(f"nudge:{username}:"):
            self._be.kv_delete(k)
        for k, _ in self._be.kv_list_prefix("nudge:"):
            parts = k.split(":", 2)
            if len(parts) == 3 and parts[2] == username:
                self._be.kv_delete(k)

    # ----- web-push subscriptions (kept briefly — see privacy note) ---------
    def save_push_sub(self, username: str, sub: dict) -> None:
        endpoint_hash = base64.urlsafe_b64encode(
            hashlib.sha256(sub.get("endpoint", "").encode()).digest()[:16]
        ).decode()
        self._be.kv_set(f"push:{username}:{endpoint_hash}", sub, config.PUSH_SUB_TTL)
        self._maybe_sweep()

    def push_subs(self, username: str) -> list[dict]:
        return [v for _, v in self._be.kv_list_prefix(f"push:{username}:")]

    def drop_push_subs(self, username: str) -> None:
        for k, _ in self._be.kv_list_prefix(f"push:{username}:"):
            self._be.kv_delete(k)

    # ----- rate limiting (sliding window in the shared kv store) -------------
    def rate_limited(self, bucket: str, limit: int, window: int) -> bool:
        """True when the caller has exceeded `limit` actions in `window` secs."""
        now = time.time()
        key = f"rl:{bucket}"
        hits = self._be.kv_get(key) or []
        hits = [h for h in hits if now - h < window]
        if len(hits) >= limit:
            self._be.kv_set(key, hits, window)
            return True
        hits.append(now)
        self._be.kv_set(key, hits, window)
        return False

    # ----- event queue ---------------------------------------------------------
    def emit(self, username: str, etype: str, payload: dict) -> None:
        self._be.enqueue(username, etype, payload)

    def poll(self, username: str, after_id: int) -> tuple[list[dict], int]:
        return self._be.fetch_new(username, after_id)


_store_singleton: Optional[Store] = None
_store_lock = threading.Lock()


def get_store() -> Store:
    """Lazily create the process-wide store wrapper (DB connections are
    per-thread/process; the *data* is shared through SQLite/MySQL)."""
    global _store_singleton
    if _store_singleton is None:
        with _store_lock:
            if _store_singleton is None:
                _store_singleton = Store()
    return _store_singleton
