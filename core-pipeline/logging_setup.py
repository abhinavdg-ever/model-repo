"""Logging configuration, in one place.

Exists because of one specific problem. The pipeline's own logs are per-page
progress lines — `[Final OCR 2] Page 37 of 400 completed` — and they are how a
long run is watched. The Azure SDKs log **every request and every response
header** through `azure.core.pipeline.policies.http_logging_policy`, at INFO.
Setting the root logger to INFO to see our lines therefore also turns those on,
and a 400-page chart buries its own progress under a few thousand lines of

    Request headers:
        'x-ms-client-request-id': '...'
        'User-Agent': 'azsdk-python-ai-documentintelligence/1.0.2 ...'
    Response headers:
        'Content-Length': '66061'
        ...

The SDK is not misbehaving — that output is genuinely useful when debugging a
403 or a throttle. It is just three orders of magnitude noisier than the thing
it is interleaved with. So it is off by default and one variable away.
"""
from __future__ import annotations

import logging
import os

# Setting the parent `azure` logger covers azure.core, azure.identity,
# azure.storage.blob and azure.ai.documentintelligence in one line — child
# loggers inherit the level they do not set themselves.
NOISY_LOGGERS = ("azure", "urllib3", "msal")

# Always quieted, regardless of AZURE_LOG_LEVEL: this logger emits a ~20-line
# WARNING naming all nine credential sources, and then raises a
# ClientAuthenticationError whose message is that same text. Our callers log
# the exception, so the warning is a verbatim duplicate — and the SDK's retry
# policy emits it once per attempt, so one failed blob call produced four
# copies of eighty lines. Errors from it still pass.
ALWAYS_QUIET = ("azure.identity._credentials.chained",)

DEFAULT_AZURE_LOG_LEVEL = "WARNING"


def azure_log_level() -> int:
    """`AZURE_LOG_LEVEL`, defaulting to WARNING. An unknown value is WARNING.

    Set it to INFO or DEBUG to get the request/response dump back when
    diagnosing an auth failure or a throttle.
    """
    raw = (os.environ.get("AZURE_LOG_LEVEL") or DEFAULT_AZURE_LOG_LEVEL).strip().upper()
    level = logging.getLevelName(raw)
    # getLevelName returns the string "Level <x>" for anything it does not know.
    return level if isinstance(level, int) else logging.WARNING


def quiet_noisy_loggers() -> int:
    """Raise the SDK loggers' level. Returns the level applied, for logging it."""
    level = azure_log_level()
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(level)
    for name in ALWAYS_QUIET:
        # Quiet by default, but honour a deliberate request for MORE detail.
        # Levels are numeric and ascend with severity (DEBUG 10 < ERROR 40), so
        # "more verbose than the default" is a LOWER number — max() would have
        # pinned this at ERROR forever and silently ignored AZURE_LOG_LEVEL=DEBUG.
        logging.getLogger(name).setLevel(
            level if level < logging.WARNING else logging.ERROR
        )
    return level


def configure_logging(level: int = logging.INFO) -> None:
    """Root logger at `level`, SDK loggers quieted.

    Call once, at process start, before anything logs.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # basicConfig is a no-op when the root logger already has a handler — which
    # it does under pytest, and under anything that configured logging before
    # we were imported. Without this the requested level is silently ignored
    # and the pipeline's progress lines never appear. Setting the level
    # directly does not disturb whatever handlers are already installed.
    logging.getLogger().setLevel(level)
    quiet_noisy_loggers()
