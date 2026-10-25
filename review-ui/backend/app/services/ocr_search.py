"""Admins' Search: a word or phrase in the pages' preliminary OCR (Tesseract) text
and in their section headers.

Words: ``ocr_results`` (``ocr_type = 'tesseract'``) joined to ``page_list`` and
``chart_list``. Section headers: ``additional_page_details.section_headers``, or,
for a page without that row, the ``section_headers`` of its Final OCR JSON (Docling,
Azure). A phrase's words may be split by any whitespace (Tesseract breaks lines), and
``whole_word`` keeps "pain" from matching "painful". Results are one row per chart
(its page count, the pages and hits in the text, the headers that match), grouped by
run / batch, each chart with its pages' snippets and its matching headers.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.services.chart_run_batch import with_run_batch_default
from app.services.db import connection

MIN_QUERY = 2
MAX_PAGES = 500
MAX_HEADERS = 2000
SNIPPETS_PER_PAGE = 3
SNIPPET_CONTEXT = 60  # characters either side of a hit

logger = logging.getLogger(__name__)

# Headers kept per page: the pipeline's accepted ones, else the Final OCR JSON's (one
# row per header; the same header from both engines is counted once, in Python).
_HEADERS_SQL = """
SELECT c.chart_name, c.run_id, c.batch_id, p.page_number, p.page_name, h->>'text'
  FROM additional_page_details a
  JOIN page_list p ON p.id = a.page_id
  JOIN chart_list c ON c.id = a.chart_id
 CROSS JOIN LATERAL jsonb_array_elements(a.section_headers) h
 WHERE jsonb_typeof(a.section_headers) = 'array' AND h->>'text' ~* %s{filters}
 LIMIT %s
"""
_OCR_JSON_HEADERS_SQL = """
SELECT c.chart_name, c.run_id, c.batch_id, p.page_number, p.page_name, h->>'text'
  FROM ocr_results o
  JOIN page_list p ON p.id = o.page_id
  JOIN chart_list c ON c.id = o.chart_id
 CROSS JOIN LATERAL jsonb_array_elements(
       CASE WHEN jsonb_typeof(o.raw_text::jsonb -> 'section_headers') = 'array'
            THEN o.raw_text::jsonb -> 'section_headers' ELSE '[]'::jsonb END) h
 WHERE o.ocr_type IN ('docling', 'azuredocintel')
   AND left(ltrim(o.raw_text), 1) = '{' AND o.raw_text ILIKE %s
   AND NOT EXISTS (SELECT 1 FROM additional_page_details a WHERE a.page_id = o.page_id)
   AND h->>'text' ~* %s{filters}
 LIMIT %s
"""
_PAGE_COUNT_SQL = """
SELECT c.chart_name, count(p.id)
  FROM chart_list c
  LEFT JOIN page_list p ON p.chart_id = c.id
 WHERE c.chart_name = ANY(%s)
 GROUP BY c.chart_name
"""


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

    filters, filter_params = "", []
    if (run_id or "").strip():
        filters += " AND c.run_id = %s"
        filter_params.append(run_id.strip())
    if (batch_id or "").strip():
        filters += " AND c.batch_id = %s"
        filter_params.append(batch_id.strip())
    sql = [
        "SELECT c.chart_name, c.run_id, c.batch_id, p.page_number, p.page_name, o.raw_text",
        "  FROM ocr_results o",
        "  JOIN page_list p ON p.id = o.page_id",
        "  JOIN chart_list c ON c.id = o.chart_id",
        " WHERE o.ocr_type = 'tesseract' AND o.raw_text ILIKE %s AND o.raw_text ~* %s" + filters,
        " ORDER BY c.run_id NULLS LAST, c.batch_id NULLS LAST, c.chart_name, p.page_number",
        " LIMIT %s",
    ]
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute("\n".join(sql), [like, regex, *filter_params, max_pages + 1])
        rows = cur.fetchall()
        cur.execute(_HEADERS_SQL.replace("{filters}", filters), [regex, *filter_params, MAX_HEADERS + 1])
        header_rows = list(cur.fetchall())
    try:  # older pages: headers only in the OCR JSON, which may not all parse as JSON
        with connection(database_url, db_schema) as conn, conn.cursor() as cur:
            cur.execute(_OCR_JSON_HEADERS_SQL.replace("{filters}", filters),
                        [like, regex, *filter_params, MAX_HEADERS + 1])
            header_rows += cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — the words and the stored headers still answer
        logger.warning("OCR search: headers from the OCR JSON skipped: %s", exc)

    truncated = len(rows) > max_pages or len(header_rows) > MAX_HEADERS
    charts: dict[str, dict[str, Any]] = {}
    order: dict[str, tuple] = {}

    def chart_row(chart_name: str, run: Any, batch: Any) -> dict[str, Any]:
        if chart_name not in charts:
            shown_run, shown_batch = with_run_batch_default(run, batch)
            charts[chart_name] = {"chart": chart_name, "run": shown_run, "batch": shown_batch, "total_pages": None,
                                  "hits": 0, "pages": [], "header_hits": 0, "header_pages": 0, "headers": []}
            order[chart_name] = (run is None, str(run or ""), batch is None, str(batch or ""), chart_name)
        return charts[chart_name]

    for chart_name, run, batch, page_number, page_name, text in rows[:max_pages]:
        count, found = snippets(text or "", pattern)
        if not count:  # the regex flavours can differ at the edges; Python has the last word
            continue
        chart = chart_row(chart_name, run, batch)
        chart["hits"] += count
        chart["pages"].append({"page_number": page_number, "page_name": page_name, "hits": count,
                               "snippets": found})

    seen_headers: set[tuple[str, str, str]] = set()
    for chart_name, run, batch, page_number, page_name, header in header_rows[:MAX_HEADERS]:
        header = " ".join(str(header or "").split())
        key = (chart_name, str(page_name), header.lower())
        if not header or key in seen_headers:
            continue
        seen_headers.add(key)
        count, shown = snippets(header, pattern, limit=1)
        if not count:
            continue
        chart = chart_row(chart_name, run, batch)
        chart["header_hits"] += count
        chart["headers"].append({"page_number": page_number, "page_name": page_name, "text": header,
                                 "hits": count, "snippet": shown[0]})

    if charts:
        with connection(database_url, db_schema) as conn, conn.cursor() as cur:
            cur.execute(_PAGE_COUNT_SQL, [list(charts)])
            for chart_name, total in cur.fetchall():
                if chart_name in charts:
                    charts[chart_name]["total_pages"] = int(total)

    ordered = [charts[name] for name in sorted(charts, key=order.__getitem__)]
    for chart in ordered:
        chart["headers"].sort(key=lambda h: (h["page_number"] is None, h["page_number"] or 0, h["text"]))
        chart["header_pages"] = len({h["page_name"] for h in chart["headers"]})

    batches: dict[tuple[str, str], dict[str, Any]] = {}
    for chart in ordered:
        b = batches.setdefault((chart["run"], chart["batch"]), {
            "run": chart["run"], "batch": chart["batch"], "charts": 0, "pages": 0, "hits": 0, "header_hits": 0,
        })
        b["charts"] += 1
        b["pages"] += len(chart["pages"])
        b["hits"] += chart["hits"]
        b["header_hits"] += chart["header_hits"]

    return {
        "query": query,
        "whole_word": whole_word,
        "charts": ordered,
        "batches": list(batches.values()),
        "total_charts": len(ordered),
        "total_pages": sum(len(c["pages"]) for c in ordered),
        "total_hits": sum(c["hits"] for c in ordered),
        "total_header_hits": sum(c["header_hits"] for c in ordered),
        "truncated": truncated,
        "max_pages": max_pages,
    }
