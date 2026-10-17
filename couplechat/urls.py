"""Project URL configuration for Couple Chat.

  /            -> Django-rendered SPA page (templates/index.html)
  /api/*       -> DRF REST endpoints (chat.api) + SSE stream (chat.views)
  /sw.js       -> Service worker at ROOT scope (required for Web Push)
  /manifest    -> web app manifest
"""
from django.urls import include, path

urlpatterns = [
    path("", include("chat.urls")),
]
