"""Why two pages of a chart are not duplicates: every pair over 90% alike and the check that stops it.

    python utilities/dup_probe.py <chart folder>        # e.g. core-pipeline/data/folders/<chart>
    python utilities/dup_probe.py <chart folder> 12 13  # one pair, by page number

Reads the chart's Final OCR (ocr/<chart>_final2.json over _final1.json) and its blank / junk
verdicts (staging/blank_junk.json). Prints page numbers, scores and rule names only, never
page text. Run it from the repo root with the core-pipeline (or tests) venv.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core-pipeline"))

import module.blank_junk.stage as bj_stage  # noqa: E402  (puts the blank_junk lib on sys.path)
from classify import duplicate_char_count, duplicate_window, word_overlap  # noqa: E402

page_dates = bj_stage.page_dates


def _texts(folder: Path) -> list[tuple[str, str]]:
    pages: dict[str, str] = {}
    order: list[str] = []
    for kind in ("final1", "final2"):
        f = folder / "ocr" / f"{folder.name}_{kind}.json"
        if not f.is_file():
            continue
        for i, page in enumerate(json.loads(f.read_text(encoding="utf-8")).get("pages") or []):
            name = str(page.get("fileName") or page.get("pageNumber") or i + 1)
            text = page.get("content") or page.get("markdown") or ""
            if name not in pages:
                order.append(name)
            if text.strip():
                pages[name] = text
    return [(name, pages.get(name, "")) for name in order]


def _verdicts(folder: Path) -> dict[str, str]:
    f = folder / "staging" / "blank_junk.json"
    if not f.is_file():
        return {}
    pages = json.loads(f.read_text(encoding="utf-8")).get("pages") or {}
    return {name: str((entry.get("final") or {}).get("decision") or "") for name, entry in pages.items()}


def _why(a: str, b: str, verdict_a: str, verdict_b: str) -> str:
    if "Duplicate" in (verdict_a, verdict_b):
        return f"already marked ({verdict_a or '?'} / {verdict_b or '?'})"
    if verdict_a and verdict_a != "Main" or verdict_b and verdict_b != "Main":
        return f"not compared: only Main pages ({verdict_a or '?'} / {verdict_b or '?'})"
    la, lb = duplicate_char_count(a), duplicate_char_count(b)
    if min(la, lb) < 300:
        return "not compared: under 300 characters"
    da, db = page_dates(a), page_dates(b)
    if da != db:
        return f"skipped: written dates differ ({len(da ^ db)} date(s) on one page only)"
    w = word_overlap(a, b)
    if min(la, lb) < 0.9 * max(la, lb):
        return f"no duplicate: lengths differ by {1 - min(la, lb) / max(la, lb):.0%} (> 10%)"
    if w.contained >= 1.0 or w.overlap >= 0.98 or (w.overlap >= 0.8 and w.different < 8):
        return "DUPLICATE by the pairwise rule (check DOS, signature-apart and continuity)"
    if w.overlap >= 0.95 or (w.overlap >= 0.8 and w.different < 15):
        return "PARTIAL"
    return "none: below 95% and 15+ words different"


def main() -> int:
    folder = Path(sys.argv[1])
    pages = _texts(folder)
    verdicts = _verdicts(folder)
    names = [name for name, _ in pages]
    index = {str(i + 1): i for i in range(len(pages))}
    if len(sys.argv) == 4:
        pairs = [(index[sys.argv[2]], index[sys.argv[3]])]
    else:
        window = duplicate_window(len(pages))
        pairs = [(i, j) for i in range(len(pages)) for j in range(i + 1, min(len(pages), i + window + 1))]
    shown = 0
    for i, j in pairs:
        (na, a), (nb, b) = pages[i], pages[j]
        if not (a.strip() and b.strip()):
            continue
        w = word_overlap(a, b)
        if len(sys.argv) != 4 and w.overlap < 0.9 and w.contained < 1.0:
            continue
        shown += 1
        print(f"p{i + 1} ({na}) vs p{j + 1} ({nb}): overlap {w.overlap:.3f}, contained {w.contained:.3f}, "
              f"{w.different} words different -> {_why(a, b, verdicts.get(na, ''), verdicts.get(nb, ''))}")
    print(f"{shown} pair(s) over 90% alike in {len(names)} pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
