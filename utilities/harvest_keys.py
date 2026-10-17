#!/usr/bin/env python3
"""Harvest every extraction key, and every label written before a date, from the chart folders.

Scans ``review-ui/data/folders/*`` and writes three CSVs:

``keys_rows.csv``
    One row per staged key/value pair in ``staging/extraction.json``: chart, page,
    field, key, tier, region, source, value, from / to, rule score, model score,
    accepted, selected, status and the sentence it was read from. ``status`` is
    ``accepted``, ``no_value`` (the key was found but no value was read: a failed
    pair), ``rejected`` (a value the rules turned down) or ``printed`` (a print key).

``keys_summary.csv``
    One row per (field, key, tier): how often it was found, read a value, was
    accepted, was selected, failed; in how many charts; example sentences of the
    failures and the successes.

``date_labels.csv``
    Read from the OCR text, not the staging: for every date on every page, the
    words just before it on the same line ('Date of Visit:', 'Printed On',
    'Office Visit'), counted. ``known_tier`` is the tier of the DOS key in
    ``extraction/dos/keys.json`` the label ends with, blank when no key covers it:
    those are the date keys the extractor cannot see yet. Text preference per page
    is Final2, then Final1, then Tesseract.

Standard library only; no venv needed::

    python utilities/harvest_keys.py
    python utilities/harvest_keys.py --field dos --out /tmp/keys
    python utilities/harvest_keys.py --folders review-ui/data/folders --label-words 4
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_FOLDERS = REPO / "review-ui" / "data" / "folders"
DOS_KEYS = REPO / "core-pipeline" / "stages" / "lib" / "extraction" / "dos" / "keys.json"

# Field lists that are not key/value pairs.
NOT_KEYED = {"heading_heron"}

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
DATE_RE = re.compile(
    r"(?<![\d/-])(?:"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|\d{1,2} ?[/-] ?\d{1,2} ?[/-] ?(?:\d{4}|\d{2})"
    r"|" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"\.?,?\s+\d{4}"
    r")(?!\d)",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z.#/&'-]*")


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def load_dos_keys() -> dict[str, str]:
    """letters-only key -> tier."""
    if not DOS_KEYS.is_file():
        return {}
    data = json.loads(DOS_KEYS.read_text(encoding="utf-8"))
    return {_letters(key): tier for tier, keys in data.items() for key in keys}


def chart_folders(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "._")))


# --- staging -------------------------------------------------------------------


def _status(row: dict) -> str:
    if row.get("tier") == "printed":
        return "printed"
    if not row.get("value"):
        return "no_value"
    return "accepted" if row.get("accepted") else "rejected"


def staged_rows(folder: Path, field: str) -> list[dict]:
    path = folder / "staging" / "extraction.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    pages = sorted(data.get("pages", {}).items(), key=lambda kv: kv[1].get("page_number") or 0)
    for page_name, page in pages:
        for field_id, rows in (page.get("fields") or {}).items():
            if field_id in NOT_KEYED or (field != "all" and field_id != field):
                continue
            for row in rows:
                out.append({
                    "chart": data.get("chart_name") or folder.name,
                    "extracted_at": data.get("extracted_at", ""),
                    "page": page_name,
                    "field": field_id,
                    "key": row.get("key", ""),
                    "tier": row.get("tier", ""),
                    "region": row.get("region", ""),
                    "source": row.get("source", ""),
                    "value": row.get("value", ""),
                    "dos_from": row.get("dos_from", ""),
                    "dos_to": row.get("dos_to", ""),
                    "score": row.get("score", ""),
                    "model_score": row.get("model_score", ""),
                    "accepted": bool(row.get("accepted")),
                    "selected": bool(row.get("selected")),
                    "status": _status(row),
                    "sentence": " ".join(str(row.get("sentence", "")).split()),
                })
    return out


def summarize(rows: list[dict], examples: int) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["field"], row["key"], row["tier"])].append(row)
    out = []
    for (field, key, tier), items in groups.items():
        status = Counter(r["status"] for r in items)
        failed = [r for r in items if r["status"] == "no_value"]
        good = [r for r in items if r["status"] == "accepted"]
        out.append({
            "field": field, "key": key, "tier": tier,
            "found": len(items),
            "with_value": sum(1 for r in items if r["value"]),
            "accepted": status["accepted"],
            "selected": sum(1 for r in items if r["selected"]),
            "no_value": status["no_value"],
            "rejected": status["rejected"],
            "printed": status["printed"],
            "charts": len({r["chart"] for r in items}),
            "failed_examples": " || ".join(f"{r['chart']}/{r['page']}: {r['sentence'][:160]}" for r in failed[:examples]),
            "accepted_examples": " || ".join(f"{r['chart']}/{r['page']}: {r['key']} = {r['value']}" for r in good[:examples]),
        })
    return sorted(out, key=lambda r: (r["field"], -r["found"]))


# --- OCR labels ----------------------------------------------------------------


def _ocr_json_pages(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(p.get("fileName") or p.get("pageNumber")): str(p.get("content") or p.get("markdown") or "")
        for p in data.get("pages", [])
    }


def _prelim_pages(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"^=====\s*(.+?)\s*=====\s*$", text, flags=re.MULTILINE)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def page_texts(folder: Path) -> dict[str, tuple[str, str]]:
    """page -> (source, text): Final2, else Final1, else Tesseract."""
    ocr = folder / "ocr"
    sources = [
        ("final2", _ocr_json_pages(ocr / f"{folder.name}_final2.json")),
        ("final1", _ocr_json_pages(ocr / f"{folder.name}_final1.json")),
        ("prelim", _prelim_pages(ocr / f"{folder.name}_prelim.txt")),
    ]
    pages: dict[str, tuple[str, str]] = {}
    for name, texts in sources:
        for page, text in texts.items():
            if page not in pages and text.strip() and text.strip().upper() not in ("ACCEPT", "UNACCEPT"):
                pages[page] = (name, text)
    return pages


def labels_before_dates(text: str, n_words: int) -> list[tuple[str, str, str]]:
    """(label, date, line) for every date with words before it on its line."""
    found = []
    for line in text.splitlines():
        for match in DATE_RE.finditer(line):
            before = line[: match.start()]
            # Only the words since the previous date or table cell.
            before = re.split(r"\||" + DATE_RE.pattern, before, flags=re.IGNORECASE)[-1] or ""
            words = WORD_RE.findall(before)[-n_words:]
            if words:
                found.append((" ".join(words), match.group(0), " ".join(line.split())))
    return found


def known_tier(label: str, keys: dict[str, str]) -> str:
    """The tier of the longest DOS key the label ends with."""
    letters = _letters(label)
    matching = [key for key in keys if key and letters.endswith(key)]
    return keys[max(matching, key=len)] if matching else ""


def harvest_labels(folders: list[Path], n_words: int, examples: int) -> list[dict]:
    keys = load_dos_keys()
    counts: dict[str, dict] = {}
    for folder in folders:
        for page, (source, text) in page_texts(folder).items():
            for label, raw_date, line in labels_before_dates(text, n_words):
                norm = " ".join(label.lower().rstrip(":.#").split())
                entry = counts.setdefault(norm, {
                    "label": norm, "count": 0, "charts": set(), "pages": set(),
                    "known_tier": known_tier(norm, keys), "examples": [],
                })
                entry["count"] += 1
                entry["charts"].add(folder.name)
                entry["pages"].add(f"{folder.name}/{page}")
                if len(entry["examples"]) < examples:
                    entry["examples"].append(f"{folder.name}/{page} [{source}]: {line[:160]}")
    return sorted(
        (
            {**e, "charts": len(e["charts"]), "pages": len(e["pages"]), "examples": " || ".join(e["examples"])}
            for e in counts.values()
        ),
        key=lambda r: (-r["count"], r["label"]),
    )


# --- main ----------------------------------------------------------------------


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        if not rows:
            return
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {len(rows):6d} rows -> {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--folders", type=Path, default=DEFAULT_FOLDERS, help="chart folders root")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "output" / "keys")
    parser.add_argument("--field", default="dos", help="staging field to harvest (dos, dob, member_id, ...) or 'all'")
    parser.add_argument("--label-words", type=int, default=4, help="words kept before a date (date_labels.csv)")
    parser.add_argument("--examples", type=int, default=3, help="example sentences per row")
    args = parser.parse_args()

    folders = chart_folders(args.folders)
    staged = [f for f in folders if (f / "staging" / "extraction.json").is_file()]
    print(f"{len(folders)} chart folders, {len(staged)} with staging -> {args.out}")

    rows = [row for folder in staged for row in staged_rows(folder, args.field)]
    write(args.out / "keys_rows.csv", rows)
    write(args.out / "keys_summary.csv", summarize(rows, args.examples))
    write(args.out / "date_labels.csv", harvest_labels(folders, args.label_words, args.examples))


if __name__ == "__main__":
    main()
