#!/usr/bin/env python3
"""Step 1 of the chart-type trainer: a folder of charts -> one row per page, no text kept.

    python ingest_folder.py --root <folder> [--out work]

<folder> has the same format as core-pipeline/outputs/charts (one subfolder per chart, as the
pipeline writes it); core-pipeline/outputs/charts itself works. The folder is only read.
Only ocr/<chart>_prelim.txt is required per chart; imaging/*_final.csv is used when present.

Writes:
  <out>/pages.csv   chart, page, page type / specialty / blank-junk / duplicate (from the
                    pipeline CSV when present), capture traits measured on the text
  <out>/charts.csv  one row per chart: pages, how many have pipeline labels, images
and prints a short report. Page types for charts without imaging/*_final.csv come from the
pipeline's page-leaf model in the next step (not built yet): their page_type is left empty.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chart_types.folder import FINAL_COLUMNS, iter_charts, text_traits  # noqa: E402

PAGE_COLUMNS = ["chart_name", "page_name", "page_no", "labels_from", *FINAL_COLUMNS, "input_dpi",
                "n_chars", "n_lines", "n_words", "garbled_share", "digit_share", "label_line_share", "fax_top"]
CHART_COLUMNS = ["chart_name", "pages", "pages_with_labels", "images", "progress_note_pages"]


def _page_no(name: str, fallback: int) -> int:
    digits = "".join(ch for ch in Path(name).stem if ch.isdigit())
    return int(digits) if digits else fallback


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, required=True, help="folder of chart folders (outputs/charts format)")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "work")
    args = ap.parse_args(argv)
    if not args.root.is_dir():
        print(f"not a folder: {args.root}")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)

    pages_out, charts_out = [], []
    for chart in iter_charts(args.root):
        labelled = notes = 0
        for i, (page_name, text) in enumerate(chart.pages.items(), start=1):
            row = {"chart_name": chart.name, "page_name": page_name, "page_no": _page_no(page_name, i)}
            final = chart.final.get(page_name)
            row["labels_from"] = "pipeline_csv" if final else ""
            for col in FINAL_COLUMNS:
                row[col] = (final or {}).get(col, "")
            labelled += bool(final)
            notes += row["page_type"].startswith("Progress Note")
            row["input_dpi"] = chart.dpi.get(page_name, "")
            row.update(text_traits(text))
            pages_out.append(row)
        charts_out.append({"chart_name": chart.name, "pages": len(chart.pages), "pages_with_labels": labelled,
                           "images": chart.images, "progress_note_pages": notes})

    for name, cols, rows in (("pages.csv", PAGE_COLUMNS, pages_out), ("charts.csv", CHART_COLUMNS, charts_out)):
        with (args.out / name).open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)

    n = len(charts_out)
    with_csv = sum(1 for c in charts_out if c["pages_with_labels"])
    with_img = sum(1 for c in charts_out if c["images"])
    print(f"{n} charts, {len(pages_out)} pages -> {args.out}")
    print(f"  pipeline labels: {with_csv}/{n} charts   images: {with_img}/{n} charts"
          f"{'  (image look will be used)' if n and with_img / n >= 0.9 else '  (text only)'}")
    print(f"  Progress Note pages (labelled charts): {sum(c['progress_note_pages'] for c in charts_out)}")
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
