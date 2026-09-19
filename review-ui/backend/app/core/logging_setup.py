"""Request / startup logging for review-ui."""
from __future__ import annotations

import logging
import os
import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("review_ui")

# Skip request lines for these — health probes and OpenAPI spam the console.
_SKIP_PATHS = frozenset({
    "/",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/api/health",
})

NOISY_LOGGERS = ("azure", "urllib3", "msal", "httpx", "httpcore")


def configure_logging(level: int | None = None) -> None:
    """Root logger + review_ui. Call once at process start."""
    if level is None:
        raw = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
        parsed = logging.getLevelName(raw)
        level = parsed if isinstance(parsed, int) else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # basicConfig is a no-op when handlers already exist (uvicorn); set level
    # directly so LOG_LEVEL still takes effect.
    logging.getLogger().setLevel(level)
    logging.getLogger("review_ui").setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """One line per API call: method path status duration."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "%s %s failed (%.0fms)",
                request.method,
                request.url.path,
                ms,
            )
            raise

        ms = (time.perf_counter() - started) * 1000
        logger.info(
            "%s %s -> %s (%.0fms)",
            request.method,
            request.url.path,
            response.status_code,
            ms,
        )
        return response
