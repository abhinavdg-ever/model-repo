"""All API routes, one module per area; main.py mounts ``router`` under /api."""
from fastapi import APIRouter

from app.routers import system, users, charts, accuracy, excel_compare, search, annotations, review_log, playground

router = APIRouter()
for _module in (system, users, charts, accuracy, excel_compare, search, annotations, review_log, playground,):
    router.include_router(_module.router)
