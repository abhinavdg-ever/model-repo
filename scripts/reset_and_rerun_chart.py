#!/usr/bin/env python3
"""Wipe one chart's DB results + workspace, then re-run the pipeline.

Destructive for that chart only (other charts untouched). Keeps the
``chart_list`` row so blob/local source metadata survives; clears stage
outputs, page_list (rebuilt on ingest), and on-disk pages/ocr/imaging.

Usage (from repo root, with core-pipeline/.env loaded):

  # Wipe only — no run
  python scripts/reset_and_rerun_chart.py --chart-name 52758770_47616472 --wipe-only

  # Wipe + re-download from blob (chart row must have blob_container/blob_path)
  python scripts/reset_and_rerun_chart.py --chart-name 52758770_47616472 --yes

  # Local source: parent folder that contains <chart_name>/
  python scripts/reset_and_rerun_chart.py --chart-name 52758770_47616472 \
    --local-read-path /data/inbox --yes

  # Same via chart id
  python scripts/reset_and_rerun_chart.py --chart-id 112

  # Stop after a stage
  python scripts/reset_and_rerun_chart.py --chart-name 52758770_47616472 --through ocr_final1

Requires DATABASE_URL (and blob credentials if the chart was ingested from blob).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

try:
    from dotenv import load_dotenv

    load_dotenv(CORE / ".env", override=True)
except ImportError:
    pass


def _resolve_chart(conn, *, chart_id: int | None, chart_name: str | None) -> dict:
    from db import get_chart, get_chart_by_name

    if chart_id is not None:
        row = get_chart(conn, int(chart_id))
        if not row:
            raise SystemExit(f"chart_id={chart_id} not found")
        return dict(row)
    name = (chart_name or "").strip()
    if not name:
        raise SystemExit("Provide --chart-id or --chart-name")
    row = get_chart_by_name(conn, name)
    if not row:
        raise SystemExit(f"chart_name={name!r} not found")
    return dict(row)


def wipe_chart(conn, chart: dict) -> dict:
    """Clear stage tables, page_list, and workspace files for one chart."""
    from db import reset_chart_results
    from db.paths import clear_chart_workspace

    chart_id = int(chart["id"])
    chart_name = str(chart["chart_name"])
    deleted = reset_chart_results(conn, chart_id)
    # Drop pages so ingest re-registers them cleanly (ids may change).
    page_result = conn.execute(
        "DELETE FROM page_list WHERE chart_id = %s", (chart_id,)
    )
    pages_deleted = getattr(page_result, "rowcount", 0) or 0
    if pages_deleted:
        deleted["page_list"] = pages_deleted
    cleared = clear_chart_workspace(chart_name)
    return {"db_deleted": deleted, "workspace_cleared": cleared}


def rerun_chart(
    chart: dict,
    *,
    through: str | None,
    only: list[str] | None,
    local_read_path: str | None,
) -> dict:
    """Re-ingest from blob (stored on chart) or a local parent folder + run."""
    from pathlib import Path

    from orchestrator.runner import ingest_and_run

    chart_name = str(chart["chart_name"])
    source = (chart.get("source") or "").strip().lower()
    blob_container = chart.get("blob_container")
    blob_path = chart.get("blob_path")

    kwargs: dict = {
        "chart_name": chart_name,
        "force": True,
        "redownload_pages": True,
        "run_pipeline": True,
        "through": through,
        "only": only,
        "run_id": chart.get("run_id"),
        "batch_id": chart.get("batch_id"),
    }

    if local_read_path:
        folder = Path(local_read_path).expanduser().resolve() / chart_name
        if not folder.is_dir():
            raise SystemExit(f"Local chart folder not found: {folder}")
        kwargs["local_path"] = str(folder)
        return ingest_and_run(**kwargs)

    if source == "blob" or (blob_container and blob_path):
        if not blob_container or not blob_path:
            raise SystemExit(
                f"Chart {chart_name} has no blob_container/blob_path to re-download from"
            )
        kwargs["blob_container"] = blob_container
        kwargs["blob_path"] = blob_path
        return ingest_and_run(**kwargs)

    raise SystemExit(
        f"Chart {chart_name} is not a blob source (source={source!r}). "
        "Pass --local-read-path <parent-of-chart-folder>."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wipe one chart's results/workspace and optionally re-run it."
    )
    parser.add_argument("--chart-id", type=int, default=None)
    parser.add_argument("--chart-name", type=str, default=None)
    parser.add_argument(
        "--wipe-only",
        action="store_true",
        help="Delete DB results + workspace only; do not re-run",
    )
    parser.add_argument(
        "--local-read-path",
        metavar="DIR",
        help="Parent directory that contains <chart_name>/ (local re-ingest)",
    )
    parser.add_argument(
        "--through",
        metavar="STAGE",
        help="Stop after this stage (same as cli.py run --through)",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="STAGE",
        help="Run only this stage (repeatable)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    args = parser.parse_args()

    from db import connect

    with connect() as conn:
        chart = _resolve_chart(conn, chart_id=args.chart_id, chart_name=args.chart_name)
        chart_id = int(chart["id"])
        chart_name = str(chart["chart_name"])
        print(
            f"Chart id={chart_id} name={chart_name} "
            f"source={chart.get('source')} "
            f"blob={chart.get('blob_container')}/{chart.get('blob_path')}"
        )
        if not args.yes:
            reply = input(
                "This deletes all stage results, pages, and workspace files "
                f"for {chart_name}. Continue? [y/N] "
            ).strip().lower()
            if reply not in {"y", "yes"}:
                print("Aborted.")
                raise SystemExit(1)

        wipe_info = wipe_chart(conn, chart)
        print(json.dumps({"wipe": wipe_info}, indent=2, default=str))

    if args.wipe_only:
        print("Wipe done (--wipe-only).")
        return

    print("Re-running pipeline…")
    result = rerun_chart(
        chart,
        through=args.through,
        only=args.only,
        local_read_path=args.local_read_path,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
