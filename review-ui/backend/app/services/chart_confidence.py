"""Chart confidence for the History list (2026-11-19, user).

The same rule as the frontend's ``confidenceLevel.ts`` (keep the two in step):

* Features: the accuracy list's seven -- Member Verification (the lower of Member Name and
  Member DOB), DOS, Blank / Junk, Page Type, Codability, Provider Signature, Orientation -- each
  from ``imaging_final.confidences``. A Blank / Junk or duplicate page counts Blank / Junk and
  Codability only. High >= 0.80, Medium 0.50-0.80, Low < 0.50.
* Page: High when every feature is High; Low when any feature is Low; else Medium.
* Chart: High when every page is High; Low when Low pages are 10% of the pages or more; else
  Medium. Its value is the mean of its pages' means.
* A % is capped by its level: Medium at most 79%, Low at most 49% (LEVEL_CAP).
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

HIGH_AT = 0.8
LOW_BELOW = 0.5
LOW_SHARE = 0.1
# The % never contradicts the level (2026-11-19, user): Medium at most 79%, Low at most 49%.
LEVEL_CAP = {"high": 1.0, "medium": 0.79, "low": 0.49}

FEATURES = ["member", "dos", "blank_junk", "page_type", "codability", "provider", "orientation"]
BLANK_JUNK_FEATURES = {"blank_junk", "codability"}


def band(value: float) -> str:
    return "high" if value >= HIGH_AT else "medium" if value >= LOW_BELOW else "low"


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


def page_level(confidences: Any, blank_junk_or_duplicate: bool) -> Optional[tuple[str, float]]:
    """(level, mean) of one page, or None when it has no feature with a confidence."""
    conf = _confidences(confidences)
    values: dict[str, float] = {}
    for key in FEATURES:
        if blank_junk_or_duplicate and key not in BLANK_JUNK_FEATURES:
            continue
        if key == "member":
            parts = [v for v in (_num(conf.get("member_name")), _num(conf.get("member_dob"))) if v is not None]
            value = min(parts) if parts else None
        else:
            value = _num(conf.get(key))
        if value is not None:
            values[key] = value
    if not values:
        return None
    levels = {key: band(v) for key, v in values.items()}
    # 2026-11-19 (user): every feature matters, so any Low feature makes the page Low.
    found = set(levels.values())
    level = "low" if "low" in found else "medium" if "medium" in found else "high"
    return level, min(sum(values.values()) / len(values), LEVEL_CAP[level])


def chart_level(pages: Iterable[Optional[tuple[str, float]]]) -> dict[str, Any]:
    """{level, value, counts} of a chart from its pages' (level, mean); level None = not scored."""
    counts = {"high": 0, "medium": 0, "low": 0}
    total = 0.0
    n = 0
    for page in pages:
        if page is None:
            continue
        counts[page[0]] += 1
        total += page[1]
        n += 1
    if not n:
        return {"level": None, "value": None, "counts": counts}
    level = "high" if counts["high"] == n else "low" if counts["low"] / n >= LOW_SHARE else "medium"
    return {"level": level, "value": round(min(total / n, LEVEL_CAP[level]), 4), "counts": counts}


_PAGES_SQL = """
SELECT c.chart_name, to_jsonb(f) -> 'confidences', b.blank_junk_flag
  FROM chart_list c
  JOIN page_list p ON p.chart_id = c.id
  LEFT JOIN imaging_final f ON f.page_id = p.id
  LEFT JOIN v_page_blank_junk_final b ON b.page_id = p.id
 WHERE c.chart_name = ANY(%s)
"""

_BLANK_JUNK_FLAGS = {"blank", "junk", "duplicate"}


def for_charts_postgres(database_url: str, db_schema: str, names: list[str]) -> dict[str, dict[str, Any]]:
    from app.services.db import connection

    pages: dict[str, list[Optional[tuple[str, float]]]] = {name: [] for name in names}
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute(_PAGES_SQL, (names,))
        for chart, confidences, flag in cur.fetchall():
            bj = str(flag or "").strip().lower() in _BLANK_JUNK_FLAGS
            pages.setdefault(str(chart), []).append(page_level(confidences, bj))
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
                       page.isDuplicate is True or str(page.blankOrJunk or "").strip().lower().startswith("yes"))
            for page in document.pages
        )
    return out
