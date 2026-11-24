"""The accuracy report."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from app.core.config import Settings, get_settings
from app.core.schemas import AccuracyReportResponse
from app.services.backend import NotOnCosmos, store_usable
from app.services.chart_run_batch import database_url_usable
from app.services.accuracy_cache import load_report
from app.services.ground_truth import GroundTruthSource
from app.routers.deps import _admin_with_db, current_user
from pydantic import BaseModel, Field

logger = logging.getLogger("review_ui.api")
router = APIRouter()


@router.get("/accuracy", response_model=AccuracyReportResponse)
def get_accuracy(
    ground_truth: GroundTruthSource = Query(
        default="latest",
        description="latest = newest value from any source; bulk_upload = each page's latest spreadsheet load, manual edits hidden.",
    ),
    refresh: bool = Query(default=False, description="Rebuild instead of reusing the last minute's report."),
    settings: Settings = Depends(get_settings),
) -> AccuracyReportResponse:
    """Saved scores. A chart whose results changed comes back unscored, with a slim document.
    Reused for up to a minute (see accuracy_cache.REPORT_TTL_SEC) unless ``refresh``."""
    if not store_usable(settings, database_url_usable):
        raise HTTPException(status_code=503, detail="Database is not configured")
    try:
        report = load_report(settings, ground_truth, fresh=refresh)
        return report.model_copy(update={"calibration": _calibration(settings, refresh)})
    except NotOnCosmos as exc:
        raise HTTPException(status_code=501, detail=exc.detail) from exc
    except Exception as exc:
        logger.warning("accuracy report failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not load accuracy") from exc




def _calibration(settings: Settings, fresh: bool = False) -> dict:
    """The Page Type / Codability confidence curves; {} without Postgres (raw values)."""
    from app.services import confidence_calibration

    if not database_url_usable(settings.database_url):
        return {}
    return confidence_calibration.load(settings.database_url, settings.db_schema, fresh=fresh)


@router.get("/accuracy/calibration")
def get_calibration(settings: Settings = Depends(get_settings)) -> dict:
    """feature -> knots [[raw, accuracy], ...] (2026-11-24, user: higher bands more accurate)."""
    return {"features": _calibration(settings)}


class CalibrationEntry(BaseModel):
    knots: list[list[float]] = Field(default_factory=list)
    pages: int = 0


class CalibrationRequest(BaseModel):
    features: dict[str, CalibrationEntry]


@router.put("/accuracy/calibration")
def put_calibration(body: CalibrationRequest, user=Depends(current_user),
                    settings: Settings = Depends(get_settings)) -> dict:
    """An admin stores the curves the Confidence Analysis fitted; empty knots clear a feature."""
    from app.services import confidence_calibration
    from app.services.accuracy_cache import clear_report_cache

    admin = _admin_with_db(user, settings, "calibrate confidence", "Calibration is kept")
    if not database_url_usable(settings.database_url):
        raise HTTPException(status_code=501, detail="Calibration is kept in Postgres")
    try:
        table = confidence_calibration.save(settings.database_url, settings.db_schema,
                                            {k: v.model_dump() for k, v in body.features.items()}, admin.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    clear_report_cache()
    logger.info("confidence calibration saved by %s: %s", admin.username, sorted(body.features))
    return {"features": table}


# --- the Accuracy View's download as an .xlsx (2026-11-19, user: page level and chart level,
# one tab each). The browser sends the tables it built; this only lays them out.

from typing import Any as _Any  # noqa: E402

from fastapi import Response  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

XLSX_MAX_ROWS = 200_000


class XlsxSheet(BaseModel):
    name: str = Field(max_length=31)
    header: list[str]
    rows: list[list[_Any]]


class XlsxRequest(BaseModel):
    sheets: list[XlsxSheet] = Field(min_length=1, max_length=10)


def build_xlsx(sheets: list[XlsxSheet]) -> bytes:
    """The sheets as one workbook: a bold, frozen header row, filters, column widths from the
    content (at most 60). A cell like "85%" is written as the number 0.85 shown as a percent."""
    import io
    import re

    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    pct = re.compile(r"^-?\d+(?:\.\d+)?%$")
    book = Workbook()
    book.remove(book.active)
    for sheet in sheets:
        ws = book.create_sheet(re.sub(r"[\[\]:*?/\\]", " ", sheet.name)[:31] or "Sheet")
        ws.append(sheet.header)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in sheet.rows[:XLSX_MAX_ROWS]:
            ws.append(row)
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, str) and pct.match(cell.value):
                    cell.value = float(cell.value[:-1]) / 100
                    cell.number_format = "0%"
        ws.freeze_panes = "A2"
        if sheet.header:
            ws.auto_filter.ref = ws.dimensions
        for i, title in enumerate(sheet.header, start=1):
            longest = max([len(str(title))] + [len(str(r[i - 1])) for r in sheet.rows[:500] if len(r) >= i])
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(8, longest + 2))
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


@router.post("/accuracy/xlsx")
def accuracy_xlsx(body: XlsxRequest) -> Response:
    """The Accuracy View's tables as an .xlsx, one sheet each (Pages, Charts)."""
    return Response(
        content=build_xlsx(body.sheets),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="accuracy.xlsx"'},
    )
