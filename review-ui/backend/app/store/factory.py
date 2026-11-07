from functools import lru_cache
import logging

from fastapi import Request

from app.store.base import FolderRepository
from app.store.postgres.repository import PostgresFolderRepository
from app.core.config import Settings, get_settings

logger = logging.getLogger("review_ui.store")


def request_is_production(request: Request) -> bool:
    """Always True: review-ui reads the database and Azure Blob only (2026-11-07)."""
    return True


@lru_cache(maxsize=1)
def _repository() -> FolderRepository:
    return build_repository(get_settings())


def get_repository(request: Request) -> FolderRepository:
    return _repository()


def build_repository(settings: Settings, production: bool | None = None) -> FolderRepository:
    """DB_BACKEND=cosmos: Cosmos DB; otherwise (postgres, both) Postgres. Never disk: page
    images come from Azure Blob only. ``production`` is accepted for old callers and ignored."""
    if settings.db_backend == "cosmos":
        from app.store.cosmos.repository import CosmosFolderRepository

        logger.info("repository=cosmos database=%s (page images from Azure Blob)", settings.cosmos_database)
        return CosmosFolderRepository(
            endpoint=settings.cosmos_endpoint,
            key=settings.cosmos_key,
            database=settings.cosmos_database,
        )
    logger.info("repository=postgres schema=%s (page images from Azure Blob)", settings.db_schema)
    return PostgresFolderRepository(
        settings.database_url,
        data_root=None,
        metadata_root=None,
        db_schema=settings.db_schema,
        disk_data=False,
    )
