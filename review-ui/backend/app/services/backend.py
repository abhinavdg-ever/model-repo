"""Which database review-ui's own services use (DB_BACKEND, 2026-11-08).

  postgres  read and write Postgres (unchanged).
  cosmos    read and write Azure Cosmos DB only (app/store/cosmos/review.py).
  both      read Postgres; every review-ui write goes to Postgres, then to Cosmos. A Cosmos
            failure is logged and never fails the request (``mirror``).

Each service asks here once per public function: ``reads_cosmos()`` picks the Cosmos path,
``mirror(...)`` copies a Postgres write to Cosmos when DB_BACKEND=both.

Admin switch (2026-11-14, user: "a cosmos and postgres mode for admin to test"): one request
may ask for the other store with the header ``X-DB-Backend`` or the cookie ``db_backend``
(``postgres`` | ``cosmos``); ``app.main.DbBackendSwitch`` checks it (an admin's token, or a
read with no token, e.g. a page image) and sets ``use()`` for that request only. Everyone
else gets DB_BACKEND. Both stores must be configured.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import Any, Callable, Optional

from app.core.config import Settings, get_settings
from app.services.chart_run_batch import database_url_usable

logger = logging.getLogger("review_ui.backend")


class NotOnCosmos(RuntimeError):
    """A feature that has no Cosmos version yet; routes answer 501 with this message."""

    status = 501

    def __init__(self, what: str) -> None:
        super().__init__(f"{what} is not available on Cosmos yet (DB_BACKEND=cosmos)")
        self.detail = str(self)


SWITCH_HEADER = "x-db-backend"
SWITCH_COOKIE = "db_backend"
SWITCHABLE = ("postgres", "cosmos")
_OVERRIDE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("db_backend_override", default=None)


def configured_mode() -> str:
    """DB_BACKEND from the settings, whatever this request asked for."""
    return get_settings().db_backend


def mode() -> str:
    """This request's store: the admin switch when set, else DB_BACKEND. Asking for postgres
    on a DB_BACKEND=both deployment keeps both (it reads Postgres and still mirrors writes)."""
    chosen = _OVERRIDE.get()
    configured = configured_mode()
    if chosen == "cosmos":
        return "cosmos"
    if chosen == "postgres":
        return "both" if configured == "both" else "postgres"
    return configured


def use(choice: Optional[str]) -> contextvars.Token:
    """Set this request's store (``postgres`` / ``cosmos`` / None = DB_BACKEND)."""
    return _OVERRIDE.set(choice if choice in SWITCHABLE else None)


def reset(token: contextvars.Token) -> None:
    _OVERRIDE.reset(token)


def cosmos_configured(settings: Settings) -> bool:
    from app.store.cosmos.credential import configured

    return configured(
        settings.cosmos_endpoint, settings.cosmos_key, getattr(settings, "cosmos_auth", "key"),
        getattr(settings, "cosmos_tenant_id", "") or getattr(settings, "azure_tenant_id", ""),
        getattr(settings, "cosmos_client_id", "") or getattr(settings, "azure_client_id", ""),
        getattr(settings, "cosmos_client_secret", "") or getattr(settings, "azure_client_secret", ""),
    )


def available(settings: Optional[Settings] = None) -> list[str]:
    """The stores this deployment can switch between: postgres (a usable DATABASE_URL),
    cosmos (Cosmos settings)."""
    settings = settings or get_settings()
    out = []
    if database_url_usable(settings.database_url):
        out.append("postgres")
    if cosmos_configured(settings):
        out.append("cosmos")
    return out


def reads_cosmos() -> bool:
    return mode() == "cosmos"


def mirrors_cosmos() -> bool:
    return mode() == "both"


def store_usable(settings: Settings, url_check: Callable[[str | None], bool] = database_url_usable) -> bool:
    """Whether the configured backend can be asked at all: Cosmos settings for cosmos, a
    real DATABASE_URL (``url_check``) otherwise."""
    chosen = mode() if settings is get_settings() else getattr(settings, "db_backend", "postgres")
    if chosen == "cosmos":
        return cosmos_configured(settings)
    return url_check(settings.database_url)


_store: Optional[Any] = None
_store_lock = threading.Lock()


def cosmos():
    """The Cosmos review store, sharing the chart views' repository (and its page cache)
    when that repository is Cosmos."""
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is None:
            from app.store.cosmos.repository import CosmosFolderRepository
            from app.store.factory import repository_for

            settings = get_settings()
            # The chart views' Cosmos repository (and its page cache), whichever store this
            # request uses: the admin switch can ask for Cosmos on a Postgres deployment.
            repo = repository_for("cosmos") if cosmos_configured(settings) else None
            if not isinstance(repo, CosmosFolderRepository):
                repo = CosmosFolderRepository(
                    endpoint=settings.cosmos_endpoint, key=settings.cosmos_key,
                    database=settings.cosmos_database, auth=settings.cosmos_auth,
                    client_id=settings.cosmos_client_id or settings.azure_client_id,
                    tenant_id=settings.cosmos_tenant_id or settings.azure_tenant_id,
                    client_secret=settings.cosmos_client_secret or settings.azure_client_secret,
                )
            _store = repo.review
    return _store


def set_cosmos(store: Optional[Any]) -> None:
    """Tests: use ``store`` (None: build from the settings again)."""
    global _store
    _store = store


def mirror(what: str, write: Callable[[Any], Any]) -> None:
    """DB_BACKEND=both: ``write(cosmos store)`` after the Postgres write; a failure is
    logged, never raised."""
    if not mirrors_cosmos():
        return
    try:
        write(cosmos())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cosmos copy of %s failed (Postgres kept it): %s", what, exc)
