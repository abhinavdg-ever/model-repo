"""Admin search: OCR text and values across charts."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from app.core.config import Settings, get_settings
from pydantic import BaseModel
from app.services import ocr_search, value_search
from app.services.auth import SignedInUser
from app.routers.deps import _admin_with_db, current_user

logger = logging.getLogger("review_ui.api")
router = APIRouter()


@router.get("/search/ocr")
def search_ocr(
    q: str = Query(..., description="A word or phrase"),
    whole_word: bool = Query(True, description="Match whole words only (pain, not painful)"),
    run: str | None = Query(None, description="Only this run (chart_list.run_id)"),
    batch: str | None = Query(None, description="Only this batch (chart_list.batch_id)"),
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> dict:
    """Admins only: pages whose Tesseract text has the word, grouped by batch and chart."""
    admin = _admin_with_db(user, settings, "search the OCR", "The OCR text is kept")
    try:
        found = ocr_search.search_tesseract(
            settings.database_url, settings.db_schema, q, whole_word=whole_word, run_id=run, batch_id=batch
        )
    except ocr_search.SearchError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except Exception as exc:
        logger.warning("ocr search failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not search the OCR text") from exc
    logger.info("ocr search %r by %s: %d page(s) in %d chart(s)", found["query"], admin.username,
                found["total_pages"], found["total_charts"])
    return found


class ValueCondition(BaseModel):
    column: str
    side: str = "prediction"
    op: str = "is"
    value: str = ""


class ValueSearchRequest(BaseModel):
    conditions: list[ValueCondition]
    run: str | None = None
    batch: str | None = None


@router.get("/search/values/columns")
def value_search_columns(user: SignedInUser | None = Depends(current_user),
                         settings: Settings = Depends(get_settings)) -> dict:
    """The columns a value search can read, and its sides and operators."""
    _admin_with_db(user, settings, "search by value", "The values are kept")
    return {"columns": value_search.columns(), "sides": list(value_search.SIDES),
            "operators": list(value_search.OPERATORS)}


@router.get("/search/values/options")
def value_search_options(
    column: str = Query(...),
    side: str = Query("prediction"),
    run: str | None = Query(None),
    batch: str | None = Query(None),
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> dict:
    """The column's values on one side, most pages first (the value picker)."""
    _admin_with_db(user, settings, "search by value", "The values are kept")
    try:
        return {"options": value_search.options(settings.database_url, settings.db_schema, column, side,
                                                run_id=run, batch_id=batch)}
    except ocr_search.SearchError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except Exception as exc:
        logger.warning("value options failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not read the values") from exc


@router.post("/search/values")
def search_values(
    body: ValueSearchRequest,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> dict:
    """Admins only: pages whose Prediction or Ground Truth has the columns at the values (AND)."""
    admin = _admin_with_db(user, settings, "search by value", "The values are kept")
    try:
        found = value_search.search_values(
            settings.database_url, settings.db_schema, [c.model_dump() for c in body.conditions],
            run_id=body.run, batch_id=body.batch,
        )
    except ocr_search.SearchError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except Exception as exc:
        logger.warning("value search failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not search the values") from exc
    logger.info("value search by %s: %d page(s) in %d chart(s)", admin.username,
                found["total_pages"], found["total_charts"])
    return found

