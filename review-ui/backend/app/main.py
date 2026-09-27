from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.routes import router
from app.core.config import get_settings
from app.core.logging_setup import RequestLoggingMiddleware, configure_logging, logger
from app.services.db import close_pools

settings = get_settings()
configure_logging()

app = FastAPI(title="Advantmed Imaging UI", version="0.1.0")
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


@app.on_event("startup")
def _startup() -> None:
    logger.info(
        "review-ui starting — data_mode=%s data_root=%s",
        settings.data_mode,
        settings.resolved_data_root,
    )


@app.on_event("shutdown")
def _shutdown() -> None:
    close_pools()


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "advantmed-imaging-ui",
        "data_mode": settings.data_mode,
        "mode_label": settings.mode_label,
        "docs": "/docs",
    }
