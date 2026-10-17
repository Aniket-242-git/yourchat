#!/usr/bin/env bash
# Local development helper: load .env, migrate, run Django's dev server.
set -e
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
mkdir -p tmp
python manage.py migrate -v 0
exec python manage.py runserver "${1:-0.0.0.0:8000}"
