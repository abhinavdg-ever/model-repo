"""Chart confidence for the History list (2026-11-19, user).

The same rule as the frontend's ``confidenceLevel.ts`` (keep the two in step):

* Features: the accuracy list's seven -- Member Verification (the average of Member Name and
  Member DOB), DOS, Blank / Junk, Page Type, Codability, Provider Signature, Orientation -- each
  from ``imaging_final.confidences``. A Blank / Junk or duplicate page counts Blank / Junk and
  Codability only. High >= 0.80, Medium 0.50-0.80, Low < 0.50.
* Page (2026-11-22, user: like an NPS, as the chart): score = (High features - Low features) /
  features, -1..+1; its value = (1 + score) / 2, at least the 20% floor, banded like any confidence.
* Chart (2026-11-23, user): score = the pages' levels averaged with weights High 1.0, Medium 0.6,
  Low 0, a critical page (CRITICAL_PAGE_TYPES by its Final page type) counting twice; the level is
  that score banded (High 0.80+, Medium 0.50-0.80, Low under 0.50; no guardrails since 2026-11-23).
* A page's % is capped by its level: Medium at most 79%, Low at most 49% (LEVEL_CAP).
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

HIGH_AT = 0.8
LOW_BELOW = 0.5
FLOOR = 0.2
# The % never contradicts the level (2026-11-19, user): Medium at most 79%, Low at most 49%.
LEVEL_CAP = {"high": 1.0, "medium": 0.79, "low": 0.49}

# 2026-11-23 (user): a Low page of these Final page types (their family) makes the chart Low, and
# they count twice in its score.
CRITICAL_PAGE_TYPES = ("Discharge Summary", "Operative Note", "Procedure Note")
CHART_WEIGHTS = {"high": 1.0, "medium": 0.6, "low": 0.0}
CRITICAL_WEIGHT = 2.0

# 2026-11-23 (user): a member field with no confidence (not found) reads this.
MEMBER_NOT_FOUND = 0.8
FEATURES = ["member", "dos", "blank_junk", "page_type", "codability", "provider", "orientation"]
BLANK_JUNK_FEATURES = {"blank_junk", "codability"}


def band(value: float) -> str:
    return "high" if value >= HIGH_AT else "medium" if value >= LOW_BELOW else "low"


# 2026-11-23 (user): per-feature cuts (High at, Low below): Blank / Junk, Provider Signature and
# Orientation High from 95%, Low under 75%; DOS High from 75%. Others: 80% / 50%.
FEATURE_CUTS = {"blank_junk": (0.95, 0.75), "provider": (0.95, 0.75), "orientation": (0.95, 0.75),
                "dos": (0.75, 0.50)}


def band_for(key: str, value: float) -> str:
    """A feature's level by its own cuts (FEATURE_CUTS), else the usual 80% / 50%."""
    high_at, low_below = FEATURE_CUTS.get(key, (HIGH_AT, LOW_BELOW))
    return "high" if value >= high_at else "medium" if value >= low_below else "low"


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _confidences(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def is_critical(page_type: Any) -> bool:
    family = str(page_type or "").split(" (")[0].strip().casefold()
    return family in {t.casefold() for t in CRITICAL_PAGE_TYPES}


def page_level(confidences: Any, blank_junk_or_duplicate: bool,
               measured: Any = None, page_type: Any = None) -> Optional[tuple[str, float, bool]]:
    """(level, mean) of one page, or None when it has no feature with a confidence. ``measured``:
    stage 1's measured confidences, for Orientation when the Final ones lack it (as the
    frontend's fieldValue)."""
    conf = dict(_confidences(confidences))
    orientation = _num(_confidences(measured).get("orientation"))
    if conf.get("orientation") is None and orientation is not None:
        conf["orientation"] = orientation
    values: dict[str, float] = {}
    for key in FEATURES:
        if blank_junk_or_duplicate and key not in BLANK_JUNK_FEATURES:
            continue
        if key == "member":
            # 2026-11-23 (user): the average of Name and DOB; one with no confidence (not found) reads
            # MEMBER_NOT_FOUND (0.80).
            parts = [MEMBER_NOT_FOUND if v is None else v
                     for v in (_num(conf.get("member_name")), _num(conf.get("member_dob")))]
            # Only on a page the Final stage scored (any confidence of its own).
            value = sum(parts) / len(parts) if any(_num(v) is not None for v in conf.values()) else None
        else:
            value = _num(conf.get(key))
        if value is not None:
            # 2026-11-24: 4 decimals before banding, as the frontend (round4): 0.79999... is 0.80.
            values[key] = round(value, 4)
    if not values:
        return None
    counts = {"high": 0, "medium": 0, "low": 0}
    for key, v in values.items():
        counts[band_for(key, v)] += 1
    # 2026-11-22 (user): like an NPS over the page's features, as the chart over its pages.
    scored = nps_level(counts)
    return scored["level"], scored["value"], is_critical(page_type)


def nps_level(counts: dict[str, int]) -> dict[str, Any]:
    """{level, value, nps} from High / Medium / Low counts: (High - Low) / n, value (1 + nps) / 2
    with the floor, banded. Pages over their features and charts over their pages alike."""
    n = sum(counts.values())
    nps = (counts["high"] - counts["low"]) / n
    value = round(max(FLOOR, (1 + nps) / 2), 4)
    return {"level": band(value), "value": round(value, 4), "nps": round(nps, 4)}


def chart_level(pages: Iterable[Optional[tuple]]) -> dict[str, Any]:
    """{level, value, reason, counts} of a chart from its pages' (level, value, critical); None =
    a page not scored (2026-11-23, user: weighted levels; the level is the score's band)."""
    counts = {"high": 0, "medium": 0, "low": 0}
    total = weight = 0.0
    for page in pages:
        if page is None:
            continue
        level, critical = page[0], bool(page[2]) if len(page) > 2 else False
        counts[level] += 1
        w = CRITICAL_WEIGHT if critical else 1.0
        total += CHART_WEIGHTS[level] * w
        weight += w
    n = sum(counts.values())
    if not n:
        return {"level": None, "value": None, "reason": None, "counts": counts}
    value = round(max(FLOOR, total / weight), 4)  # rounded first: 80% exactly is High
    # 2026-11-23 (user): the level is the score's band, no guardrails.
    level, reason = ("high" if value >= HIGH_AT else "medium" if value >= LOW_BELOW else "low"), ""
    return {"level": level, "value": round(value, 4), "reason": reason, "counts": counts}


_PAGES_SQL = """
SELECT c.chart_name, to_jsonb(f) -> 'confidences', b.blank_junk_flag, q.quality_detail -> 'confidences',
       f.page_type
  FROM chart_list c
  JOIN page_list p ON p.chart_id = c.id
  LEFT JOIN imaging_final f ON f.page_id = p.id
  LEFT JOIN ocr_quality_results q ON q.page_id = p.id
  LEFT JOIN v_page_blank_junk_final b ON b.page_id = p.id
 WHERE c.chart_name = ANY(%s)
"""

_BLANK_JUNK_FLAGS = {"blank", "junk", "duplicate"}


def for_charts_postgres(database_url: str, db_schema: str, names: list[str]) -> dict[str, dict[str, Any]]:
    from app.services.db import connection

    pages: dict[str, list[Optional[tuple]]] = {name: [] for name in names}
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute(_PAGES_SQL, (names,))
        for chart, confidences, flag, measured, page_type in cur.fetchall():
            bj = str(flag or "").strip().lower() in _BLANK_JUNK_FLAGS
            pages.setdefault(str(chart), []).append(page_level(confidences, bj, measured, page_type))
    return {name: chart_level(found) for name, found in pages.items()}


def for_charts_cosmos(names: list[str]) -> dict[str, dict[str, Any]]:
    from app.services import backend
    from app.store.cosmos import summaries

    found = summaries.current(backend.cosmos(), names)
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        summary = found.get(name)
        if summary is None:
            out[name] = chart_level([])
            continue
        document = summaries.document(summary)
        out[name] = chart_level(
            page_level(page.finalConfidences or {},
                       page.isDuplicate is True or str(page.blankOrJunk or "").strip().lower().startswith("yes"),
                       page.measuredConfidences or {}, page.finalPageType)
            for page in document.pages
        )
    return out
