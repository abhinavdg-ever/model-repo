"""OCR labeled page scans into family-training rows.

HEIC scans are converted to JPEG first. Tesseract reads the JPEG (or the
original JPEG/PNG/TIFF). Blank and junk rows are skipped. Each output row has
``page_id``, ``chart_id``, ``page_index``, ``raw_text``, ``page_family``, and
``page_type``. Page type stays on the row for a later keyword file; this step
does not train it.

    python training/page-classification/data-prep/scripts/prepare_training_text.py
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

DATA = Path(__file__).resolve().parents[1]
DEFAULT_CSV = DATA / "training_page_classification.csv"
DEFAULT_IMAGES = Path("/Users/abhinavdasgupta/Desktop/Training/processed")
DEFAULT_JPEG = DEFAULT_IMAGES.parent / "ocr-jpeg"
DEFAULT_OCR = DATA / "ocr"
DEFAULT_OUTPUT = DATA / "training_text.jsonl"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic"}
OCR_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
REJECT_TYPES = {"blank", "junk"}
_RANK = {ext: i for i, ext in enumerate([".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".heic"])}


def is_blank_or_junk(row: dict[str, str]) -> bool:
    return (row.get("page_type") or "").strip().casefold() in REJECT_TYPES


def index_scans(root: Path) -> dict[str, Path]:
    """One scan per page id. A JPEG wins over a HEIC of the same stem."""
    found: dict[str, Path] = {}
    for path in root.iterdir():
        if not path.is_file() or path.name.startswith("._"):
            continue
        if path.suffix.lower() not in IMAGE_EXTS:
            continue
        current = found.get(path.stem)
        if current is None or _RANK.get(path.suffix.lower(), 99) < _RANK.get(current.suffix.lower(), 99):
            found[path.stem] = path
    return found


def jpeg_for_ocr(scan: Path, jpeg_dir: Path) -> Path:
    """Return a path Tesseract can read. HEIC is written to ``jpeg_dir``."""
    if scan.suffix.lower() in OCR_EXTS:
        return scan
    if scan.suffix.lower() != ".heic":
        raise RuntimeError(f"No OCR format for {scan.name}")
    dest = jpeg_dir / f"{scan.stem}.jpg"
    if dest.is_file() and dest.stat().st_mtime >= scan.stat().st_mtime and dest.stat().st_size > 0:
        return dest
    tool = shutil.which("heif-convert")
    if tool is None:
        raise RuntimeError("heif-convert is not on PATH")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{scan.stem}.tmp.jpg")
    subprocess.run([tool, "-q", "85", str(scan), str(tmp)], check=True, capture_output=True)
    tmp.replace(dest)
    return dest


def tesseract_cmd() -> str:
    configured = (os.environ.get("TESSERACT_CMD") or "").strip()
    if configured:
        return configured
    found = shutil.which("tesseract")
    if not found:
        raise SystemExit("tesseract is not on PATH. Set TESSERACT_CMD to the binary.")
    return found


def ocr_image(image: Path, binary: str, lang: str) -> str:
    result = subprocess.run(
        [binary, str(image), "stdout", "-l", lang],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"tesseract failed on {image.name}")
    return result.stdout.replace("\x0c", "").rstrip("\n")


def _ocr_job(image: str, dest: str, binary: str, lang: str) -> tuple[str, str, str]:
    target = Path(dest)
    source = Path(image)
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime and target.stat().st_size > 0:
        return dest, target.read_text(encoding="utf-8"), ""
    try:
        text = ocr_image(source, binary, lang)
    except RuntimeError as exc:
        return dest, "", str(exc)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return dest, text, ""


def load_labels(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit(f"{path} has no header row")
        return [
            {key: (value or "").strip() for key, value in row.items() if key is not None}
            for row in reader
            if (row.get("page_id") or "").strip() and not is_blank_or_junk(row)
        ]


def prepare(args: argparse.Namespace) -> int:
    images = Path(args.images)
    if not images.is_dir():
        raise SystemExit(f"Image folder not found: {images}")
    labels = load_labels(Path(args.csv))
    scans = index_scans(images)
    jpeg_dir = Path(args.jpeg_dir)
    ocr_dir = Path(args.ocr_dir)
    ocr_dir.mkdir(parents=True, exist_ok=True)
    binary = tesseract_cmd()

    jobs: list[tuple[str, str, str, str]] = []
    pending: list[dict[str, str]] = []
    problems: list[str] = []
    for row in labels:
        page_id = row["page_id"]
        scan = scans.get(page_id)
        if scan is None:
            problems.append(f"no image for {page_id}")
            continue
        try:
            image = jpeg_for_ocr(scan, jpeg_dir)
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            problems.append(f"convert failed {page_id}: {exc}")
            continue
        dest = ocr_dir / f"{page_id}.txt"
        jobs.append((str(image), str(dest), binary, args.lang))
        pending.append(row)

    texts: dict[str, str] = {}
    workers = max(1, args.workers)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_ocr_job, *job) for job in jobs]
        done = 0
        for future in as_completed(futures):
            dest, text, error = future.result()
            done += 1
            page_id = Path(dest).stem
            if error:
                problems.append(f"ocr failed {page_id}: {error}")
            else:
                texts[page_id] = text
            if done % 25 == 0 or done == len(futures):
                print(f"ocr {done}/{len(futures)}", flush=True)

    records = []
    for row in pending:
        text = texts.get(row["page_id"])
        if text is None:
            continue
        records.append({
            "page_id": row["page_id"],
            "chart_id": row.get("chart_id", ""),
            "page_index": row.get("page_index", ""),
            "raw_text": text,
            "page_family": row.get("page_family", ""),
            "page_type": row.get("page_type", ""),
        })

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    csv_path = output.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "page_id", "chart_id", "page_index", "page_family", "page_type", "raw_text",
        ])
        writer.writeheader()
        writer.writerows(records)
    print(f"wrote {len(records)} rows -> {output}")
    print(f"wrote {len(records)} rows -> {csv_path}")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OCR labeled pages into family-training rows.")
    parser.add_argument("--images", type=Path, default=DEFAULT_IMAGES)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--jpeg-dir", type=Path, default=DEFAULT_JPEG)
    parser.add_argument("--ocr-dir", type=Path, default=DEFAULT_OCR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--lang", default="eng")
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    sys.exit(prepare(parse_args(argv)))


if __name__ == "__main__":
    main()
