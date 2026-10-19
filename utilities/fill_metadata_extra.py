#!/usr/bin/env python3
"""Add the client's extra columns to every batch metadata CSV, then write them to Postgres.

The client sends one dump across all charts: ``metadata_extra.csv`` (recordId plus
MemberID, ClientID, ProjectID, SourceName, RR Method, Measure, ServiceNote). Put it in the metadata
folder beside the batch files (``metadata_R1_B1.csv`` …). This script matches each batch
row to the dump by recordId, adds the missing columns to the batch file (a blank in the
dump keeps the file's value; names and DOB are never touched), and with ``--load``
upserts the batch files into ``manifest_member_list``. The dump itself is never loaded
as a batch.

Examples::

    cd core-pipeline && source .venv/bin/activate

    # What would change, nothing written
    python ../utilities/fill_metadata_extra.py --dry-run

    # Fill the CSVs in review-ui/data/metadata, then the database
    python ../utilities/fill_metadata_extra.py --load

    # Another folder, or a dump with another name / an .xlsx
    python ../utilities/fill_metadata_extra.py /path/to/metadata --load
    python ../utilities/fill_metadata_extra.py /path/to/metadata --source dump.xlsx --load

Same job over HTTP (core-pipeline API)::

    curl -X POST localhost:8001/api/manifest/metadata-extra \\
      -H 'Content-Type: application/json' -d '{"metadata_dir": "/path/to/metadata", "load": true}'

Requires the core-pipeline virtualenv; ``--load`` also needs ``DATABASE_URL`` (env or
``core-pipeline/.env``) and, on an existing database, ``schema/changes_2026-10-19.sql``.
"""
from __future__ import annotations

import argparse
import json
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

from jobs.manifest_sweeper import EXTRA_FILE_NAME  # noqa: E402
from jobs.metadata_expand import run  # noqa: E402

DEFAULT_FOLDER = REPO / "review-ui" / "data" / "metadata"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "metadata_dir", nargs="?", type=Path, default=DEFAULT_FOLDER,
        help=f"Folder of batch metadata CSVs (default {DEFAULT_FOLDER})",
    )
    parser.add_argument(
        "--source", type=Path, default=None,
        help=f"The dump (.csv / .xlsx); default <metadata_dir>/{EXTRA_FILE_NAME}",
    )
    parser.add_argument("--new-file", help="Write dump records no batch file has to this CSV in the folder")
    parser.add_argument("--load", action="store_true", help="Then write the batch files to manifest_member_list")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing")
    args = parser.parse_args(argv)

    folder = args.metadata_dir.expanduser().resolve()
    if not folder.is_dir():
        parser.error(f"not a folder: {folder}")
    try:
        result = run(
            args.source.expanduser() if args.source else None,
            metadata_dir=folder,
            new_file=args.new_file,
            load=args.load,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
