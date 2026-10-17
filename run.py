#!/usr/bin/env python3
"""Local development runner (SQLite backend).

    python run.py

Loads a .env file first if present (SECRET_KEY, VAPID_*, ...).
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # threaded=True mimics Passenger multi-threading; SSE drains work fine.
    app.run(host="127.0.0.1", port=port, debug=os.environ.get("FLASK_DEBUG") == "1",
            threaded=True)
