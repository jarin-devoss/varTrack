"""
app/api/middlewares/correlation.py
────────────────────────────────────
X-Correlation-ID propagation for distributed tracing.

  • If header absent  → generate a new UUID
  • If header present → use as-is, but truncate to 128 chars to cap memory
    (deliberately truncate rather than reject so legitimate long IDs survive)
  • Echo in response header so callers can correlate across services
"""
from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_HEADER          = "x-correlation-id"
_MAX_CORRELATION = 128   # cap correlation ID length to bound memory usage


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """Propagate or generate X-Correlation-ID for every request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        raw = request.headers.get(_HEADER)
        if raw:
            correlation_id = raw[:_MAX_CORRELATION]
        else:
            correlation_id = str(uuid.uuid4())

        request.state.correlation_id = correlation_id

        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response
