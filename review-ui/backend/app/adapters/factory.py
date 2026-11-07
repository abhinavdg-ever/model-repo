from functools import lru_cache
import logging

from fastapi import Request

from app.adapters.base import FolderRepository
from app.adapters.postgres.repository import PostgresFolderRepository
from app.core.config import Settings, get_settings

logger = logging.getLogger("review_ui.adapters")


def request_is_production(request: Request) -> bool:
    """Always True: review-ui reads the database and Azure Blob only (2026-11-07)."""
    return True


@lru_cache(maxsize=1)
def _repository() -> FolderRepository:
    return build_repository(get_settings())


def get_repository(request: Request) -> FolderRepository:
    return _repository()


def build_repository(settings: Settings, production: bool | None = None) -> FolderRepository:
    """Postgres, never disk: no fallback for a chart, its page list or the folder list, and
    nothing from a chart folder. Page images come from Azure Blob only. ``production`` is
    accepted for old callers and ignored."""
    logger.info("repository=postgres schema=%s (page images from Azure Blob)", settings.db_schema)
    return PostgresFolderRepository(
        settings.database_url,
        data_root=None,
        metadata_root=None,
        db_schema=settings.db_schema,
        disk_data=False,
    )
