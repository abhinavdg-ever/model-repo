"""Admins' Search: a word or phrase in the pages' preliminary OCR (Tesseract) text.

Reads ``ocr_results`` (``ocr_type = 'tesseract'``) joined to ``page_list`` and
``chart_list``, so it answers per page in either data mode, given a database. A
phrase's words may be split by any whitespace (Tesseract breaks lines), and
``whole_word`` keeps "pain" from matching "painful". Results are grouped by run /
batch, then chart, then page, each page with its hit count and a few snippets.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.chart_run_batch import with_run_batch_default
from app.services.db import connection

MIN_QUERY = 2
MAX_PAGES = 500
SNIPPETS_PER_PAGE = 3
SNIPPET_CONTEXT = 60  # characters either side of a hit


class SearchError(ValueError):
    """A query the search cannot run; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _words(query: str) -> list[str]:
    return [w for w in (query or "").split() if w]


def _pg_literal(word: str) -> str:
    """A word as a literal inside a Postgres (ARE) regular expression."""
    return "".join(ch if ch.isalnum() else "\\" + ch for ch in word)


def _like_literal(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def patterns(query: str, *, whole_word: bool) -> tuple[str, str, re.Pattern[str]]:
    """(ILIKE prefilter, Postgres regex, Python regex) for the query."""
    words = _words(query)
    longest = max(words, key=len)
    pg = r"\s+".join(_pg_literal(w) for w in words)
    py = r"\s+".join(re.escape(w) for w in words)
    if whole_word:
        pg = rf"\m{pg}\M"
        py = rf"(?<!\w){py}(?!\w)"
    return f"%{_like_literal(longest)}%", pg, re.compile(py, re.IGNORECASE)


def snippets(text: str, pattern: re.Pattern[str], limit: int = SNIPPETS_PER_PAGE) -> tuple[int, list[dict[str, str]]]:
    """(hit count, the first ``limit`` hits with their surrounding text, one line each)."""
    hits = list(pattern.finditer(text))
    out = []
    for m in hits[:limit]:
        start, end = max(0, m.start() - SNIPPET_CONTEXT), min(len(text), m.end() + SNIPPET_CONTEXT)
        flat = lambda s: " ".join(s.split())  # noqa: E731
        out.append({
            "before": ("…" if start else "") + flat(text[start:m.start()]),
            "match": flat(m.group(0)),
            "after": flat(text[m.end():end]) + ("…" if end < len(text) else ""),
        })
    return len(hits), out


def search_tesseract(
    database_url: str,
    db_schema: str,
    query: str,
    *,
    whole_word: bool = True,
    run_id: str | None = None,
    batch_id: str | None = None,
    max_pages: int = MAX_PAGES,
) -> dict[str, Any]:
    query = " ".join(_words(query))
    if len(query) < MIN_QUERY:
        raise SearchError(400, f"Search for at least {MIN_QUERY} characters")
    like, regex, pattern = patterns(query, whole_word=whole_word)

    sql = [
        "SELECT c.chart_name, c.run_id, c.batch_id, p.page_number, p.page_name, o.raw_text",
        "  FROM ocr_results o",
        "  JOIN page_list p ON p.id = o.page_id",
        "  JOIN chart_list c ON c.id = o.chart_id",
        " WHERE o.ocr_type = 'tesseract' AND o.raw_text ILIKE %s AND o.raw_text ~* %s",
    ]
    params: list[Any] = [like, regex]
    if (run_id or "").strip():
        sql.append("   AND c.run_id = %s")
        params.append(run_id.strip())
    if (batch_id or "").strip():
        sql.append("   AND c.batch_id = %s")
        params.append(batch_id.strip())
    sql.append(" ORDER BY c.run_id NULLS LAST, c.batch_id NULLS LAST, c.chart_name, p.page_number")
    sql.append(" LIMIT %s")
    params.append(max_pages + 1)

    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute("\n".join(sql), params)
        rows = cur.fetchall()

    truncated = len(rows) > max_pages
    charts: dict[str, dict[str, Any]] = {}
    for chart_name, run, batch, page_number, page_name, text in rows[:max_pages]:
        count, found = snippets(text or "", pattern)
        if not count:  # the regex flavours can differ at the edges; Python has the last word
            continue
        shown_run, shown_batch = with_run_batch_default(run, batch)
        chart = charts.setdefault(chart_name, {
            "chart": chart_name, "run": shown_run, "batch": shown_batch, "hits": 0, "pages": [],
        })
        chart["hits"] += count
        chart["pages"].append({"page_number": page_number, "page_name": page_name, "hits": count,
                               "snippets": found})

    batches: dict[tuple[str, str], dict[str, Any]] = {}
    for chart in charts.values():
        b = batches.setdefault((chart["run"], chart["batch"]), {
            "run": chart["run"], "batch": chart["batch"], "charts": 0, "pages": 0, "hits": 0,
        })
        b["charts"] += 1
        b["pages"] += len(chart["pages"])
        b["hits"] += chart["hits"]

    return {
        "query": query,
        "whole_word": whole_word,
        "charts": list(charts.values()),
        "batches": list(batches.values()),
        "total_charts": len(charts),
        "total_pages": sum(len(c["pages"]) for c in charts.values()),
        "total_hits": sum(c["hits"] for c in charts.values()),
        "truncated": truncated,
        "max_pages": max_pages,
    }
