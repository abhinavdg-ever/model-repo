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
# Pool size and wait: DB_POOL_MIN / DB_POOL_MAX / DB_POOL_WAIT_SEC in
# review-ui/.env (Settings; 1, 20 and 10 s by default). While core-pipeline writes, reads slow
# down and a busy page holds several connections at once; 10 ran out.
# After a failed connect, every DB read fails at once for this long instead of
# each waiting the pool timeout: an unreachable database (firewall, VPN, wrong
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
            from app.core.config import get_settings

            settings = get_settings()
            pool = ConnectionPool(
                url,
                min_size=max(0, min(settings.db_pool_min, settings.db_pool_max)),
                max_size=max(1, settings.db_pool_max),
                timeout=max(1.0, settings.db_pool_wait_sec),
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
        # A busy pool (every connection lent out) is not a dead database: only
        # stop trying when the pool could not keep a single connection open.
        stats = pool.get_stats() if pool is not None else {}  # type: ignore[attr-defined]
        if stats.get("pool_size", 0) > 0:
            logger.warning(
                "database pool busy (%s of %s connections in use, %s waiting): %s",
                stats.get("pool_size", 0) - stats.get("pool_available", 0),
                stats.get("pool_max"), stats.get("requests_waiting"),
                str(exc).splitlines()[0][:160],
            )
            raise
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


# How long a "does this table exist" answer is trusted (a migration shows up after this).
TABLE_EXISTS_TTL_SEC = 600.0
_table_exists_cache: dict[tuple[str, str, str], tuple[float, bool]] = {}


def table_exists(database_url: str, db_schema: str | None, table: str) -> bool:
    """Whether ``table`` exists in the schema, cached for TABLE_EXISTS_TTL_SEC so a chart
    open does not ask the database each time."""
    import time

    key = (psycopg_url(database_url), valid_schema(db_schema), table)
    now = time.monotonic()
    hit = _table_exists_cache.get(key)
    if hit and now - hit[0] < TABLE_EXISTS_TTL_SEC:
        return hit[1]
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
        exists = bool((cur.fetchone() or [False])[0])
    _table_exists_cache[key] = (now, exists)
    return exists


def close_pools() -> None:
    with _pools_lock:
        for pool in _pools.values():
            try:
                pool.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                logger.debug("pool close failed", exc_info=True)
        _pools.clear()
