#!/usr/bin/env python3
"""CLI entrypoints for core-pipeline."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Advantmed core-pipeline CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ingest = sub.add_parser("ingest", help="Download chart from blob and run pipeline")
    p_ingest.add_argument("--container", required=True)
    p_ingest.add_argument("--path", required=True, help="Blob path to chart folder")
    p_ingest.add_argument("--run-id")
    p_ingest.add_argument("--batch-id")
    p_ingest.add_argument("--no-pipeline", action="store_true")

    p_local = sub.add_parser("register-local", help="Register local chart folder and run")
    p_local.add_argument("chart_name")
    p_local.add_argument("--run-id")
    p_local.add_argument("--batch-id")
    p_local.add_argument("--force", action="store_true")
    p_local.add_argument("--no-pipeline", action="store_true")

    p_import = sub.add_parser(
        "import-folder",
        help="Import any local folder of images into the workspace and run it",
    )
    p_import.add_argument("path", help="Folder containing .jpg/.png page images")
    p_import.add_argument(
        "--chart-name",
        help="Chart name (default: the source folder's own name)",
    )
    p_import.add_argument(
        "--move",
        action="store_true",
        help="Move the images instead of copying (default: copy, source kept)",
    )
    p_import.add_argument(
        "--recursive",
        action="store_true",
        help="Also pick up images in subfolders",
    )
    p_import.add_argument(
        "--force",
        action="store_true",
        help="Replace pages already in the workspace for this chart",
    )
    p_import.add_argument(
        "--no-manifest",
        action="store_true",
        help="Ignore any CSV/XLSX manifest sitting in the source folder",
    )
    p_import.add_argument("--run-id")
    p_import.add_argument("--batch-id")
    p_import.add_argument("--no-pipeline", action="store_true")

    p_run = sub.add_parser("run", help="Run pipeline for existing chart_id")
    p_run.add_argument("chart_id", type=int)
    p_run.add_argument(
        "--force",
        action="store_true",
        help="Reprocess pages already completed (default: resume, skip them)",
    )
    p_run.add_argument(
        "--only",
        action="append",
        metavar="STAGE",
        help="Run only these stages, e.g. --only member_verify --only blank_junk:2. "
             "Repeatable.",
    )

    p_stages = sub.add_parser("stages", help="List the pipeline stages in order")

    p_status = sub.add_parser("status", help="Show a chart's stage progress")
    p_status.add_argument("chart_id", type=int)

    p_manifest = sub.add_parser(
        "manifest",
        help="Load a batch/group manifest (local file/dir or blob) with upsert",
    )
    src = p_manifest.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--local",
        metavar="PATH",
        help="Local CSV/XLSX file or directory (e.g. metadata_R1_B1.csv)",
    )
    src.add_argument("--blob-prefix", help="Azure blob prefix of batch manifests")
    p_manifest.add_argument(
        "--blob-container", help="Required with --blob-prefix"
    )
    p_manifest.add_argument("--run-id", help="Override run id (default: parse R# from filename)")
    p_manifest.add_argument("--batch-id", help="Override batch id (default: parse B# from filename)")
    p_manifest.add_argument(
        "--no-mirror",
        action="store_true",
        help="Do not mirror blob files into review-ui/data/metadata",
    )

    # Back-compat aliases
    p_manifest_sweep = sub.add_parser(
        "manifest-sweep", help="Alias: blob manifest load"
    )
    p_manifest_sweep.add_argument("--container", required=True)
    p_manifest_sweep.add_argument("--prefix", required=True)

    p_manifest_local = sub.add_parser(
        "manifest-local", help="Alias: local manifest load"
    )
    p_manifest_local.add_argument("file")

    p_api = sub.add_parser("serve", help="Start FastAPI server")

    args = parser.parse_args()

    if args.cmd == "serve":
        from api.main import main as serve_main

        serve_main()
        return

    if args.cmd == "ingest":
        from orchestrator.runner import ingest_and_run

        result = ingest_and_run(
            blob_container=args.container,
            blob_path=args.path,
            run_id=args.run_id,
            batch_id=args.batch_id,
            run_pipeline=not args.no_pipeline,
        )
        print(json.dumps(result, default=str, indent=2))
        return

    if args.cmd == "register-local":
        from stages.download_blob import register_local_pages
        from orchestrator.runner import run_pipeline_for_chart

        reg = register_local_pages(
            args.chart_name, run_id=args.run_id, batch_id=args.batch_id
        )
        out = {"register": reg}
        if not args.no_pipeline:
            out["pipeline"] = run_pipeline_for_chart(reg["chart_id"], force=args.force)
        print(json.dumps(out, default=str, indent=2))
        return

    if args.cmd == "import-folder":
        from stages.download_blob import import_local_folder
        from orchestrator.runner import run_pipeline_for_chart

        reg = import_local_folder(
            args.path,
            chart_name=args.chart_name,
            move=args.move,
            recursive=args.recursive,
            force=args.force,
            load_manifest=not args.no_manifest,
            run_id=args.run_id,
            batch_id=args.batch_id,
        )
        out = {"import": reg}
        if not args.no_pipeline:
            out["pipeline"] = run_pipeline_for_chart(reg["chart_id"])
        print(json.dumps(out, default=str, indent=2))
        return

    if args.cmd == "run":
        from orchestrator.runner import run_pipeline_for_chart

        print(
            json.dumps(
                run_pipeline_for_chart(
                    args.chart_id, force=args.force, only=args.only
                ),
                default=str,
                indent=2,
            )
        )
        return

    if args.cmd == "stages":
        from orchestrator.runner import STAGE_CHAIN

        for index, (name, pass_no, _fn) in enumerate(STAGE_CHAIN, start=1):
            label = name if pass_no == 1 else f"{name}:{pass_no}"
            print(f"{index:2d}. {label}")
        return

    if args.cmd == "status":
        from db import connect
        from db.chart_status import refresh_chart_status

        with connect() as conn:
            print(
                json.dumps(
                    refresh_chart_status(conn, args.chart_id), default=str, indent=2
                )
            )
        return

    if args.cmd == "manifest":
        from jobs.manifest_sweeper import run_load

        if args.blob_prefix and not args.blob_container:
            raise SystemExit("--blob-container is required with --blob-prefix")
        print(
            json.dumps(
                run_load(
                    local_path=args.local,
                    blob_container=args.blob_container,
                    blob_prefix=args.blob_prefix,
                    run_id=args.run_id,
                    batch_id=args.batch_id,
                    mirror_local=not args.no_mirror,
                ),
                default=str,
                indent=2,
            )
        )
        return

    if args.cmd == "manifest-sweep":
        from jobs.manifest_sweeper import run_load

        print(
            json.dumps(
                run_load(
                    blob_container=args.container,
                    blob_prefix=args.prefix,
                ),
                default=str,
                indent=2,
            )
        )
        return

    if args.cmd == "manifest-local":
        from jobs.manifest_sweeper import run_load

        print(
            json.dumps(
                run_load(local_path=Path(args.file)),
                default=str,
                indent=2,
            )
        )
        return


if __name__ == "__main__":
    main()
