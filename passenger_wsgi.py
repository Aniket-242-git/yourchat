"""
Phusion Passenger (cPanel "Setup Python App") WSGI entry point.

cPanel calls passenger_wsgi.py:application — keep this file at the ROOT of
the app directory you select in "Setup Python App".
"""
import os
import sys

# Make sure the app root (this file's directory) is importable, and that the
# virtualenv selected in cPanel is used (Passenger normally does both, but
# being explicit avoids surprises on some cPanel builds).
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

os.environ.setdefault("ENV", "prod")   # cPanel deployment defaults to prod/MySQL

from app import create_app  # noqa: E402

application = create_app()  # Passenger looks for `application`
app = application           # alias for tooling that expects `app`
