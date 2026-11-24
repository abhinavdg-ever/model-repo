"""Confidence calibration for Page Type and Codability (2026-11-24, user: "the higher bucket should
have higher accuracy ... I am okay with fewer records High but I need good accuracy").

A feature's raw confidence (the pipeline's, in imaging_final.confidences) is mapped through an
increasing curve fitted on the ground truth: knots [[raw, measured accuracy], ...], linear between
them, flat beyond the ends. A page then reads as sure as pages like it were right, so a higher band
is never less accurate than a lower one, and High (80%+) is given only where 80%+ were right.

The Confidence Analysis fits the curves (the frontend scores Page Type and Codability; an admin
saves them) and stores them in ``confidence_calibration``; the frontend (confidenceLevel.ts) and the
History twin (chart_confidence.py) apply them on read. The pipeline's stored raw values never
change, so a refit always measures raw scores.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("review_ui.calibration")

FEATURES = ("page_type", "codability")
MIN_PAGES = 30
TTL_SEC = 60.0

_cache: dict[tuple[str, str], tuple[float, dict[str, list[list[float]]]]] = {}
_lock = threading.Lock()


def valid_knots(knots: Any) -> Optional[list[list[float]]]:
    """[[raw, accuracy], ...] with both in [0, 1], raw increasing and accuracy never falling, 2+
    knots; None when not."""
    try:
        pts = [[float(a), float(b)] for a, b in knots]
    except (TypeError, ValueError):
        return None
    if len(pts) < 2 or any(not (0 <= a <= 1 and 0 <= b <= 1) for a, b in pts):
        return None
    if any(b[0] < a[0] or b[1] < a[1] for a, b in zip(pts, pts[1:])):
        return None
    return [[round(a, 4), round(b, 4)] for a, b in pts]


def apply(knots: Optional[list[list[float]]], value: Optional[float]) -> Optional[float]:
    """``value`` through the curve (4 decimals); unchanged with no curve."""
    if value is None or not knots or len(knots) < 2:
        return value
    pts = sorted((float(a), float(b)) for a, b in knots)
    if value <= pts[0][0]:
        return round(pts[0][1], 4)
    if value >= pts[-1][0]:
        return round(pts[-1][1], 4)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= value <= x1:
            return round(y0 if x1 <= x0 else y0 + (y1 - y0) * (value - x0) / (x1 - x0), 4)
    return value


def _rows_to_table(rows: list[tuple]) -> dict[str, list[list[float]]]:
    out: dict[str, list[list[float]]] = {}
    for feature, knots in rows:
        if isinstance(knots, str):
            try:
                knots = json.loads(knots)
            except ValueError:
                continue
        good = valid_knots(knots)
        if feature in FEATURES and good:
            out[str(feature)] = good
    return out


def load(database_url: str, db_schema: str, fresh: bool = False) -> dict[str, list[list[float]]]:
    """feature -> knots, reused for a minute. {} when the table is missing or unreadable."""
    key = (database_url, db_schema)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and not fresh and now - hit[0] < TTL_SEC:
            return hit[1]
    try:
        from app.services.db import connection

        with connection(database_url, db_schema) as conn, conn.cursor() as cur:
            cur.execute("SELECT feature, knots FROM confidence_calibration")
            table = _rows_to_table(cur.fetchall())
    except Exception as exc:  # noqa: BLE001 - no calibration is the raw values, never an error
        logger.info("confidence calibration not read: %s", exc)
        table = {}
    with _lock:
        _cache[key] = (now, table)
    return table


def save(database_url: str, db_schema: str, features: dict[str, Any], user_id: Optional[int]) -> dict[str, Any]:
    """Store each feature's curve ({"knots", "pages"}); an empty ``knots`` removes that feature's
    curve (raw values again). Returns the table now in force."""
    from app.services.db import connection

    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        for feature, entry in features.items():
            if feature not in FEATURES:
                raise ValueError(f"unknown feature {feature!r}; calibrated: {', '.join(FEATURES)}")
            knots = (entry or {}).get("knots")
            if not knots:
                cur.execute("DELETE FROM confidence_calibration WHERE feature = %s", (feature,))
                continue
            good = valid_knots(knots)
            pages = int((entry or {}).get("pages") or 0)
            if good is None:
                raise ValueError(f"{feature}: knots must rise, each [raw, accuracy] in 0-1, at least two")
            if pages < MIN_PAGES:
                raise ValueError(f"{feature}: fitted on {pages} pages; at least {MIN_PAGES} are needed")
            cur.execute(
                """INSERT INTO confidence_calibration (feature, knots, pages, user_id)
                   VALUES (%s, %s::jsonb, %s, %s)
                   ON CONFLICT (feature) DO UPDATE SET knots = EXCLUDED.knots, pages = EXCLUDED.pages,
                       user_id = EXCLUDED.user_id, updated_at = now()""",
                (feature, json.dumps(good), pages, user_id),
            )
    return load(database_url, db_schema, fresh=True)
