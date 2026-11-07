"""Health and the app configuration the frontend reads."""

import logging
from fastapi import APIRouter, Request
from app.store.factory import request_is_production
from app.core.config import get_settings
from app.core.schemas import (
    AppConfigResponse,
    HealthResponse,
)
from app.services.backend import store_usable
from app.services.chart_run_batch import database_url_usable

logger = logging.getLogger("review_ui.api")
router = APIRouter()


def _mode(request: Request) -> tuple[str, str]:
    """(data_mode, label) from DATA_MODE."""
    production = request_is_production(request)
    return ("production", "Production Mode") if production else ("local", "Local Mode")


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    data_mode, label = _mode(request)
    return HealthResponse(status="ok", data_mode=data_mode, mode_label=label)


@router.get("/config", response_model=AppConfigResponse)
def app_config(request: Request) -> AppConfigResponse:
    """Public UI config. Does not expose secrets."""
    settings = get_settings()
    sas_configured = bool(settings.blob_sas_token.strip())
    data_mode, label = _mode(request)
    return AppConfigResponse(
        data_mode=data_mode,
        mode_label=label,
        db_available=store_usable(settings, database_url_usable),
        default_mode="production",
        file_viewer_blob_enabled=settings.file_viewer_blob_enabled,
        blob_auth_mode=settings.blob_auth_mode,
        blob_account_url=settings.blob_account_url.strip().rstrip("/"),
        blob_container=settings.blob_container.strip().strip("/"),
        blob_path_template=settings.blob_path_template.strip() or "{folder}/pages/{filename}",
        blob_entra_ready=settings.blob_entra_ready,
        blob_auth_required=settings.blob_auth_required,
        blob_sas_configured=sas_configured,
        playground_enabled=bool(settings.playground_targets),
        playground_targets=settings.playground_targets,
        playground_container=settings.playground_container.strip(),
        client_view=settings.client_view,
    )

