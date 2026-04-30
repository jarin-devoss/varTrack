"""
app/api/middlewares/security_headers.py
─────────────────────────────────────────
Standard defensive HTTP security headers on every response.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options":        "DENY",
    "Cache-Control":          "no-store, no-cache, must-revalidate",
    "Expires":                "0",
    "Pragma":                 "no-cache",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard defensive headers to every HTTP response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in _HEADERS.items():
            response.headers[name] = value
        return response
