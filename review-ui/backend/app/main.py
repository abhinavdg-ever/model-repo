import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.routers import router
from app.core.config import get_settings
from app.core.logging_setup import RequestLoggingMiddleware, configure_logging, logger
from app.services.blob_store import blob_auth_label, check_blob_access, resolved_blob_account_url
from app.services.chart_run_batch import database_url_usable
from app.services.db import close_pools

settings = get_settings()
configure_logging()


class DbBackendSwitch:
    """Admin switch between Postgres and Cosmos for one request (2026-11-14, user). The choice
    comes in the ``X-DB-Backend`` header or the ``db_backend`` cookie (an ``<img>`` cannot send
    a header). A request with a sign-in token takes it only when the token is an admin's; one
    with no token (a page image) only for a read. The store must be configured. Every answer
    says which store it used (``X-DB-Backend``)."""

    READS = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        from http.cookies import SimpleCookie

        from app.services import backend
        from app.services.auth import read_token

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        wanted = (headers.get(backend.SWITCH_HEADER) or "").strip().lower()
        if not wanted and headers.get("cookie"):
            jar = SimpleCookie()
            try:
                jar.load(headers["cookie"])
            except Exception:  # noqa: BLE001 - a malformed cookie header: no switch
                jar = SimpleCookie()
            if backend.SWITCH_COOKIE in jar:
                wanted = jar[backend.SWITCH_COOKIE].value.strip().lower()
        choice = None
        if wanted in backend.SWITCHABLE and wanted in backend.available():
            auth = headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                user = read_token(get_settings(), auth[7:].strip())
                allowed = user is not None and user.role == "admin"
            else:
                allowed = scope.get("method", "GET").upper() in self.READS
            choice = wanted if allowed else None
        token = backend.use(choice)
        used = backend.mode()

        async def tagged(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-db-backend", used.encode("latin-1"))]
            await send(message)

        try:
            await self.inner(scope, receive, tagged)
        finally:
            backend.reset(token)


app = FastAPI(title="Advantmed Imaging UI", version="0.1.0")
app.add_middleware(DbBackendSwitch)
app.add_middleware(RequestLoggingMiddleware)
# /ocr and /imaging return whole-chart JSON that compresses 5-10x. Level 5
# trades a little ratio for much less CPU than the default 9.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api")


def _safe_db(url: str) -> str:
    """host:port/db of DATABASE_URL, no credentials."""
    tail = url.rsplit("@", 1)[-1]
    return tail if "@" in url else "(not set)"


def _check_sources(db_on: bool) -> None:
    """The slow checks, off the startup path. Opening the DB pool here also
    means the first page load does not pay for it."""
    if db_on:
        try:
            from app.services.db import connection

            with connection(settings.database_url, settings.db_schema) as conn:
                charts = conn.execute("SELECT count(*) FROM chart_list").fetchone()[0]
            logger.info("  db check    : reachable, %s chart(s) in chart_list", charts)
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            logger.warning("  db check    : NOT reachable — %s", str(exc).splitlines()[0][:200])
    logger.info("  blob access : %s", check_blob_access(settings))


@app.on_event("startup")
def _startup() -> None:
    # One source since 2026-11-07: the database for results, Azure Blob for page images.
    db_on = database_url_usable(settings.database_url)
    logger.info("review-ui starting — database + Azure Blob")
    logger.info("  results     : Postgres %s (schema %s)", _safe_db(settings.database_url), settings.db_schema)
    if settings.db_backend == "cosmos":
        logger.warning("  DB_BACKEND=cosmos: every route reads and writes Cosmos (%s); not on Cosmos "
                       "yet (501): accuracy report, search by value", settings.cosmos_database)
        db_on = False  # nothing reads Postgres
    elif settings.db_backend == "both":
        logger.warning("  DB_BACKEND=both: reading Postgres; review-ui's writes are copied to Cosmos")
    if not db_on and settings.db_backend != "cosmos":
        logger.warning("  DATABASE_URL is not set: nothing to show")
    logger.info("  page images : Azure Blob")
    logger.info("  account     : %s", resolved_blob_account_url(settings) or "(not set)")
    logger.info("  auth        : %s", blob_auth_label(settings))
    logger.info("  container   : %s", settings.blob_container.strip() or "(per chart: chart_list.blob_container)")
    threading.Thread(target=_check_sources, args=(db_on,), name="source-check", daemon=True).start()


@app.on_event("shutdown")
def _shutdown() -> None:
    close_pools()


def _database_target() -> str:
    """host:port/database (schema) of DATABASE_URL, never the user or password, so two
    instances can be checked against the same database."""
    from urllib.parse import urlparse

    raw = (settings.database_url or "").strip()
    if not raw:
        return "none"
    url = urlparse(raw.replace("postgresql+psycopg://", "postgresql://", 1))
    port = f":{url.port}" if url.port else ""
    return f"{url.hostname}{port}{url.path or ''} (schema {settings.db_schema})"


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "advantmed-imaging-ui",
        "data_mode": settings.data_mode,
        "db_backend": settings.db_backend,
        "mode_label": settings.mode_label,
        "database": _database_target(),
        "docs": "/docs",
    }
