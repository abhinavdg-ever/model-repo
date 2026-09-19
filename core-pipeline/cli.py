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

    def add_stage_flags(parser_obj) -> None:
        """`--through` and `--only`, spelled the same everywhere they appear."""
        parser_obj.add_argument(
            "--through",
            metavar="STAGE",
            help="Run the chain and stop after this stage, e.g. --through ocr_final2",
        )
        parser_obj.add_argument(
            "--only",
            action="append",
            metavar="STAGE",
            help="Run only this stage, whatever ran before. Repeatable, "
                 "e.g. --only member_verify --only blank_junk:2",
        )

    def add_write_flags(parser_obj) -> None:
        """`--all-files` / `--skip-orig-pages`, spelled the same everywhere."""
        mode = parser_obj.add_mutually_exclusive_group()
        mode.add_argument(
            "--skip-orig-pages",
            dest="write_mode",
            action="store_const",
            const="skip_orig_pages",
            help="Write corrected-pages/, ocr/, imaging/ but NOT pages/ (default)",
        )
        mode.add_argument(
            "--all-files",
            dest="write_mode",
            action="store_const",
            const="all_files",
            help="Write everything, including the original pages/",
        )
        parser_obj.set_defaults(write_mode="skip_orig_pages")
        parser_obj.add_argument(
            "--overwrite",
            action="store_true",
            help="Replace files already at the write destination",
        )

    p_run = sub.add_parser(
        "run",
        help="Read one chart (blob or local), run the chain, optionally write it out",
    )
    src_r = p_run.add_mutually_exclusive_group(required=True)
    src_r.add_argument("--local-read-path", metavar="PATH",
                       help="Directory holding the chart folder")
    src_r.add_argument("--blob-read-path",
                       help="Prefix holding the chart folder (with --blob-container)")
    p_run.add_argument("--folder-name", required=True,
                       help="The chart folder under the read path. Becomes the chart name.")
    p_run.add_argument("--blob-container", help="Required with --blob-read-path")
    p_run.add_argument("--blob-write-path", help="Prefix to write results to")
    p_run.add_argument("--local-write-path", help="Directory to write results to")
    add_write_flags(p_run)
    p_run.add_argument(
        "--force", action="store_true", help="Reprocess pages already completed"
    )
    add_stage_flags(p_run)
    p_run.add_argument("--run-id")
    p_run.add_argument("--batch-id")
    p_run.add_argument("--no-pipeline", action="store_true", help="Intake only")

    p_batch = sub.add_parser(
        "batch",
        help="Scan a folder or blob prefix: register all charts, then run with workers",
    )
    src_b = p_batch.add_mutually_exclusive_group(required=True)
    src_b.add_argument(
        "--local-read-path", metavar="PATH",
        help="Parent folder; each subfolder holding images is one chart",
    )
    src_b.add_argument(
        "--blob-read-path",
        help="Blob prefix; each sub-folder holding images is one chart",
    )
    p_batch.add_argument("--blob-container", help="Required with --blob-read-path")
    p_batch.add_argument("--blob-write-path", help="Prefix to write each chart to")
    p_batch.add_argument("--local-write-path", help="Directory to write each chart to")
    add_write_flags(p_batch)
    p_batch.add_argument("--force", action="store_true", help="Reprocess pages already done")
    add_stage_flags(p_batch)
    p_batch.add_argument("--no-pipeline", action="store_true", help="Intake only")
    p_batch.add_argument("--limit", type=int, help="Only the first N charts (dry runs)")
    p_batch.add_argument(
        "--workers", type=int, default=None,
        help="Charts to run concurrently (default BATCH_WORKERS, usually 4)",
    )
    p_batch.add_argument("--run-id")
    p_batch.add_argument("--batch-id")

    p_write = sub.add_parser(
        "write",
        help="Write an already-run chart out, without reprocessing it",
    )
    p_write.add_argument("chart_name", help="Folder name under data/folders")
    src_w = p_write.add_mutually_exclusive_group(required=True)
    src_w.add_argument("--local-write-path", metavar="PATH", help="Destination directory")
    src_w.add_argument("--blob-write-path",
                       help="Destination prefix (with --blob-container)")
    p_write.add_argument("--blob-container", help="Required with --blob-write-path")
    add_write_flags(p_write)

    p_rerun = sub.add_parser("rerun", help="Re-run the chain for an existing chart_id")
    p_rerun.add_argument("chart_id", type=int)
    p_rerun.add_argument(
        "--force",
        action="store_true",
        help="Reprocess pages already completed (default: resume, skip them)",
    )
    add_stage_flags(p_rerun)

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

    # The CLI runs the same stages in-process, so it wants the same logging:
    # our progress lines visible, the Azure SDK request/response dump not.
    # `serve` is excluded because api.main configures logging itself.
    if args.cmd != "serve":
        import logging

        from logging_setup import configure_logging

        configure_logging(logging.INFO)

    if args.cmd == "serve":
        from api.main import main as serve_main

        serve_main()
        return

    if args.cmd == "run":
        from orchestrator.runner import ingest_and_run

        if args.blob_read_path and not args.blob_container:
            parser.error("--blob-read-path requires --blob-container")
        if args.blob_read_path and args.local_write_path:
            parser.error("a blob source writes to --blob-write-path")
        if args.local_read_path and args.blob_write_path:
            parser.error("a local source writes to --local-write-path")

        folder = args.folder_name
        if args.local_read_path:
            source = str(Path(args.local_read_path) / folder)
            blob_path = None
        else:
            source = None
            blob_path = f"{args.blob_read_path.strip('/')}/{folder}"

        result = ingest_and_run(
            local_path=source,
            blob_container=args.blob_container,
            blob_path=blob_path,
            chart_name=folder,
            run_id=args.run_id,
            batch_id=args.batch_id,
            run_pipeline=not args.no_pipeline,
            force=args.force,
            only=args.only,
            through=args.through,
        )
        if args.blob_write_path or args.local_write_path:
            from jobs.export_chart import write_chart

            result["write"] = write_chart(
                result.get("chart_name") or folder,
                local_path=args.local_write_path,
                blob_container=args.blob_container,
                blob_path=args.blob_write_path,
                overwrite=args.overwrite,
                write_mode=args.write_mode,
            )
        print(json.dumps(result, default=str, indent=2))
        return

    if args.cmd == "batch":
        from jobs.batch_intake import run_batch

        if args.blob_read_path and not args.blob_container:
            parser.error("--blob-read-path requires --blob-container")
        print(
            json.dumps(
                run_batch(
                    local_read_path=args.local_read_path,
                    blob_container=args.blob_container,
                    blob_read_path=args.blob_read_path,
                    local_write_path=args.local_write_path,
                    blob_write_path=args.blob_write_path,
                    write_mode=args.write_mode,
                    overwrite=args.overwrite,
                    force=args.force,
                    run_pipeline=not args.no_pipeline,
                    only=args.only,
                    through=args.through,
                    limit=args.limit,
                    run_id=args.run_id,
                    batch_id=args.batch_id,
                    workers=args.workers,
                ),
                default=str,
                indent=2,
            )
        )
        return

    if args.cmd == "write":
        from jobs.export_chart import write_chart

        if args.blob_write_path and not args.blob_container:
            parser.error("--blob-write-path requires --blob-container")
        print(
            json.dumps(
                write_chart(
                    args.chart_name,
                    local_path=args.local_write_path,
                    blob_container=args.blob_container,
                    blob_path=args.blob_write_path,
                    overwrite=args.overwrite,
                    write_mode=args.write_mode,
                ),
                default=str,
                indent=2,
            )
        )
        return

    if args.cmd == "rerun":
        from orchestrator.runner import run_pipeline_for_chart

        print(
            json.dumps(
                run_pipeline_for_chart(
                    args.chart_id,
                    force=args.force,
                    only=args.only,
                    through=args.through,
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
