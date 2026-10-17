"""
WSGI config for Couple Chat (Phusion Passenger on cPanel).

Passenger itself looks for ``application`` in passenger_wsgi.py at the app
root; that file simply imports this module. Keeping the real WSGI object here
follows standard Django layout and lets ``manage.py runserver`` share it too.
"""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "couplechat.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
