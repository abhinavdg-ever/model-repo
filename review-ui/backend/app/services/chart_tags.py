"""A chart's tags (2026-11-07, user): chart_list.tags, a JSON array of strings set with
utilities/tag_chart.py. History and Accuracy offer them as the Tag filter. review-ui only reads
them.
"""

from __future__ import annotations

import re

from app.services.db import connection

MAX_TAG_CHARS = 60
MAX_TAGS = 20


def clean_tags(tags: list[str]) -> list[str]:
    """Trimmed, inner spaces collapsed, at most MAX_TAG_CHARS each, no empties, no repeats
    (without case), in the order given, at most MAX_TAGS. utilities/tag_chart.py uses the same
    rule."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = re.sub(r"\s+", " ", str(raw or "")).strip()[:MAX_TAG_CHARS].strip()
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            out.append(tag)
    if len(out) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags per chart")
    return out


def chart_tags_map(database_url: str, db_schema: str = "public") -> dict[str, list[str]]:
    """chart name -> its tags. Read through to_jsonb, so a database without the column
    (before schema/migrations/2026-11-07.sql) answers no tags instead of failing."""
    with connection(database_url, db_schema) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.chart_name, to_jsonb(c) -> 'tags'
                  FROM chart_list c
                 WHERE jsonb_typeof(to_jsonb(c) -> 'tags') = 'array'
                   AND jsonb_array_length(to_jsonb(c) -> 'tags') > 0
                """
            )
            rows = cur.fetchall()
    out: dict[str, list[str]] = {}
    for chart, tags in rows:
        cleaned = clean_tags([str(t) for t in (tags or []) if isinstance(t, (str, int, float))][:MAX_TAGS])
        if cleaned:
            out[str(chart)] = sorted(cleaned, key=str.casefold)
    return out
