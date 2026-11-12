#!/usr/bin/env python3
"""Pages whose OCR engines disagree: Match rate (OCR overlap) under a cutoff.

The same Match rate review-ui shows on the OCR page: each engine's page text (Tesseract,
Final OSS / Docling, Final AzDocInt) as a set of words; Jaccard overlap of every pair of
engines that have text; the average. A page needs text from at least 2 engines.

Read only. Prints and writes a CSV of chart, page id, page name, rate and the pairs:
never any page text.

    python3 utilities/ocr_low_overlap.py                 # VM / Mac
    python utilities\\ocr_low_overlap.py                  # Windows (Command Prompt)

    pip install "psycopg[binary]"    (if neither psycopg nor psycopg2 is installed)
"""
# ============================ PARAMETERS ============================
DATABASE_URL = ""          # "" = DATABASE_URL from the environment, else core-pipeline/.env
CUTOFF = 0.10              # pages with Match rate below this (0.10 = 10%)
CHARTS = []                # [] = every chart; or ["67993625_56070967", ...]
OUTPUT_CSV = "ocr_low_overlap.csv"
# ====================================================================

import csv
import json
import os
import re
import sys
from itertools import combinations
from pathlib import Path

ENGINES = {"tesseract": "Tess", "docling": "OSS", "azuredocintel": "AzDocInt"}


def database_url() -> str:
    if DATABASE_URL:
        return DATABASE_URL
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = Path(__file__).resolve().parents[1] / "core-pipeline" / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("Set DATABASE_URL at the top of this script (or in the environment).")


def connect(url: str):
    try:
        import psycopg

        return psycopg.connect(url, connect_timeout=15)
    except ImportError:
        import psycopg2

        return psycopg2.connect(url, connect_timeout=15)


def page_text(ocr_type: str, raw: str | None) -> str:
    """The page's text as review-ui shows it: Final OCR JSON -> its content."""
    body = (raw or "").strip()
    if ocr_type != "tesseract" and body.startswith("{"):
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                body = str(data.get("content") or data.get("markdown") or "").strip()
        except ValueError:
            pass
    if body.startswith("Skipped for ") or body.startswith("Skipped because of"):
        return ""
    return re.sub(r"<!--\s*image\s*-->", " ", body, flags=re.IGNORECASE)


def words(text: str) -> set[str]:
    """review-ui's tokenize(): lower case, letters and digits, words of 2+ characters."""
    return {t for t in re.sub(r"[\W_]+", " ", text.lower()).split() if len(t) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


SQL = """
SELECT c.chart_name, p.id, p.page_name, o.ocr_type, o.raw_text
  FROM ocr_results o
  JOIN page_list p ON p.id = o.page_id
  JOIN chart_list c ON c.id = o.chart_id
 {where}
 ORDER BY c.chart_name, p.id
"""


def main() -> int:
    where, args = "", ()
    if CHARTS:
        where, args = "WHERE c.chart_name = ANY(%s)", (list(CHARTS),)
    pages: dict[tuple, dict[str, set[str]]] = {}
    with connect(database_url()) as conn:
        cur = conn.cursor()
        cur.execute("SET default_transaction_read_only = on")
        cur.execute(SQL.format(where=where), args)
        for chart, page_id, page_name, ocr_type, raw in cur:
            if ocr_type not in ENGINES:
                continue
            tokens = words(page_text(ocr_type, raw))
            entry = pages.setdefault((chart, page_id, page_name), {})
            if tokens:
                entry[ENGINES[ocr_type]] = tokens

    low, compared = [], 0
    for (chart, page_id, page_name), by_engine in pages.items():
        if len(by_engine) < 2:
            continue
        compared += 1
        pairs = [(a, b, jaccard(by_engine[a], by_engine[b])) for a, b in combinations(sorted(by_engine), 2)]
        rate = sum(r for _, _, r in pairs) / len(pairs)
        if rate < CUTOFF:
            low.append({
                "chart_name": chart, "page_id": page_id, "page_name": page_name,
                "match_rate_pct": round(rate * 100, 1), "engines": "+".join(sorted(by_engine)),
                "pairs": "; ".join(f"{a}-{b} {r * 100:.0f}%" for a, b, r in pairs),
            })

    low.sort(key=lambda r: (r["chart_name"], r["page_id"]))
    print(f"pages with 2+ OCR engines: {compared:,}   below {CUTOFF * 100:.0f}%: {len(low):,}   "
          f"charts: {len({r['chart_name'] for r in low}):,}")
    for r in low:
        print(f"  {r['chart_name']}  page_id {r['page_id']}  {r['page_name']}  "
              f"{r['match_rate_pct']}%  ({r['pairs']})")
    fields = ["chart_name", "page_id", "page_name", "match_rate_pct", "engines", "pairs"]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(low)
    print(f"written: {OUTPUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
