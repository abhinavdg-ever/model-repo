"""Which database review-ui's own services use (DB_BACKEND, 2026-11-08).

  postgres  read and write Postgres (unchanged).
  cosmos    read and write Azure Cosmos DB only (app/store/cosmos/review.py).
  both      read Postgres; every review-ui write goes to Postgres, then to Cosmos. A Cosmos
            failure is logged and never fails the request (``mirror``).

Each service asks here once per public function: ``reads_cosmos()`` picks the Cosmos path,
``mirror(...)`` copies a Postgres write to Cosmos when DB_BACKEND=both.
"""

from __future__ import annotations

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


def mode() -> str:
    return get_settings().db_backend


def reads_cosmos() -> bool:
    return mode() == "cosmos"


def mirrors_cosmos() -> bool:
    return mode() == "both"


def store_usable(settings: Settings, url_check: Callable[[str | None], bool] = database_url_usable) -> bool:
    """Whether the configured backend can be asked at all: Cosmos settings for cosmos, a
    real DATABASE_URL (``url_check``) otherwise."""
    if getattr(settings, "db_backend", "postgres") == "cosmos":
        return bool(settings.cosmos_endpoint.strip() and settings.cosmos_key.strip())
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
            from app.store.factory import _repository

            settings = get_settings()
            repo = _repository() if settings.db_backend == "cosmos" else None
            if not isinstance(repo, CosmosFolderRepository):
                repo = CosmosFolderRepository(
                    endpoint=settings.cosmos_endpoint, key=settings.cosmos_key,
                    database=settings.cosmos_database,
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
