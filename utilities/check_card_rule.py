#!/usr/bin/env python3
"""How the card rule (blank / junk R3C) would call the pages already in the database,
against the reviewers' ground truth. Reads only; prints counts, never page text.

For every printed or visual page (the page-tag model's document_type) it runs
card_rules.detect on the page's best OCR (Final OCR 2, else Final OCR 1, else Tesseract)
and counts strong cards (junked), medium cards (flagged for review) and near misses by
what the ground truth says the page is: blank, junk, main, or no ground truth.

    cd core-pipeline && source .venv/bin/activate
    python ../utilities/check_card_rule.py
    python ../utilities/check_card_rule.py --run R2 --batch B4

Strong cards on "GT main" pages are the rule's false positives: each is a page R3C would
junk that a reviewer kept. Needs DATABASE_URL (env or core-pipeline/.env).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
for path in (CORE, CORE / "stages" / "lib" / "blank_junk"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    from dotenv import load_dotenv

    load_dotenv(CORE / ".env")
except ImportError:
    pass

from card_rules import detect  # noqa: E402
from db import connect  # noqa: E402

_PAGES = """
SELECT c.chart_name AS chart, p.page_number AS page,
       lower(COALESCE(to_jsonb(q) ->> 'document_type', '')) AS document_type,
       (SELECT o.raw_text FROM ocr_results o WHERE o.page_id = p.id
         ORDER BY array_position(ARRAY['azuredocintel','docling','tesseract']::text[], o.ocr_type::text)
         LIMIT 1) AS text,
       g.blank_page AS blank, g.junk_page AS junk
  FROM page_list p
  JOIN chart_list c ON c.id = p.chart_id
  LEFT JOIN ocr_quality_results q ON q.page_id = p.id
  LEFT JOIN LATERAL (SELECT t.blank_page, t.junk_page FROM page_ground_truth t
                      WHERE t.chart_name = c.chart_name AND t.page_number = p.page_number
                      ORDER BY t.updated_at DESC LIMIT 1) g ON TRUE
 WHERE (%(run)s::text IS NULL OR c.run_id = %(run)s)
   AND (%(batch)s::text IS NULL OR c.batch_id = %(batch)s)
"""


def _truth(blank: object, junk: object) -> str:
    def yes(value: object) -> bool:
        return str(value or "").strip().lower().startswith("y")

    if yes(blank):
        return "GT blank"
    if yes(junk):
        return "GT junk"
    if str(blank or "").strip() or str(junk or "").strip():
        return "GT main"
    return "no GT"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", help="only charts of this run_id")
    parser.add_argument("--batch", help="only charts of this batch_id")
    parser.add_argument("--list", action="store_true",
                        help="also name the chart and page of each strong card on a GT main page")
    args = parser.parse_args(argv)

    tally: Counter = Counter()
    misses: list[tuple[str, int, str]] = []
    pages = typed = 0
    with connect() as conn:
        for row in conn.execute(_PAGES, {"run": args.run, "batch": args.batch}).fetchall():
            row = dict(row) if not isinstance(row, tuple) else dict(
                zip(("chart", "page", "document_type", "text", "blank", "junk"), row)
            )
            chart, page, document_type, text = row["chart"], row["page"], row["document_type"], row["text"]
            blank, junk = row["blank"], row["junk"]
            pages += 1
            if document_type not in ("printed", "visual"):
                continue
            typed += 1
            card = detect(text or "")
            if card.is_card:
                kind = "strong card (junked)" if card.confidence == "strong" else "medium card (review)"
            elif card.blocked_by:
                kind = "near miss (kept)"
            else:
                continue
            truth = _truth(blank, junk)
            tally[(kind, truth)] += 1
            if kind.startswith("strong") and truth == "GT main":
                misses.append((chart, page, card.card_type))

    print(f"{pages} pages, {typed} printed or visual")
    for (kind, truth), count in sorted(tally.items()):
        print(f"  {kind:22s} {truth:9s} {count}")
    false_positives = sum(n for (kind, truth), n in tally.items() if kind.startswith("strong") and truth == "GT main")
    print(f"strong cards a reviewer kept (false positives): {false_positives}")
    if args.list:
        for chart, page, card_type in misses:
            print(f"  {chart} page {page}: {card_type}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
