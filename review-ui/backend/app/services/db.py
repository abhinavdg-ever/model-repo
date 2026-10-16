"""Pooled Postgres connections shared by every review-ui DB read.

Opening a fresh connection per query cost a TCP + auth handshake plus a
``SET search_path`` round-trip each time; one chart open made a dozen of them.
One pool per (url, schema) keeps connections warm, and ``search_path`` is set
at connect time so it costs nothing per borrow.

Falls back to a plain connect when ``psycopg_pool`` is not installed.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger("review_ui.db")

CONNECT_TIMEOUT_SEC = 3
POOL_MAX_SIZE = 10
# How long a request waits for a free pooled connection before failing.
POOL_WAIT_SEC = 5.0
# After a failed connect, every DB read fails at once for this long instead of
# each waiting POOL_WAIT_SEC: an unreachable database (firewall, VPN, wrong
# host) would otherwise add seconds to every request, even in Local mode.
DOWN_RETRY_SEC = 30.0


class DatabaseUnavailable(RuntimeError):
    """The database did not answer recently; not retried until DOWN_RETRY_SEC."""


_down_until: dict[tuple[str, str], float] = {}

_pools: dict[tuple[str, str], object] = {}
_pools_lock = threading.Lock()


def psycopg_url(database_url: str) -> str:
    """Accept sqlalchemy-style postgresql+psycopg:// and plain postgresql://."""
    url = (database_url or "").strip()
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgres+psycopg://"):
        return "postgresql://" + url[len("postgres+psycopg://") :]
    return url


def valid_schema(db_schema: str | None) -> str:
    schema = (db_schema or "public").strip() or "public"
    if not schema.replace("_", "").isalnum():
        raise ValueError(f"Invalid DB_SCHEMA: {schema!r}")
    return schema


def _connect_kwargs(schema: str) -> dict[str, object]:
    return {
        "connect_timeout": CONNECT_TIMEOUT_SEC,
        "options": f"-c search_path={schema}",
    }


def _pool_for(url: str, schema: str):
    key = (url, schema)
    pool = _pools.get(key)
    if pool is not None:
        return pool
    from psycopg_pool import ConnectionPool

    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            pool = ConnectionPool(
                url,
                min_size=1,
                max_size=POOL_MAX_SIZE,
                timeout=POOL_WAIT_SEC,
                kwargs=_connect_kwargs(schema),
                # A pooled socket can die under us (laptop sleep, Wi-Fi or VPN
                # change: "Can't assign requested address"). Test each one on
                # borrow and reconnect, rather than fail the request mid-query.
                check=ConnectionPool.check_connection,
                open=True,
                name=f"review-ui:{schema}",
            )
            _pools[key] = pool
    return pool


@contextmanager
def connection(database_url: str, db_schema: str | None = "public") -> Iterator:
    """Borrow a connection with ``search_path`` already set.

    Commits on clean exit, rolls back on error, then returns the connection to
    the pool — same semantics as ``with psycopg.connect(...) as conn``.
    """
    import time

    url = psycopg_url(database_url)
    schema = valid_schema(db_schema)
    key = (url, schema)
    now = time.monotonic()
    if _down_until.get(key, 0.0) > now:
        raise DatabaseUnavailable(
            f"database unreachable; next try in {_down_until[key] - now:.0f}s"
        )
    try:
        pool = _pool_for(url, schema)
    except ImportError:
        pool = None
    try:
        if pool is None:
            import psycopg

            borrowed = psycopg.connect(url, **_connect_kwargs(schema))
        else:
            borrowed = pool.connection()  # type: ignore[attr-defined]
        conn = borrowed.__enter__()
    except Exception as exc:
        # Getting a connection failed (not a query): stop trying for a while.
        if key not in _down_until or _down_until[key] <= now:
            logger.warning(
                "database unreachable (%s); DB reads fail fast for %.0fs",
                str(exc).splitlines()[0][:160], DOWN_RETRY_SEC,
            )
        _down_until[key] = time.monotonic() + DOWN_RETRY_SEC
        # A pool left open keeps reconnecting (and logging) in the background;
        # drop it, and the next try after DOWN_RETRY_SEC builds a fresh one.
        with _pools_lock:
            dead = _pools.pop(key, None)
        if dead is not None:
            # close() waits for its workers; do not hold the request for it.
            threading.Thread(target=dead.close, name="db-pool-close", daemon=True).start()  # type: ignore[attr-defined]
        raise
    _down_until.pop(key, None)
    try:
        yield conn
    except BaseException as exc:
        if not borrowed.__exit__(type(exc), exc, exc.__traceback__):
            raise
    else:
        borrowed.__exit__(None, None, None)


def close_pools() -> None:
    with _pools_lock:
        for pool in _pools.values():
            try:
                pool.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                logger.debug("pool close failed", exc_info=True)
        _pools.clear()
