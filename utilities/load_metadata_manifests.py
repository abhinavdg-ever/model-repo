#!/usr/bin/env python3
"""Load every ``metadata_Rn_Bn`` CSV/XLSX under a folder into Postgres.

Writes rows into ``manifest_member_list`` (upsert). Run id / batch id come from
the filename (``metadata_R1_B1.csv`` → R1 / B1) unless you override them.

Examples::

    # All metadata_R*_B* files in the default metadata folder
    python utilities/load_metadata_manifests.py

    python utilities/load_metadata_manifests.py /path/to/metadata/
    python utilities/load_metadata_manifests.py /path/to/metadata_R2_B3.csv

Requires the **core-pipeline** virtualenv and ``DATABASE_URL`` (env or
``core-pipeline/.env``)::

    cd core-pipeline && source .venv/bin/activate
    python ../utilities/load_metadata_manifests.py
    python ../utilities/load_metadata_manifests.py /path/to/metadata/
    python ../utilities/load_metadata_manifests.py /path/to/metadata_R2_B3.csv
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

try:
    from dotenv import load_dotenv

    load_dotenv(CORE / ".env")
except ImportError:
    pass

from jobs.manifest_sweeper import (  # noqa: E402
    FILENAME_RUN_BATCH_RE,
    MANIFEST_SUFFIXES,
    run_load,
)

logger = logging.getLogger(__name__)

DEFAULT_FOLDER = REPO / "review-ui" / "data" / "metadata"


def is_metadata_rn_bn(path: Path) -> bool:
    if path.suffix.casefold() not in MANIFEST_SUFFIXES:
        return False
    if path.name.startswith("._"):
        return False
    return bool(FILENAME_RUN_BATCH_RE.search(path.name))


def collect_metadata_files(path: Path) -> list[Path]:
    path = path.expanduser().resolve()
    if path.is_file():
        if not is_metadata_rn_bn(path):
            raise SystemExit(
                f"Not a metadata_Rn_Bn CSV/XLSX: {path.name}\n"
                f"Expected a name like metadata_R1_B1.csv"
            )
        return [path]
    if not path.is_dir():
        raise SystemExit(f"Path not found: {path}")

    files = sorted(p for p in path.iterdir() if is_metadata_rn_bn(p))
    if not files:
        raise SystemExit(
            f"No metadata_Rn_Bn.csv/.xlsx files in {path}\n"
            f"Looked for names matching metadata_R# _B# (e.g. metadata_R1_B1.csv)"
        )
    return files


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description=(
            "Read metadata_Rn_Bn.csv/.xlsx from a folder and upsert into "
            "manifest_member_list."
        )
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=str(DEFAULT_FOLDER),
        help=f"File or folder (default: {DEFAULT_FOLDER})",
    )
    parser.add_argument("--run-id", help="Override run id for every file")
    parser.add_argument("--batch-id", help="Override batch id for every file")
    args = parser.parse_args(argv)

    files = collect_metadata_files(Path(args.path))
    logger.info("Found %d metadata file(s)", len(files))
    for f in files:
        logger.info("  %s", f.name)

    # Point run_load at the parent dir when we have several files, or at the
    # single file. It already walks a directory for CSV/XLSX; we pre-filter so
    # only metadata_Rn_Bn names are present in that folder listing by loading
    # each file individually when mixed content might exist.
    totals = {"files": 0, "inserted": 0, "updated": 0, "skipped": 0, "charts": 0}
    errors: list[str] = []
    for path in files:
        try:
            result = run_load(
                local_path=path,
                run_id=args.run_id,
                batch_id=args.batch_id,
            )
            totals["files"] += result.get("files", 0)
            totals["inserted"] += result.get("inserted", 0)
            totals["updated"] += result.get("updated", 0)
            totals["skipped"] += result.get("skipped", 0)
            totals["charts"] += result.get("charts", 0)
            for err in result.get("errors") or []:
                errors.append(err)
        except Exception as exc:
            logger.exception("Failed %s", path.name)
            errors.append(f"{path}: {exc}")

    logger.info(
        "Done: %d file(s), +%d insert, ~%d update, %d skipped, %d chart id(s)",
        totals["files"],
        totals["inserted"],
        totals["updated"],
        totals["skipped"],
        totals["charts"],
    )
    if errors:
        for err in errors:
            logger.error("%s", err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
