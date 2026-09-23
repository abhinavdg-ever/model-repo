"""Load a batch/group manifest into manifest_member_list.

A manifest file (e.g. metadata_R1_B1.csv) is a batch of charts: one row per
chart candidate member. Re-runs upsert — conflicts update the existing row.

Inputs:
  * Local file or directory of .csv / .xlsx
  * Azure blob prefix (all matching files under the prefix)

Conflict key (first match wins):
  1. (record_id, external_member_id) when MemberID present
  2. else (record_id, lower(member_name), member_dob)

record_id is the client's RecordId, which is also chart_list.chart_name and the
chart folder name — so a manifest can be loaded before or after its charts are
ingested, and nothing needs linking afterwards.

Usage:
  python -m jobs.manifest_sweeper --local ../review-ui/data/metadata/metadata_R1_B1.csv
  python -m jobs.manifest_sweeper --local ../review-ui/data/metadata/
  python -m jobs.manifest_sweeper --blob-container imaging-pipeline --blob-prefix Manifests/
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from config import METADATA_ROOT
from db import (
    connect,
    create_job,
    update_job,
    upsert_manifest_members,
)

logger = logging.getLogger(__name__)

MANIFEST_SUFFIXES = (".csv", ".xlsx", ".xls")
# metadata_R1_B1.csv → run_id=R1, batch_id=B1
FILENAME_RUN_BATCH_RE = re.compile(
    r"metadata[_-]?(?P<run>R\d+)[_-]?(?P<batch>B\d+)",
    re.IGNORECASE,
)


def _parse_dob(raw: str) -> Optional[str]:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _name_parts(row: dict[str, str]) -> tuple[str, str, str]:
    """(first, middle, last) from the manifest row.

    The verification rules match first / middle / last independently
    (classify_two_word_name / classify_three_word_name), so the parts must be
    stored separately — a single joined string cannot drive them. When only a
    joined name is supplied it is split as a fallback.
    """

    def pick(*keys: str) -> str:
        for key in keys:
            if row.get(key):
                return str(row[key]).strip()
        return ""

    first = pick("DummyFirstName", "FirstName", "first_name", "First Name")
    middle = pick("DummyMiddleName", "MiddleName", "middle_name", "Middle Name")
    last = pick("DummyLastName", "LastName", "last_name", "Last Name")
    if first or last:
        return first, middle, last

    joined = pick("member_name", "MemberName", "Member Name", "name")
    parts = [p for p in joined.split() if p]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    if len(parts) == 1:
        return parts[0], "", ""
    return "", "", ""


def _compose_name(row: dict[str, str]) -> str:
    for key in ("member_name", "MemberName", "Member Name", "name"):
        if row.get(key):
            return str(row[key]).strip()
    first, middle, last = _name_parts(row)
    return " ".join(p for p in (first, middle, last) if p).strip()


def _record_id(row: dict[str, str]) -> Optional[str]:
    for key in ("recordId", "RecordId", "record_id", "chart_name", "ChartName"):
        if row.get(key):
            return str(row[key]).strip()
    return None


def _member_id(row: dict[str, str]) -> Optional[str]:
    for key in ("MemberID", "member_id", "external_member_id", "MemberId"):
        if row.get(key):
            val = str(row[key]).strip()
            if val:
                return val
    return None


def _dob(row: dict[str, str]) -> Optional[str]:
    for key in ("DummyDOB", "member_dob", "DOB", "dob", "DateOfBirth"):
        if row.get(key):
            return _parse_dob(str(row[key]))
    return None


def parse_run_batch_from_name(name: str) -> tuple[Optional[str], Optional[str]]:
    m = FILENAME_RUN_BATCH_RE.search(Path(name).name)
    if not m:
        return None, None
    return m.group("run").upper(), m.group("batch").upper()


def _parse_csv_bytes(data: bytes) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _parse_xlsx_bytes(data: bytes) -> list[dict[str, str]]:
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError("openpyxl required for xlsx manifests") from exc
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = [str(h).strip() if h is not None else "" for h in next(rows_iter)]
    out: list[dict[str, str]] = []
    for values in rows_iter:
        row = {
            headers[i]: ("" if values[i] is None else str(values[i]))
            for i in range(len(headers))
            if headers[i]
        }
        out.append(row)
    return out


def parse_manifest_bytes(name: str, data: bytes) -> list[dict[str, str]]:
    lower = name.casefold()
    if lower.endswith(".csv"):
        return _parse_csv_bytes(data)
    if lower.endswith(".xlsx") or lower.endswith(".xls"):
        return _parse_xlsx_bytes(data)
    return []


def _row_to_member(row: dict[str, str], *, source_file: str, source_path: str) -> Optional[dict[str, Any]]:
    """Shape a CSV/XLSX row like a ``manifest_member_list`` dict (no ``id``)."""
    rid = _record_id(row)
    name = _compose_name(row)
    if not rid or not name:
        return None
    first, middle, last = _name_parts(row)
    run_id, batch_id = parse_run_batch_from_name(source_file)
    return {
        "id": None,
        "record_id": rid,
        "member_name": name,
        "first_name": first or None,
        "middle_name": middle or None,
        "last_name": last or None,
        "member_dob": _dob(row),
        "external_member_id": _member_id(row),
        "run_id": run_id,
        "batch_id": batch_id,
        "source_file": source_file,
        "source_path": source_path,
    }


def lookup_manifest_members_on_disk(
    record_id: str,
    *,
    metadata_root: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Scan on-disk manifesto CSVs/XLSX for rows whose record_id matches.

    Search order (first root that yields hits wins — we merge all hits from
    every readable root so a split drop still finds the row):

      1. ``metadata_root`` or ``METADATA_ROOT`` (``review-ui/data/metadata``)
      2. ``DATA_ROOT/manifest`` (``review-ui/data/folders/manifest``)
      3. sibling ``…/data/manifest`` next to the folders tree

    Used when Postgres / MemoryStore has no rows yet — same files review-ui
    Local Mode reads for Manifest Details.
    """
    from config import DATA_ROOT

    wanted = (record_id or "").strip()
    if not wanted:
        return []

    roots: list[Path] = []
    primary = (metadata_root or METADATA_ROOT).expanduser()
    roots.append(primary)
    # User drop: folders/manifest next to chart workspaces
    roots.append((DATA_ROOT / "manifest").expanduser())
    roots.append((DATA_ROOT.parent / "manifest").expanduser())
    # Sibling metadata if METADATA_ROOT was overridden away from data/metadata
    sibling_meta = (DATA_ROOT.parent / "metadata").expanduser()
    if sibling_meta.resolve() != primary.resolve():
        roots.append(sibling_meta)

    matches: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen_paths or not resolved.is_dir():
            continue
        seen_paths.add(resolved)
        for path in sorted(p for p in resolved.iterdir() if _is_manifest_file(p)):
            if path.name.startswith("._"):
                continue
            try:
                rows = parse_manifest_bytes(path.name, path.read_bytes())
            except Exception:
                logger.exception("Skipping unreadable manifest %s", path)
                continue
            for row in rows:
                norm = {
                    (k or "").strip(): ("" if v is None else str(v)).strip()
                    for k, v in row.items()
                }
                if _record_id(norm) != wanted:
                    continue
                member = _row_to_member(
                    norm, source_file=path.name, source_path=str(path.resolve())
                )
                if member:
                    matches.append(member)
    return matches


def _is_manifest_file(path: Path) -> bool:
    return path.is_file() and path.suffix.casefold() in MANIFEST_SUFFIXES


def collect_local_files(local_path: Path) -> list[Path]:
    path = local_path.expanduser().resolve()
    if path.is_file():
        if not _is_manifest_file(path):
            raise RuntimeError(f"Not a CSV/XLSX manifest: {path}")
        return [path]
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if _is_manifest_file(p))
        if not files:
            raise RuntimeError(f"No CSV/XLSX files in {path}")
        return files
    raise RuntimeError(f"Path not found: {path}")


def _ingest_rows(
    conn: Any,
    rows: list[dict[str, str]],
    *,
    source_file: str,
    source_path: str,
    run_id: Optional[str],
    batch_id: Optional[str],
) -> dict[str, Any]:
    skipped = 0
    charts: set[str] = set()
    members: list[dict[str, Any]] = []

    for row in rows:
        rid = _record_id(row)
        name = _compose_name(row)
        if not rid or not name:
            skipped += 1
            continue
        first, middle, last = _name_parts(row)

        # record_id IS the chart name, so there is nothing to resolve and no
        # chart row to create. v6 created a placeholder chart_list row per
        # record, which filled the review UI with empty charts for records that
        # were never ingested; a manifest here simply stands on its own until a
        # chart with the same name is ingested.
        charts.add(rid)
        members.append(
            {
                "record_id": rid,
                "member_name": name,
                "first_name": first or None,
                "middle_name": middle or None,
                "last_name": last or None,
                "member_dob": _dob(row),
                "external_member_id": _member_id(row),
                "run_id": run_id,
                "batch_id": batch_id,
                "source_file": source_file,
                "source_path": source_path,
            }
        )

    # One round-trip per 100 rows (MemberID / name+DOB groups separately).
    stats = upsert_manifest_members(conn, members)

    return {
        "inserted": stats["inserted"],
        "updated": stats["updated"],
        "skipped": skipped,
        "records": len(charts),
        "record_ids": sorted(charts),
        "charts": len(charts),
    }


def load_manifest_file(
    conn: Any,
    *,
    name: str,
    data: bytes,
    source_path: str,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    mirror_local: bool = False,
) -> dict[str, Any]:
    file_run, file_batch = parse_run_batch_from_name(name)
    effective_run = run_id or file_run
    effective_batch = batch_id or file_batch
    rows = parse_manifest_bytes(name, data)
    if mirror_local:
        METADATA_ROOT.mkdir(parents=True, exist_ok=True)
        (METADATA_ROOT / Path(name).name).write_bytes(data)
    stats = _ingest_rows(
        conn,
        rows,
        source_file=Path(name).name,
        source_path=source_path,
        run_id=effective_run,
        batch_id=effective_batch,
    )
    return {
        "file": Path(name).name,
        "source_path": source_path,
        "run_id": effective_run,
        "batch_id": effective_batch,
        "rows_read": len(rows),
        **stats,
    }


def run_load(
    *,
    local_path: Optional[str | Path] = None,
    blob_container: Optional[str] = None,
    blob_prefix: Optional[str] = None,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    mirror_local: bool = True,
) -> dict[str, Any]:
    """Load one batch manifest (local file/dir or blob prefix) with upsert semantics."""
    if bool(local_path) == bool(blob_container or blob_prefix):
        # XOR: exactly one mode
        if local_path and (blob_container or blob_prefix):
            raise ValueError("Pass either --local or --blob-*, not both")
        if not local_path and not blob_container:
            raise ValueError("Provide --local PATH or --blob-container + --blob-prefix")

    with connect() as conn:
        job_id = create_job(
            conn, chart_id=None, stage_name="manifest_sweep", status="running"
        )
        update_job(conn, job_id, started=True)

    file_results: list[dict[str, Any]] = []
    errors: list[str] = []
    totals = {"inserted": 0, "updated": 0, "skipped": 0, "charts": 0, "files": 0}

    try:
        if local_path:
            for path in collect_local_files(Path(local_path)):
                try:
                    with connect() as conn:
                        result = load_manifest_file(
                            conn,
                            name=path.name,
                            data=path.read_bytes(),
                            source_path=str(path),
                            run_id=run_id,
                            batch_id=batch_id,
                            mirror_local=False,
                        )
                    file_results.append(result)
                    totals["files"] += 1
                    totals["inserted"] += result["inserted"]
                    totals["updated"] += result["updated"]
                    totals["skipped"] += result["skipped"]
                    totals["charts"] += result["charts"]
                    logger.info(
                        "Loaded %s: +%s insert, ~%s update, %s charts (run=%s batch=%s)",
                        path.name,
                        result["inserted"],
                        result["updated"],
                        result["charts"],
                        result["run_id"],
                        result["batch_id"],
                    )
                except Exception as exc:
                    logger.exception("Failed local manifest %s", path)
                    errors.append(f"{path}: {exc}")
        else:
            from db.blob_store import (
                download_blob_bytes,
                ensure_blob_ready,
                list_blobs_with_suffixes,
            )

            assert blob_container and blob_prefix
            ensure_blob_ready(blob_container)
            blob_names = list_blobs_with_suffixes(
                blob_container, blob_prefix, MANIFEST_SUFFIXES
            )
            if not blob_names:
                raise RuntimeError(
                    f"No manifest files under {blob_container}/{blob_prefix}"
                )
            for blob_name in blob_names:
                try:
                    data = download_blob_bytes(blob_container, blob_name)
                    with connect() as conn:
                        result = load_manifest_file(
                            conn,
                            name=Path(blob_name).name,
                            data=data,
                            source_path=blob_name,
                            run_id=run_id,
                            batch_id=batch_id,
                            mirror_local=mirror_local,
                        )
                    file_results.append(result)
                    totals["files"] += 1
                    totals["inserted"] += result["inserted"]
                    totals["updated"] += result["updated"]
                    totals["skipped"] += result["skipped"]
                    totals["charts"] += result["charts"]
                    logger.info(
                        "Loaded blob %s: +%s insert, ~%s update, %s charts",
                        blob_name,
                        result["inserted"],
                        result["updated"],
                        result["charts"],
                    )
                except Exception as exc:
                    logger.exception("Failed blob manifest %s", blob_name)
                    errors.append(f"{blob_name}: {exc}")

        with connect() as conn:
            update_job(
                conn,
                job_id,
                status="failed" if errors and totals["files"] == 0 else "completed",
                error_message="; ".join(errors) if errors else None,
                completed=True,
            )

        return {
            "job_id": job_id,
            "mode": "local" if local_path else "blob",
            **totals,
            "members_upserted": totals["inserted"] + totals["updated"],
            "files_detail": file_results,
            "errors": errors,
        }
    except Exception as exc:
        with connect() as conn:
            update_job(
                conn, job_id, status="failed", error_message=str(exc), completed=True
            )
        raise


# Back-compat aliases used by API / older CLI
def run_sweep(
    *,
    blob_container: str,
    blob_prefix: str,
    mirror_local: bool = True,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> dict[str, Any]:
    return run_load(
        blob_container=blob_container,
        blob_prefix=blob_prefix,
        mirror_local=mirror_local,
        run_id=run_id,
        batch_id=batch_id,
    )


def run_sweep_local(
    csv_or_xlsx: Path,
    *,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> dict[str, Any]:
    return run_load(local_path=csv_or_xlsx, run_id=run_id, batch_id=batch_id)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(
        description=(
            "Load a batch/group manifest into Postgres. "
            "Re-runs update existing members on conflict."
        )
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--local",
        metavar="PATH",
        help="Local CSV/XLSX file or directory of batch manifests",
    )
    src.add_argument(
        "--blob-prefix",
        metavar="PREFIX",
        help="Azure blob prefix containing batch manifest files",
    )
    parser.add_argument(
        "--blob-container",
        help="Azure container (required with --blob-prefix)",
    )
    parser.add_argument("--run-id", help="Override run id (else parse from filename)")
    parser.add_argument("--batch-id", help="Override batch id (else parse from filename)")
    parser.add_argument(
        "--no-mirror",
        action="store_true",
        help="Do not copy blob files into review-ui/data/metadata",
    )
    args = parser.parse_args(argv)

    if args.blob_prefix and not args.blob_container:
        parser.error("--blob-container is required with --blob-prefix")

    result = run_load(
        local_path=args.local,
        blob_container=args.blob_container,
        blob_prefix=args.blob_prefix,
        run_id=args.run_id,
        batch_id=args.batch_id,
        mirror_local=not args.no_mirror,
    )
    print(json.dumps(result, default=str, indent=2))
    return 1 if result.get("errors") and result.get("files", 0) == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
