#!/usr/bin/env python3
"""Upload a local folder of charts (one subfolder per chart) to Azure Blob.

    <folder>/<chart_name>/*.jpg  ->  <container>/<prefix>/<chart_name>/<file>

Uses the Blob settings in core-pipeline/.env (AZURE_STORAGE_*), the same sign-in as the
pipeline. Images and PDFs only; AppleDouble (._*) and other files are skipped. A blob that
already exists with the same size is skipped, so a re-run only sends what is missing.

    # macOS / Ubuntu VM
    cd core-pipeline && source .venv/bin/activate
    python ../utilities/upload_charts_to_blob.py "/Volumes/Extreme SSD/Advantmed/Training/Test Charts" \
        --prefix raw_input/Batch1/Run1 --dry-run

    # Windows (PowerShell)
    cd core-pipeline; .venv\\Scripts\\Activate.ps1
    python ..\\utilities\\upload_charts_to_blob.py "D:\\Test Charts" --prefix raw_input/Batch1/Run1 --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
sys.path.insert(0, str(CORE))

SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf"}


def chart_files(root: Path, only: set[str] | None) -> dict[str, list[Path]]:
    charts: dict[str, list[Path]] = {}
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        if only and folder.name not in only:
            continue
        files = sorted(f for f in folder.rglob("*")
                       if f.is_file() and not f.name.startswith("._") and f.suffix.lower() in SUFFIXES)
        if files:
            charts[folder.name] = files
    return charts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, help="local folder with one subfolder per chart")
    ap.add_argument("--prefix", required=True, help="blob folder above the chart folders, e.g. raw_input/Batch1/Run1")
    ap.add_argument("--container", help="default: AZURE_STORAGE_CONTAINER from core-pipeline/.env")
    ap.add_argument("--chart", action="append", help="only these chart folders (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be uploaded, touch nothing")
    args = ap.parse_args(argv)

    charts = chart_files(args.folder, set(args.chart or []) or None)
    if not charts:
        print(f"no chart folders with images under {args.folder}")
        return 1
    prefix = args.prefix.strip("/")
    total = sum(len(v) for v in charts.values())
    print(f"{len(charts)} charts, {total} files -> {prefix}/<chart_name>/")
    if args.dry_run:
        for name, files in charts.items():
            print(f"  {prefix}/{name}/  {len(files)} files")
        return 0

    from dotenv import load_dotenv
    load_dotenv(CORE / ".env")
    from common.db.blob_store import get_container_client

    container = get_container_client(args.container)
    sent = skipped = 0
    for name, files in charts.items():
        base = args.folder / name
        for f in files:
            blob = container.get_blob_client(f"{prefix}/{name}/{f.relative_to(base).as_posix()}")
            size = f.stat().st_size
            try:
                if blob.get_blob_properties().size == size:
                    skipped += 1
                    continue
            except Exception:  # not there yet
                pass
            with f.open("rb") as fh:
                blob.upload_blob(fh, overwrite=True, length=size)
            sent += 1
        print(f"  {name}: done")
    print(f"uploaded {sent}, already there {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
