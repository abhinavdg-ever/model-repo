#!/usr/bin/env python3
"""Turn local PDFs into one folder per file, with a JPEG per page.

Each PDF becomes a chart folder the File Viewer can open:

    <out>/<pdf name>/pages/1.jpg
    <out>/<pdf name>/pages/2.jpg

Pass local paths, or a text file with one path per line. A directory is
every PDF inside it.

    python ../utilities/pdfs_to_folders.py /path/to/chart-a.pdf /path/to/chart-b.pdf
    python ../utilities/pdfs_to_folders.py --list paths.txt
    python ../utilities/pdfs_to_folders.py /path/to/pdfs/

Run from ``core-pipeline`` with that venv active. Pages land in
``review-ui/data/folders/<pdf name>/pages/``.

Needs PyMuPDF: pip install pymupdf
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_BAD_NAME = re.compile(r"[\\/:*?\"<>|\s]+")


def folder_name_for(path: Path) -> str:
    """Chart folder name from the PDF filename."""
    cleaned = _BAD_NAME.sub("_", path.stem).strip("._") or "pdf"
    return cleaned[:120]


def read_sources(paths: list[str], list_file: Path | None) -> list[Path]:
    raw = list(paths)
    if list_file is not None:
        for line in list_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw.append(line)
    if not raw:
        raise SystemExit("give at least one local PDF, a folder of PDFs, or --list paths.txt")

    found: list[Path] = []
    for item in raw:
        path = Path(item).expanduser()
        if path.is_dir():
            found.extend(sorted(path.glob("*.pdf")))
            found.extend(sorted(path.glob("*.PDF")))
            continue
        found.append(path)
    # De-dupe while keeping order. glob of both cases can repeat on macOS.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        key = path.resolve() if path.exists() else path
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def render_pages(pdf_path: Path, pages_dir: Path, dpi: int) -> int:
    import pymupdf

    pages_dir.mkdir(parents=True, exist_ok=True)
    zoom = dpi / 72
    matrix = pymupdf.Matrix(zoom, zoom)
    with pymupdf.open(pdf_path) as doc:
        for index, page in enumerate(doc, start=1):
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            pixmap.save(str(pages_dir / f"{index}.jpg"))
        return doc.page_count


def unique_folder(parent: Path, name: str) -> Path:
    candidate = parent / name
    if not candidate.exists():
        return candidate
    n = 2
    while (parent / f"{name}_{n}").exists():
        n += 1
    return parent / f"{name}_{n}"


def place_one(pdf_path: Path, out_dir: Path, dpi: int) -> Path:
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"not a PDF: {pdf_path}")
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    folder = unique_folder(out_dir, folder_name_for(pdf_path))
    count = render_pages(pdf_path, folder / "pages", dpi)
    print(f"{folder.name}: {count} page(s)")
    return folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="*", help="Local PDF files or a folder of them")
    parser.add_argument("--list", type=Path, help="Text file, one local PDF path per line")
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO / "review-ui" / "data" / "folders",
        help="Parent folder. One child folder per PDF.",
    )
    parser.add_argument("--dpi", type=int, default=150)
    args = parser.parse_args(argv)

    try:
        import pymupdf  # noqa: F401
    except ImportError:
        raise SystemExit("PyMuPDF is required: pip install pymupdf")

    args.out.mkdir(parents=True, exist_ok=True)
    failed = 0
    for pdf_path in read_sources(args.pdfs, args.list):
        try:
            place_one(pdf_path, args.out, args.dpi)
        except Exception as exc:
            failed += 1
            print(f"{pdf_path}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
