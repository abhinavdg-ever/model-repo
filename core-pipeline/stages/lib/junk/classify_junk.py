#!/usr/bin/env python3
"""
Junk / blank page classification for imaging folders → CSV.

Uses existing preliminary OCR only — does **not** re-OCR.

Input per chart: ``ocr/<chart>_prelim.txt`` (page sections ``===== 1.jpg =====``).

On each page's prelim text:
  1) empty / &lt; 5 alphanumeric chars → Blank
  2) short text with “blank” / “intentionally blank” → Blank
  3) invoice keywords → Invoice
  4) cover / fax keywords → Cover
  5) duplicate fingerprint vs earlier page in same chart → Duplicate
  else → Main

Optional ``--image``: also treat near-white page images as Blank (usually unnecessary
when prelim OCR already exists).

Usage:
  cd 02-imaging-pipeline/junk-classification
  python classify_junk.py
  python classify_junk.py --out ./output/junk_classification.csv
  python classify_junk.py --image   # optional pixel blank check
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

MONOREPO_ROOT = SCRIPT_DIR.parents[1]
IMAGING_UI_CANDIDATES = (
    MONOREPO_ROOT / "05-imaging-ui",
    SCRIPT_DIR.parent.parent / "05-imaging-ui",
)

from blank import is_likely_blank_image  # noqa: E402
from classify import (  # noqa: E402
    CLASSIFICATION_LABELS,
    CODE_BLANK,
    CODE_DUPLICATE,
    CODE_MAIN,
    JUNK_CODES,
    classification_confidence,
    classify_text,
    fingerprint,
)

UI_PAGE_MARKER_RE = re.compile(r"^=====\s*(.+?)\s*=====\s*$", re.MULTILINE)
IMAGE_RE = re.compile(r"\.(jpe?g|png|webp|tif{1,2})$", re.IGNORECASE)
PAGE_NUM_RE = re.compile(r"^(\d+)\.(jpe?g|png|webp|tif{1,2})$", re.IGNORECASE)

CSV_COLUMNS = [
    "chart_name",
    "page_name",
    "page_number",
    "page_classification",
    "page_classification_confidence",
    "page_group",
    "reason",
    "ocr_source",
]


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def bootstrap_env() -> Path | None:
    ui: Path | None = None
    for cand in IMAGING_UI_CANDIDATES:
        if (cand / ".env").is_file() or (cand / "data" / "folders").is_dir():
            ui = cand.resolve()
            break
    if ui:
        _load_env_file(ui / ".env")
    _load_env_file(SCRIPT_DIR / ".env")
    return ui


def default_data_root(ui: Path | None) -> Path:
    env = (os.environ.get("DATA_ROOT") or "").strip()
    if env:
        path = Path(env)
        if not path.is_absolute() and ui is not None:
            return (ui / path).resolve()
        return path.resolve()
    if ui is not None:
        return (ui / "data" / "folders").resolve()
    return (MONOREPO_ROOT / "05-imaging-ui" / "data" / "folders").resolve()


def find_ocr_file(folder: Path) -> tuple[Path | None, str | None]:
    """Junk/blank classification uses preliminary OCR only."""
    ocr_dir = folder / "ocr"
    name = folder.name
    prelim = ocr_dir / f"{name}_prelim.txt"
    if prelim.is_file() and prelim.stat().st_size > 0:
        return prelim, "prelim"
    if ocr_dir.is_dir():
        for p in sorted(ocr_dir.glob("*_prelim.txt")):
            if p.is_file() and p.stat().st_size > 0:
                return p, "prelim"
    return None, None


def split_ocr_pages(text: str) -> list[tuple[str, str]]:
    """Return [(page_name, page_text), ...] from ===== page ===== markers."""
    matches = list(UI_PAGE_MARKER_RE.finditer(text or ""))
    if not matches:
        return [("1.jpg", (text or "").strip())] if (text or "").strip() else []

    pages: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        pages.append((name, body))
    return pages


def list_page_images(folder: Path) -> dict[str, Path]:
    """Map filename / stem / page number → image path under pages/."""
    pages_dir = folder / "pages"
    out: dict[str, Path] = {}
    if not pages_dir.is_dir():
        return out
    for path in sorted(pages_dir.iterdir()):
        if not path.is_file() or path.name.startswith("._"):
            continue
        if not IMAGE_RE.search(path.name):
            continue
        out[path.name.lower()] = path
        out[path.stem.lower()] = path
        m = PAGE_NUM_RE.match(path.name)
        if m:
            out[m.group(1)] = path
            out[f"#{m.group(1)}"] = path
    return out


def resolve_image(page_name: str, images: dict[str, Path]) -> Path | None:
    key = page_name.strip().lower()
    if key in images:
        return images[key]
    stem = Path(page_name).stem.lower()
    if stem in images:
        return images[stem]
    if stem.isdigit() and stem in images:
        return images[stem]
    return None


def page_number_from_name(page_name: str) -> str:
    stem = Path(page_name).stem
    if stem.isdigit():
        return stem
    m = re.match(r"page[_\s-]?(\d+)", stem, re.IGNORECASE)
    if m:
        return m.group(1)
    return ""


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def classify_folder(
    folder: Path,
    *,
    use_image: bool,
) -> tuple[list[dict[str, str]], str | None]:
    ocr_path, source = find_ocr_file(folder)
    if not ocr_path or source is None:
        return [], None

    text = ocr_path.read_text(encoding="utf-8", errors="replace")
    pages = split_ocr_pages(text)
    images = list_page_images(folder) if use_image else {}

    # First pass: blank / invoice / cover (and image blank)
    pending: list[dict] = []
    for page_name, page_text in pages:
        blank_via_image = False
        code = CODE_MAIN
        reason = ""

        if use_image:
            img_path = resolve_image(page_name, images)
            if img_path is not None:
                try:
                    from PIL import Image

                    with Image.open(img_path) as im:
                        if is_likely_blank_image(im):
                            code = CODE_BLANK
                            reason = "blank_image"
                            blank_via_image = True
                except OSError:
                    pass

        if code == CODE_MAIN:
            code, reason = classify_text(page_text)

        pending.append(
            {
                "page_name": page_name,
                "page_text": page_text,
                "code": code,
                "reason": reason,
                "blank_via_image": blank_via_image,
            }
        )

    # Second pass: duplicates in page order (first occurrence stays Main)
    seen_fp: set[str] = set()
    rows: list[dict[str, str]] = []
    for item in pending:
        code = int(item["code"])
        reason = str(item["reason"])
        if code == CODE_MAIN:
            fp = fingerprint(str(item["page_text"]))
            if fp is not None:
                if fp in seen_fp:
                    code = CODE_DUPLICATE
                    reason = "duplicate"
                else:
                    seen_fp.add(fp)

        label = CLASSIFICATION_LABELS.get(code, "Main")
        conf = classification_confidence(
            code, blank_via_image=bool(item["blank_via_image"] and code == CODE_BLANK)
        )
        # Duplicate is not junk — content may be valid; flag separately.
        if code in JUNK_CODES:
            page_group = "junk"
        elif code == CODE_DUPLICATE:
            page_group = "duplicate"
        else:
            page_group = "main"
        rows.append(
            {
                "chart_name": folder.name,
                "page_name": str(item["page_name"]),
                "page_number": page_number_from_name(str(item["page_name"])),
                "page_classification": label,
                "page_classification_confidence": (
                    f"{conf:.2f}" if conf is not None else ""
                ),
                "page_group": page_group,
                "reason": reason,
                "ocr_source": source,
            }
        )
    return rows, source


def main() -> None:
    ui = bootstrap_env()

    parser = argparse.ArgumentParser(
        description="Classify blank / invoice / cover / duplicate junk pages → CSV"
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=default_data_root(ui),
        help="Path to data/folders",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=SCRIPT_DIR / "output" / "junk_classification.csv",
        help="Combined CSV path",
    )
    parser.add_argument(
        "--per-chart",
        action="store_true",
        help="Also write imaging/<chart>_junk.csv under each folder",
    )
    parser.add_argument(
        "--image",
        action="store_true",
        help="Also run pixel blank check on pages/*.jpg (default: prelim text only)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N folders (0 = all)",
    )
    args = parser.parse_args()

    data_root = args.data_root.resolve()
    if not data_root.is_dir():
        print(f"DATA_ROOT not found: {data_root}", file=sys.stderr)
        sys.exit(1)

    folders = sorted(
        p for p in data_root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    if args.limit and args.limit > 0:
        folders = folders[: args.limit]

    use_image = bool(args.image)
    print(f"data_root: {data_root}")
    print(f"folders: {len(folders)}")
    print(f"input: ocr/*_prelim.txt only")
    print(f"image blank check: {'ON (--image)' if use_image else 'OFF (prelim text only)'}")
    print(f"out: {args.out.resolve()}")

    all_rows: list[dict[str, str]] = []
    for folder in folders:
        rows, source = classify_folder(folder, use_image=use_image)
        if source is None:
            print(f"  skip {folder.name}: no prelim OCR")
            continue
        all_rows.extend(rows)
        junk_n = sum(1 for r in rows if r["page_group"] == "junk")
        dup_n = sum(1 for r in rows if r["page_group"] == "duplicate")
        if args.per_chart:
            chart_csv = folder / "imaging" / f"{folder.name}_junk.csv"
            write_csv(chart_csv, rows)
            print(
                f"  {folder.name}: source={source} pages={len(rows)} "
                f"junk={junk_n} duplicate={dup_n} → {chart_csv.name}"
            )
        else:
            print(
                f"  {folder.name}: source={source} pages={len(rows)} "
                f"junk={junk_n} duplicate={dup_n}"
            )

    write_csv(args.out.resolve(), all_rows)
    junk_total = sum(1 for r in all_rows if r["page_group"] == "junk")
    dup_total = sum(1 for r in all_rows if r["page_group"] == "duplicate")
    print(
        f"Wrote {len(all_rows)} row(s) "
        f"(junk={junk_total} duplicate={dup_total}) → {args.out.resolve()}"
    )
    print(f"Done — {len(folders)} folder(s) scanned.")


if __name__ == "__main__":
    main()
