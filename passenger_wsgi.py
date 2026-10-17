"""
WSGI entry point for Phusion Passenger (cPanel "Setup Python App").

cPanel's Passenger configuration points at this file; it simply exposes the
Django application defined in couplechat/wsgi.py.
"""
import os
import sys

# Make sure the project root is importable even if Passenger starts with a
# different cwd (it normally adds the app root, but shared hosts vary).
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "couplechat.settings")

from couplechat.wsgi import application  # noqa: F401,E402  (Passenger needs `application`)
