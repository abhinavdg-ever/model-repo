"""Shared file/path helpers for chart workspace."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from config import IMAGE_SUFFIXES, imaging_dir, ocr_dir, pages_dir

PAGE_MARKER_RE = re.compile(r"^=====\s*(.+?)\s*=====\s*$", re.MULTILINE)
PAGE_NUM_RE = re.compile(r"^(\d+)\.(jpe?g|png|webp|tif{1,2})$", re.IGNORECASE)


def list_local_pages(chart_name: str) -> list[Path]:
    root = pages_dir(chart_name)
    if not root.is_dir():
        return []
    files = [
        p for p in root.iterdir()
        if p.is_file()
        and p.suffix.lower() in IMAGE_SUFFIXES
        # macOS AppleDouble stubs. `._1.jpg` carries the resource fork of
        # `1.jpg`, not an image — copying onto exFAT/SMB creates one per page,
        # so an intake that filtered them at the source (download_blob,
        # batch_intake both do) still finds them here, on the destination.
        # Registering them doubles page_count and fails every stage on them.
        and not p.name.startswith("._")
    ]

    def sort_key(p: Path):
        m = PAGE_NUM_RE.match(p.name)
        if m:
            return (0, int(m.group(1)))
        return (1, p.name.casefold())

    return sorted(files, key=sort_key)


def write_combined_ocr_txt(
    chart_name: str, kind: str, page_texts: list[tuple[str, str]]
) -> Path:
    """kind: prelim | final1 — page_texts: [(page_filename, text), ...]"""
    out = ocr_dir(chart_name) / f"{chart_name}_{kind}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for page_name, text in page_texts:
        blocks.append(f"===== {page_name} =====\n{(text or '').rstrip()}\n")
    out.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")
    return out


def write_final2_json(
    chart_name: str, pages: list[dict[str, Any]]
) -> Path:
    out = ocr_dir(chart_name) / f"{chart_name}_final2.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "recordId": chart_name,
        "model": "prebuilt-read",
        "pageCount": len(pages),
        "pages": pages,
    }
    out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return out


def parse_combined_ocr_txt(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = PAGE_MARKER_RE.split(text)
    # parts: [preamble, name1, body1, name2, body2, ...]
    result: dict[str, str] = {}
    i = 1
    while i + 1 < len(parts):
        name = parts[i].strip()
        body = parts[i + 1].strip()
        result[name] = body
        i += 2
    return result


def parse_final2_json(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for page in doc.get("pages") or []:
        name = str(page.get("fileName") or f"{page.get('pageNumber')}.jpg")
        out[name] = str(page.get("content") or "")
    return out


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def append_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.is_file() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def imaging_csv(chart_name: str, suffix: str) -> Path:
    return imaging_dir(chart_name) / f"{chart_name}_{suffix}.csv"


def load_ocr_text_for_page(
    chart_name: str,
    page_name: str,
    *,
    prefer: str = "final2",
) -> str:
    """prefer: final2 | final1 | prelim"""
    order = {
        "final2": ["final2", "final1", "prelim"],
        "final1": ["final1", "prelim"],
        "prelim": ["prelim"],
    }.get(prefer, ["final2", "final1", "prelim"])
    for kind in order:
        if kind == "final2":
            texts = parse_final2_json(ocr_dir(chart_name) / f"{chart_name}_final2.json")
        else:
            texts = parse_combined_ocr_txt(
                ocr_dir(chart_name) / f"{chart_name}_{kind}.txt"
            )
        if page_name in texts:
            return texts[page_name]
        # try bare number match
        for k, v in texts.items():
            if Path(k).stem == Path(page_name).stem:
                return v
    return ""


def iso_or_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None
