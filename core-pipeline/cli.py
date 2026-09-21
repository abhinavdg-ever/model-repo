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

    load_dotenv(ROOT / ".env", override=True)
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
        parser_obj.add_argument(
            "--skip-ocr",
            dest="skip_ocr",
            action="store_true",
            default=None,
            help="Skip prelim/final1/final2 when ocr/ has files, else write "
                 "the three ocr/ files from ocr_results in the DB "
                 "(overrides SKIP_OCR=false). Runs OCR if neither exists.",
        )
        parser_obj.add_argument(
            "--no-skip-ocr",
            dest="skip_ocr",
            action="store_false",
            help="Force OCR engines even if SKIP_OCR=true in .env",
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
            help="Replace files already at the write destination "
                 "(default: write missing, skip existing)",
        )

    p_run = sub.add_parser(
        "run",
        help=(
            "One chart: intake+pipeline+optional write, or resume an existing "
            "chart (pass --chart-id / --chart-name with no read path)"
        ),
    )
    src_r = p_run.add_mutually_exclusive_group(required=False)
    src_r.add_argument("--local-read-path", metavar="PATH",
                       help="Directory holding the chart folder")
    src_r.add_argument("--blob-read-path",
                       help="Prefix holding the chart folder (with --blob-container)")
    p_run.add_argument(
        "--folder-name",
        help="The chart folder under the read path. Becomes the chart name.",
    )
    p_run.add_argument(
        "--chart-id", type=int,
        help="Resume an existing chart by id (no read path)",
    )
    p_run.add_argument(
        "--chart-name",
        help="Resume an existing chart by folder name (no read path)",
    )
    p_run.add_argument("--blob-container", help="Required with --blob-read-path")
    p_run.add_argument("--blob-write-path", help="Prefix to write results to")
    p_run.add_argument("--local-write-path", help="Directory to write results to")
    add_write_flags(p_run)
    p_run.add_argument(
        "--resume",
        dest="force",
        action="store_false",
        help="Skip pages already completed (default: reprocess / force=true)",
    )
    p_run.set_defaults(force=True)
    add_stage_flags(p_run)
    p_run.add_argument("--run-id")
    p_run.add_argument("--batch-id")
    p_run.add_argument("--no-pipeline", action="store_true", help="Intake only")

    def _configure_batch(parser_obj) -> None:
        src_b = parser_obj.add_mutually_exclusive_group(required=True)
        src_b.add_argument(
            "--local-read-path", metavar="PATH",
            help="Parent folder; each subfolder holding images is one chart",
        )
        src_b.add_argument(
            "--blob-read-path",
            help="Blob prefix; each sub-folder holding images is one chart",
        )
        parser_obj.add_argument("--blob-container", help="Required with --blob-read-path")
        parser_obj.add_argument("--blob-write-path", help="Prefix to write each chart to")
        parser_obj.add_argument("--local-write-path", help="Directory to write each chart to")
        add_write_flags(parser_obj)
        parser_obj.add_argument(
            "--resume",
            dest="force",
            action="store_false",
            help=(
                "Resume after a partial/timeout batch: skip charts already "
                "complete, keep OCR on disk, only re-run incomplete pages "
                "(default: reprocess / force=true)"
            ),
        )
        parser_obj.set_defaults(force=True)
        add_stage_flags(parser_obj)
        parser_obj.add_argument("--no-pipeline", action="store_true", help="Intake only")
        parser_obj.add_argument("--limit", type=int, help="Only the first N charts (dry runs)")
        parser_obj.add_argument(
            "--workers", type=int, default=None,
            help="Charts to run concurrently (default BATCH_WORKERS, usually 4)",
        )
        parser_obj.add_argument("--run-id")
        parser_obj.add_argument("--batch-id")

    p_batch = sub.add_parser(
        "batch-run",
        help="Every chart under a path: register, run, optionally write",
    )
    _configure_batch(p_batch)
    p_batch_alias = sub.add_parser(
        "batch",
        help="Alias for batch-run",
    )
    _configure_batch(p_batch_alias)
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
        from db import connect, get_chart, get_chart_by_name
        from db.paths import (
            normalize_blob_path,
            normalize_folder_name,
            normalize_fs_path,
        )
        from jobs.export_chart import write_chart
        from orchestrator.runner import ingest_and_run, run_pipeline_for_chart

        args.local_read_path = normalize_fs_path(args.local_read_path)
        args.local_write_path = normalize_fs_path(
            getattr(args, "local_write_path", None)
        )
        args.blob_read_path = normalize_blob_path(
            getattr(args, "blob_read_path", None)
        )
        args.blob_write_path = normalize_blob_path(
            getattr(args, "blob_write_path", None)
        )
        if getattr(args, "folder_name", None):
            args.folder_name = normalize_folder_name(args.folder_name)
        if getattr(args, "chart_name", None):
            args.chart_name = normalize_folder_name(args.chart_name)

        has_source = bool(args.local_read_path or args.blob_read_path)
        resume = bool(args.chart_id or args.chart_name)
        if has_source and resume:
            parser.error("pass a read source, or --chart-id/--chart-name to resume — not both")
        if not has_source and not resume:
            parser.error(
                "provide --local-read-path/--blob-read-path + --folder-name, "
                "or --chart-id / --chart-name to resume"
            )
        if has_source and not args.folder_name:
            parser.error("--folder-name is required with a read path")
        if args.blob_read_path and not args.blob_container:
            parser.error("--blob-read-path requires --blob-container")
        if args.blob_read_path and args.local_write_path:
            parser.error("a blob source writes to --blob-write-path")
        if args.local_read_path and args.blob_write_path:
            parser.error("a local source writes to --local-write-path")
        if args.blob_write_path and args.local_write_path:
            parser.error("give one write destination, not both")

        if resume:
            with connect() as conn:
                if args.chart_id:
                    chart = get_chart(conn, args.chart_id)
                else:
                    chart = get_chart_by_name(conn, args.chart_name)
                if not chart:
                    raise SystemExit("chart not found")
                chart_id = int(chart["id"])
                folder = str(chart["chart_name"])
            result = run_pipeline_for_chart(
                chart_id,
                force=args.force,
                only=args.only,
                through=args.through,
                skip_ocr=args.skip_ocr,
            )
            result = {"chart_id": chart_id, "chart_name": folder, "pipeline": result}
        else:
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
                skip_ocr=args.skip_ocr,
            )
            folder = result.get("chart_name") or folder

        if args.blob_write_path or args.local_write_path:
            result["write"] = write_chart(
                folder,
                local_path=args.local_write_path,
                blob_container=args.blob_container,
                blob_path=args.blob_write_path,
                overwrite=args.overwrite,
                write_mode=args.write_mode,
            )
        print(json.dumps(result, default=str, indent=2))
        return

    if args.cmd in ("batch-run", "batch"):
        from db.paths import normalize_blob_path, normalize_fs_path
        from jobs.batch_intake import run_batch

        args.local_read_path = normalize_fs_path(args.local_read_path)
        args.local_write_path = normalize_fs_path(
            getattr(args, "local_write_path", None)
        )
        args.blob_read_path = normalize_blob_path(
            getattr(args, "blob_read_path", None)
        )
        args.blob_write_path = normalize_blob_path(
            getattr(args, "blob_write_path", None)
        )

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
                    skip_ocr=args.skip_ocr,
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
        from db.paths import normalize_blob_path, normalize_fs_path
        from jobs.manifest_sweeper import run_load

        args.local = normalize_fs_path(args.local)
        args.blob_prefix = normalize_blob_path(
            getattr(args, "blob_prefix", None)
        )

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
