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

File logs (optional, on by default): daily rotation under ``LOG_DIR``
(default ``core-pipeline/logs/``) as ``core-pipeline.log`` + dated backups.
Stdout still works for ``docker compose logs``.

Console lines tag every message as ``[batch-N] [chart_name]`` (worker +
document) so parallel charts are easy to tell apart. Colour, when enabled,
applies to the worker tag only. File logs stay plain (no ANSI).
"""
from __future__ import annotations

import contextvars
import logging
import os
import sys
import threading
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

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
LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(threadName)s] [%(chart)s] %(name)s: %(message)s"
)
LOG_FORMAT_COLOR = (
    "%(asctime)s %(levelname)s [%(worker_colored)s] [%(chart)s] %(name)s: %(message)s"
)
# Keep a month of daily files; older ones are removed on rotate.
LOG_BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_DAYS") or "30")

# Stable palette for worker tags (not hash()-based — process-stable).
_WORKER_COLORS = (
    "\033[36m",   # cyan
    "\033[33m",   # yellow
    "\033[35m",   # magenta
    "\033[32m",   # green
    "\033[34m",   # blue
    "\033[91m",   # bright red
    "\033[96m",   # bright cyan
    "\033[93m",   # bright yellow
    "\033[95m",   # bright magenta
    "\033[92m",   # bright green
)
_RESET = "\033[0m"
_DIM = "\033[2m"

# Chart currently being processed on this thread / asyncio task. Propagates
# into every log line via ``_ChartContextFilter`` so batch/parallel runs are
# attributable without editing every ``logger.info``.
_current_chart: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pipeline_chart", default=""
)


def set_current_chart(chart_name: Optional[str]) -> contextvars.Token:
    """Bind the chart name into ``[batch#] [chart#]`` log tags for this context."""
    return _current_chart.set((chart_name or "").strip())


def reset_current_chart(token: contextvars.Token) -> None:
    _current_chart.reset(token)


def get_current_chart() -> str:
    return _current_chart.get() or ""


def _chart_tag() -> str:
    return _current_chart.get() or "-"


class _ChartContextFilter(logging.Filter):
    """Inject ``record.chart`` so the format string always has a value."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.chart = _chart_tag()
        return True


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


def resolve_log_dir() -> Path:
    """Directory for daily log files. ``LOG_DIR`` overrides the default."""
    raw = (os.environ.get("LOG_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent / "logs"


def _flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().casefold() not in {"0", "false", "no", "off"}


def _color_enabled(stream: object) -> bool:
    """LOG_COLOR=true|false|auto (default auto: tty, or true under Docker)."""
    raw = (os.environ.get("LOG_COLOR") or "auto").strip().casefold()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    # auto
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except Exception:
        return False


def _worker_color(name: str) -> str:
    if not name or name in {"MainThread", "asyncio_0"}:
        return f"{_DIM}{name or '-'}{_RESET}"
    idx = sum(ord(c) for c in name) % len(_WORKER_COLORS)
    return f"{_WORKER_COLORS[idx]}{name}{_RESET}"


class _WorkerColorFormatter(logging.Formatter):
    """Colour only the worker/thread tag; rest of the line stays normal."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "chart"):
            record.chart = _chart_tag()
        record.worker_colored = _worker_color(getattr(record, "threadName", "") or "-")
        return super().format(record)


class _ChartAwareFormatter(logging.Formatter):
    """Plain formatter that never KeyErrors on missing ``chart``."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "chart"):
            record.chart = _chart_tag()
        return super().format(record)


def set_worker_name(label: str) -> str:
    """Rename the current thread for log lines (e.g. ``batch-2``, ``page-0``)."""
    name = (label or "").strip() or "worker"
    threading.current_thread().name = name
    return name


def _attach_daily_file_handler(level: int) -> Path | None:
    """Write ``logs/core-pipeline.log``, rotate at midnight to dated backups."""
    if not _flag("LOG_TO_FILE", True):
        return None

    log_dir = resolve_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    path = log_dir / "core-pipeline.log"
    absolute = str(path.resolve())
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, TimedRotatingFileHandler) and getattr(
            handler, "baseFilename", None
        ) == absolute:
            return path

    handler = TimedRotatingFileHandler(
        absolute,
        when="midnight",
        interval=1,
        backupCount=max(1, LOG_BACKUP_COUNT),
        encoding="utf-8",
        utc=False,
    )
    handler.suffix = "%Y-%m-%d"
    handler.setLevel(level)
    handler.setFormatter(_ChartAwareFormatter(LOG_FORMAT))
    handler.addFilter(_ChartContextFilter())
    root.addHandler(handler)
    return path


def _ensure_chart_filter(handler: logging.Handler) -> None:
    if not any(isinstance(f, _ChartContextFilter) for f in handler.filters):
        handler.addFilter(_ChartContextFilter())


def _configure_stream_handlers(level: int) -> None:
    """Ensure stdout/stderr handlers show worker names (+ colour when enabled)."""
    root = logging.getLogger()
    color = _color_enabled(sys.stderr)
    fmt: logging.Formatter = (
        _WorkerColorFormatter(LOG_FORMAT_COLOR)
        if color
        else _ChartAwareFormatter(LOG_FORMAT)
    )
    for handler in root.handlers:
        if isinstance(handler, TimedRotatingFileHandler):
            # File handler already got the filter at attach time; keep format plain.
            _ensure_chart_filter(handler)
            continue
        if isinstance(handler, logging.StreamHandler):
            handler.setFormatter(fmt)
            handler.setLevel(level)
            _ensure_chart_filter(handler)


def configure_logging(level: int = logging.INFO) -> None:
    """Root logger at `level`, SDK loggers quieted, optional daily file log.

    Call once, at process start, before anything logs.
    """
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
    )
    # basicConfig is a no-op when the root logger already has a handler — which
    # it does under pytest, and under anything that configured logging before
    # we were imported. Without this the requested level is silently ignored
    # and the pipeline's progress lines never appear. Setting the level
    # directly does not disturb whatever handlers are already installed.
    root = logging.getLogger()
    root.setLevel(level)
    # basicConfig's default handler has no chart filter / %(chart)s yet.
    for handler in root.handlers:
        _ensure_chart_filter(handler)
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, TimedRotatingFileHandler
        ):
            if not isinstance(handler.formatter, _WorkerColorFormatter):
                handler.setFormatter(_ChartAwareFormatter(LOG_FORMAT))
    quiet_noisy_loggers()

    log_path = _attach_daily_file_handler(level)
    _configure_stream_handlers(level)
    if log_path is not None:
        logging.getLogger("core-pipeline").info(
            "Daily file log: %s (rotates at midnight, keep %d days)",
            log_path,
            LOG_BACKUP_COUNT,
        )
