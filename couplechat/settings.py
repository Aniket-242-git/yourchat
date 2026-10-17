"""
Couple Chat — Django project settings.

Works out of the box for local development (SQLite state store) and for
cPanel shared hosting via Phusion Passenger (ENV=prod switches BOTH the
Django session DB and the transient core state store to MySQL).

All secrets/config come from environment variables (set them in cPanel
"Setup Python App"). Never hardcode them.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ---------------------------------------------------------------------------
# Environment / basics
# ---------------------------------------------------------------------------
ENV = _env("ENV", "dev").lower()
IS_PROD = ENV == "prod"

# REQUIRED in production. In dev we generate an ephemeral key automatically.
SECRET_KEY = _env("DJANGO_SECRET_KEY") or _env("SECRET_KEY")
if not SECRET_KEY:
    if IS_PROD:
        raise RuntimeError(
            "DJANGO_SECRET_KEY must be set in the environment when ENV=prod"
        )
    SECRET_KEY = "dev-insecure-" + os.urandom(24).hex()

DEBUG = not IS_PROD and _env("DJANGO_DEBUG") == "1"

# Comma-separated list, e.g. chat.example.com,www.chat.example.com
ALLOWED_HOSTS = [h.strip() for h in _env("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]

# Behind cPanel's LiteSpeed/Apache proxy we need SCRIPT_NAME awareness;
# set FORCE_SCRIPT_NAME only if the app is mounted under a subpath.
_script_name = _env("DJANGO_FORCE_SCRIPT_NAME")
if _script_name:
    FORCE_SCRIPT_NAME = _script_name

WSGI_APPLICATION = "couplechat.wsgi.application"

# ---------------------------------------------------------------------------
# Apps — no Django admin, no user model: this app has NO accounts by design.
# sessions/auth/contenttypes are included ONLY so Django can persist its
# signed session cookie store (MySQL in prod / SQLite in dev).
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.auth",           # needed by contenttypes/session machinery
    "django.contrib.contenttypes",
    "django.contrib.sessions",       # server-side session storage (cookie id only)
    "django.contrib.staticfiles",
    "rest_framework",                # DRF — powers every /api/* endpoint
    "chat",                          # Couple Chat UI (views) + API (serializers/views)
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    # NOTE: CSRF middleware is ENABLED. DRF views use SessionAuthentication,
    # so unsafe requests must carry the X-CSRF-Token header (the client JS
    # reads it from <meta name="csrf-token"> injected by the template).
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "chat.middleware.SecurityHeadersMiddleware",   # CSP / HSTS / misc headers
]

ROOT_URLCONF = "couplechat.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Databases — MySQL when ENV == "prod", SQLite otherwise.
# (The transient core state store follows the same rule — see core/store.py.)
# ---------------------------------------------------------------------------
if IS_PROD:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": _env("DB_NAME"),
            "USER": _env("DB_USER"),
            "PASSWORD": _env("DB_PASSWORD"),
            "HOST": _env("DB_HOST", "localhost"),
            "PORT": _env("DB_PORT", "3306"),
            "CONN_MAX_AGE": 0,          # shared hosts kill idle conns; reconnect each request
            "OPTIONS": {"charset": "utf8mb4", "connect_timeout": 10},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "tmp" / "django.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Sessions live purely in Django's session backend (DB-backed). We never use
# client-side cookies for anything sensitive; the session holds ONLY the
# claimed username.
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
SESSION_COOKIE_NAME = "cc_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = IS_PROD          # HTTPS-only cookie in production
CSRF_COOKIE_HTTPONLY = True              # JS sends the token via the meta tag
CSRF_COOKIE_SECURE = IS_PROD
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in _env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]
SESSION_EXPIRE_AT_BROWSER_CLOSE = True   # privacy: closing the browser ends the session

# ---------------------------------------------------------------------------
# DRF defaults — plain JSON API, session auth (no tokens, no user model).
# Throttling is handled by our own store-based sliding windows (per username),
# because DRF's cache throttles would need a cache config on shared hosting.
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "UNAUTHENTICATED_USER": None,        # we have no User model; keep it anonymous-free
    "EXCEPTION_HANDLER": "chat.api.api_exception_handler",
}

# ---------------------------------------------------------------------------
# Static files. In dev runserver serves them; on cPanel point the app's
# static alias at STATIC_ROOT after `python manage.py collectstatic`.
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# ---------------------------------------------------------------------------
# Privacy-oriented logging: WARNING+ only, console only. No request bodies,
# no usernames, no message content is ever logged.
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler", "level": "WARNING"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "couplechat.push": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Security knobs Django controls natively
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "no-referrer"
X_FRAME_OPTIONS = "DENY"
