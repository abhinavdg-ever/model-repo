from functools import lru_cache

from app.adapters.base import FolderRepository
from app.adapters.local.repository import LocalFolderRepository
from app.adapters.postgres.repository import PostgresFolderRepository
from app.core.config import Settings, get_settings


@lru_cache
def get_repository() -> FolderRepository:
    settings = get_settings()
    return build_repository(settings)


def build_repository(settings: Settings) -> FolderRepository:
    if settings.is_production_mode:
        return PostgresFolderRepository(
            settings.database_url,
            data_root=settings.resolved_data_root,
            db_schema=settings.db_schema,
        )
    return LocalFolderRepository(
        settings.resolved_data_root,
        metadata_root=settings.resolved_metadata_root,
    )
