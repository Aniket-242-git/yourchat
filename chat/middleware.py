"""
Security headers middleware.

Adds CSP + hardening headers to every response. The app is deliberately
same-origin-only: no CDNs, inline styles kept minimal ('unsafe-inline' is
scoped to style-src because the pink theme tweaks a few style attributes;
scripts are NEVER allowed inline — 'self' only, which forces all logic into
/static/js/*.js).
"""


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        csp = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "form-action 'none'"          # the SPA uses fetch(), never form posts
        )
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy",
                                    "geolocation=(), camera=(), microphone=()")
        response.headers.setdefault("X-XSS-Protection", "0")  # CSP is the modern control
        if getattr(request, "secure", False):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=2592000; includeSubDomains")
        # Service worker & manifest must always be revalidated.
        if request.path in ("/sw.js", "/manifest.webmanifest"):
            response.headers["Cache-Control"] = "no-cache"
            response.headers["Service-Worker-Allowed"] = "/"
        return response
