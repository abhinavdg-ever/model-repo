"""Retry transient Azure / OpenAI failures with exponential backoff.

The Azure and OpenAI SDKs already retry some transport errors. This wrapper
covers the gaps that still fail charts in practice: 429 rate limits with
Retry-After, brief 5xx windows, and dropped connections that the SDK gave up
on. Permanent failures (401, 403, 404) are not retried.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Status codes that are worth waiting out. 408/429/5xx only — auth and
# not-found errors would burn the same call forever.
_RETRY_HTTP = frozenset({408, 429, 500, 502, 503, 504})


def _status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    headers = None
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    if not headers:
        return None
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def is_transient_azure_error(exc: BaseException) -> bool:
    """True when another attempt is more likely to succeed than fail."""
    if isinstance(exc, (TimeoutError, ConnectionError, ConnectionResetError, BrokenPipeError)):
        return True

    name = type(exc).__name__
    module = type(exc).__module__ or ""

    # azure-core
    if name in {
        "ServiceRequestError",
        "ServiceResponseError",
        "HttpResponseError",
    } or module.startswith("azure."):
        code = _status_code(exc)
        if code is None:
            # Transport / incomplete response — no status, still worth retrying.
            if name in {"ServiceRequestError", "ServiceResponseError"}:
                return True
            return "timeout" in str(exc).lower() or "timed out" in str(exc).lower()
        return code in _RETRY_HTTP

    # openai
    if name in {
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "APIStatusError",
    } or module.startswith("openai"):
        code = _status_code(exc)
        if code is None:
            return name != "AuthenticationError"
        return code in _RETRY_HTTP

    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
            "timed out",
            "timeout",
            "too many requests",
            "429",
            "503",
            "502",
            "504",
        )
    )


def call_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    label: str = "azure",
) -> T:
    """Call ``fn`` up to ``attempts`` times on transient failures.

    Delay is exponential with jitter, capped at ``max_delay``. A ``Retry-After``
    header, when present, wins over the computed delay (still capped).
    """
    attempts = max(1, int(attempts))
    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if attempt >= attempts or not is_transient_azure_error(exc):
                raise
            advised = _retry_after_seconds(exc)
            delay = advised if advised is not None else min(
                max_delay, base_delay * (2 ** (attempt - 1))
            )
            delay = min(max_delay, delay) + random.uniform(0.0, 0.25 * delay)
            logger.warning(
                "%s attempt %s/%s failed (%s: %s); retrying in %.1fs",
                label,
                attempt,
                attempts,
                type(exc).__name__,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last is not None
    raise last


def azure_sdk_retry_kwargs(
    *,
    total: int = 5,
    backoff_factor: float = 0.8,
) -> dict[str, Any]:
    """Keyword args for Azure SDK clients that accept ``retry_total`` etc.

    BlobServiceClient and DocumentIntelligenceClient honour these via
    ``kwargs`` into the pipeline policy.
    """
    return {
        "retry_total": total,
        "retry_connect": total,
        "retry_read": total,
        "retry_status": total,
        "retry_backoff_factor": backoff_factor,
        "retry_on_status_codes": list(_RETRY_HTTP),
    }
