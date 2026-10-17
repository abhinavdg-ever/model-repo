from functools import lru_cache
import logging

from fastapi import Request

from app.adapters.base import FolderRepository
from app.adapters.local.repository import LocalFolderRepository
from app.adapters.postgres.repository import PostgresFolderRepository
from app.core.config import Settings, get_settings

logger = logging.getLogger("review_ui.adapters")


def request_is_production(request: Request) -> bool:
    """DB (Postgres + Azure Blob) or Local: DATA_MODE alone. The old top-bar
    switch's cookie (advantmed_data_mode) is ignored."""
    return get_settings().is_production_mode


@lru_cache(maxsize=2)
def _repository(production: bool) -> FolderRepository:
    return build_repository(get_settings(), production=production)


def get_repository(request: Request) -> FolderRepository:
    return _repository(request_is_production(request))


def build_repository(settings: Settings, production: bool | None = None) -> FolderRepository:
    if settings.is_production_mode if production is None else production:
        logger.info(
            "repository=postgres schema=%s data_root=%s metadata_root=%s",
            settings.db_schema,
            settings.resolved_data_root,
            settings.resolved_metadata_root,
        )
        # Production reads its data from Postgres only: no disk fallback for a chart, its
        # page list or the folder list, and nothing from staging. Page images come from
        # Azure Blob, else the workspace copy (the source document, not pipeline output).
        return PostgresFolderRepository(
            settings.database_url,
            data_root=settings.resolved_data_root,
            metadata_root=None,
            db_schema=settings.db_schema,
            disk_data=False,
        )
    logger.info(
        "repository=local data_root=%s metadata_root=%s database_url=%s",
        settings.resolved_data_root,
        settings.resolved_metadata_root,
        "set" if (settings.database_url or "").strip() else "unset",
    )
    return LocalFolderRepository(
        settings.resolved_data_root,
        metadata_root=settings.resolved_metadata_root,
        database_url=settings.database_url,
        db_schema=settings.db_schema,
    )
