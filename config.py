"""
Couple Chat – central configuration.

All tunables live here and can be overridden with environment variables so the
same code runs in development (SQLite) and production (MySQL on cPanel).

Environment variables
---------------------
ENV                     "prod" -> MySQL, anything else -> SQLite   (default: dev)
SECRET_KEY              Flask session secret. REQUIRED in prod.
DATABASE_URL / DB settings (see _mysql_config below)
VAPID_PUBLIC_KEY        Web Push VAPID public key  (base64url)
VAPID_PRIVATE_KEY       Web Push VAPID private key (base64url)  -- keep secret!
VAPID_CLAIMS_EMAIL      Contact email for VAPID, e.g. "mailto:you@example.com"
TTLS / rate limits      see constants below
"""
import os


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------
ENV = os.environ.get("ENV", "dev").strip().lower()
IS_PROD = ENV == "prod"

# Flask session secret. In production this MUST be set in the cPanel app
# environment; we refuse to fall back to an insecure default there.
SECRET_KEY = os.environ.get("SECRET_KEY") or os.urandom(32).hex()

# --------------------------------------------------------------------------
# Storage backend selection:  MySQL when ENV == "prod", otherwise SQLite.
# --------------------------------------------------------------------------
DB_BACKEND = "mysql" if IS_PROD else "sqlite"

# SQLite (dev): a temp file shared between Passenger processes.
SQLITE_PATH = os.environ.get(
    "SQLITE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp", "couplechat.sqlite"),
)

# MySQL (prod): standard DB_* variables as used by cPanel.
MYSQL_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", ""),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", ""),
}

# --------------------------------------------------------------------------
# Privacy / TTLs — everything in the store auto-expires.
# --------------------------------------------------------------------------
PRESENCE_TTL = int(os.environ.get("PRESENCE_TTL", "75"))     # heartbeat every ~25s
NUDGE_TTL = int(os.environ.get("NUDGE_TTL", "90"))           # pending connect request
QUEUE_MAX_PER_USER = int(os.environ.get("QUEUE_MAX", "200"))  # bounded event queue
PUSH_SUB_TTL = int(os.environ.get("PUSH_SUB_TTL", "86400"))  # push subs kept 24h max
SUB_SWEEP_EVERY = int(os.environ.get("SUB_SWEEP_EVERY", "50"))  # GC push subs every N writes

# --------------------------------------------------------------------------
# Rate limiting (sliding window counters kept in the shared store)
# --------------------------------------------------------------------------
RL_NUDGE = (int(os.environ.get("RL_NUDGE_MAX", "5")), 60)      # nudges per minute
RL_MSG = (int(os.environ.get("RL_MSG_MAX", "30")), 60)         # messages per minute
RL_LOGIN = (int(os.environ.get("RL_LOGIN_MAX", "10")), 60)     # login attempts/min/user
RL_LOCKED = (int(os.environ.get("RL_LOCKED_MAX", "10")), 60)   # unlock attempts/min/msg

MAX_MESSAGE_LEN = 4000        # plaintext cap enforced client+server
MAX_EVENT_BYTES = 64 * 1024   # hard cap on a relayed event payload (locks are small)

# --------------------------------------------------------------------------
# Web Push (VAPID)
# --------------------------------------------------------------------------
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL", "mailto:admin@example.com")
PUSH_ENABLED = bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)
