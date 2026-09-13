from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "azure_blob"))

import azure_blob_storage as blob_store
import config as azure_config
from azure_read_ocr import AzureReadOcrExtractor

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_queue() -> tuple[list[dict], int, list[str]]:
    """Tally every record folder and page count under the raw prefix.

    Records whose OCR JSON already has every raw page are marked done so they
    are not sent to Azure again (even if progress was built after OCR finished).
    """
    folders = blob_store.list_raw_record_ids()
    if not folders:
        raise SystemExit(
            f"No record folders under {blob_store.AZURE_STORAGE_CONTAINER}/"
            f"{blob_store.require_raw_prefix()}"
        )
    queue: list[dict] = []
    completed: list[str] = []
    total_pages = 0
    already_done = 0
    for index, record_id in enumerate(folders, start=1):
        pages = blob_store.list_raw_page_blobs(record_id)
        page_count = len(pages)
        total_pages += page_count
        raw_names = {name for name, _blob in pages}
        existing = blob_store.load_ocr_document(record_id)
        done_names: set[str] = set()
        if isinstance(existing, dict):
            for page in existing.get("pages") or []:
                name = str(page.get("fileName") or "").strip()
                if name:
                    done_names.add(name)
        is_complete = bool(raw_names) and raw_names <= done_names
        if not raw_names:
            is_complete = True
        status = "done" if is_complete else "pending"
        if is_complete:
            already_done += 1
            completed.append(record_id)
        queue.append(
            {
                "recordId": record_id,
                "pageCount": page_count,
                "status": status,
                "pages_done": sorted(done_names & raw_names) if raw_names else [],
                "error": None,
            }
        )
        if index % 25 == 0 or index == len(folders):
            logger.info(
                "tally progress %s/%s folders (pages so far=%s, already_done=%s)",
                index,
                len(folders),
                total_pages,
                already_done,
            )
    return queue, total_pages, completed


def new_progress(queue: list[dict], total_pages: int, completed: list[str] | None = None) -> dict:
    return {
        "raw_blob_container": blob_store.AZURE_STORAGE_CONTAINER,
        "raw_blob_prefix": blob_store.require_raw_prefix(),
        "ocr_blob_container": blob_store.AZURE_STORAGE_CONTAINER,
        "ocr_blob_prefix": blob_store.require_write_prefix(),
        "folder_count": len(queue),
        "page_count_total": total_pages,
        "queue": queue,
        "completed": list(completed or []),
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "status": "running",
    }


def load_progress() -> dict:
    path = azure_config.PROGRESS_FILE
    if not path.is_file():
        logger.info("building OCR queue from raw prefix...")
        queue, total_pages, completed = build_queue()
        progress = new_progress(queue, total_pages, completed)
        write_json(path, progress)
        logger.info(
            "first run: folders=%s pages=%s already_done=%s under %s/%s",
            progress["folder_count"],
            progress["page_count_total"],
            len(completed),
            blob_store.AZURE_STORAGE_CONTAINER,
            blob_store.require_raw_prefix(),
        )
        return progress

    progress = read_json(path)
    same_paths = (
        str(progress.get("raw_blob_container") or "") == blob_store.AZURE_STORAGE_CONTAINER
        and str(progress.get("raw_blob_prefix") or "") == blob_store.require_raw_prefix()
        and str(progress.get("ocr_blob_container") or "") == blob_store.AZURE_STORAGE_CONTAINER
        and str(progress.get("ocr_blob_prefix") or "") == blob_store.require_write_prefix()
    )
    if not same_paths or not isinstance(progress.get("queue"), list):
        logger.info("paths changed or queue missing: rebuilding tally...")
        queue, total_pages, completed = build_queue()
        progress = new_progress(queue, total_pages, completed)
        write_json(path, progress)
        logger.info(
            "new queue: folders=%s pages=%s already_done=%s",
            progress["folder_count"],
            progress["page_count_total"],
            len(completed),
        )
        return progress

    for item in progress["queue"]:
        if str(item.get("status") or "") == "running":
            item["status"] = "pending"
    progress["status"] = "running"
    progress["updated_at"] = utc_now()
    write_json(path, progress)
    done = sum(1 for item in progress["queue"] if item.get("status") == "done")
    logger.info(
        "resume: %s/%s folders done, total_pages=%s",
        done,
        progress.get("folder_count"),
        progress.get("page_count_total"),
    )
    return progress


def save_progress(progress: dict) -> None:
    progress["updated_at"] = utc_now()
    write_json(azure_config.PROGRESS_FILE, progress)


def _queue_item(progress: dict, record_id: str) -> dict | None:
    for item in progress.get("queue") or []:
        if item.get("recordId") == record_id:
            return item
    return None


def _set_item_status(progress: dict, record_id: str, status: str, **extra) -> None:
    item = _queue_item(progress, record_id)
    if item is None:
        return
    item["status"] = status
    for key, value in extra.items():
        item[key] = value
    if status == "done":
        completed = list(progress.get("completed") or [])
        if record_id not in completed:
            completed.append(record_id)
        progress["completed"] = completed
    save_progress(progress)


def _mark_page_done(progress: dict, record_id: str, file_name: str) -> None:
    item = _queue_item(progress, record_id)
    if item is None:
        return
    done = list(item.get("pages_done") or [])
    if file_name not in done:
        done.append(file_name)
    item["pages_done"] = sorted(done)
    save_progress(progress)


def pending_records(progress: dict) -> list[str]:
    return [
        str(item.get("recordId") or "")
        for item in progress.get("queue") or []
        if item.get("status") in {"pending", "error"} and item.get("recordId")
    ]


def load_document(record_id: str) -> dict:
    data = blob_store.load_ocr_document(record_id)
    if isinstance(data, dict) and isinstance(data.get("pages"), list):
        return data
    return {
        "recordId": record_id,
        "model": "prebuilt-read",
        "pageCount": 0,
        "pages": [],
    }


def save_document(record_id: str, document: dict) -> None:
    blob_name = blob_store.save_ocr_document(record_id, document)
    logger.info("saved OCR JSON -> %s/%s", blob_store.AZURE_STORAGE_CONTAINER, blob_name)


def done_file_names(document: dict) -> set[str]:
    names: set[str] = set()
    for page in document.get("pages") or []:
        name = str(page.get("fileName") or "").strip()
        if name:
            names.add(name)
    return names


def ocr_record(
    record_id: str,
    folder_no: int,
    folder_count: int,
    progress: dict,
    extractor: AzureReadOcrExtractor,
) -> None:
    _set_item_status(progress, record_id, "running", error=None)
    logger.info("start record %s (%s/%s)", record_id, folder_no, folder_count)

    try:
        pages = blob_store.list_raw_page_blobs(record_id)
        if not pages:
            logger.info("skip %s (no page images)", record_id)
            _set_item_status(progress, record_id, "done", pages_done=[])
            return

        document = load_document(record_id)
        already = done_file_names(document)
        item = _queue_item(progress, record_id)
        if item is not None:
            item["pages_done"] = sorted(already)
            item["pageCount"] = len(pages)
            save_progress(progress)

        pending = [
            (page_number, file_name, blob_name)
            for page_number, (file_name, blob_name) in enumerate(pages, start=1)
            if file_name not in already
        ]
        if not pending:
            _set_item_status(progress, record_id, "done", pages_done=sorted(already))
            logger.info("finished record %s (already complete)", record_id)
            return

        for page_number, file_name, blob_name in pending:
            logger.info(
                "ocr page %s/%s folder %s/%s record=%s file=%s",
                page_number,
                len(pages),
                folder_no,
                folder_count,
                record_id,
                file_name,
            )
            image_bytes = blob_store.download_bytes(blob_name)
            page = extractor.extract_page_bytes(image_bytes, file_name, page_number)
            document["pages"] = [item for item in document["pages"] if item.get("fileName") != file_name]
            document["pages"].append(page)
            # save_ocr_document sorts by fileName ascending and renumbers pageNumber
            save_document(record_id, document)
            _mark_page_done(progress, record_id, file_name)

        _set_item_status(progress, record_id, "done")
        logger.info("finished record %s (%s/%s)", record_id, folder_no, folder_count)
    except Exception as exc:
        logger.exception("record %s failed: %s", record_id, exc)
        _set_item_status(progress, record_id, "error", error=str(exc))
        raise


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("azure.identity").setLevel(logging.WARNING)

    extractor = AzureReadOcrExtractor()
    if not extractor.available:
        raise SystemExit("Azure Document Intelligence is not configured. Set endpoint and key in the repo-root .env")
    if not blob_store.storage_configured():
        raise SystemExit("Azure Blob Storage is not configured. Set AZURE_STORAGE_* in the repo-root .env")
    try:
        raw_prefix = blob_store.require_raw_prefix()
        write_prefix = blob_store.require_write_prefix()
    except RuntimeError as err:
        raise SystemExit(str(err)) from err

    try:
        blob_store.ensure_blob_ready()
    except Exception as err:
        raise SystemExit(f"Azure Blob auth failed: {err}") from err

    logger.info(
        "raw images <- %s/%s/{record}/...",
        blob_store.AZURE_STORAGE_CONTAINER,
        raw_prefix,
    )
    logger.info(
        "OCR JSON output -> %s/%s/{record}/{record}_final2.json",
        blob_store.AZURE_STORAGE_CONTAINER,
        write_prefix,
    )
    logger.info("mode=sequential (one record, one page at a time)")

    progress = load_progress()
    queue = list(progress.get("queue") or [])
    folder_count = int(progress.get("folder_count") or len(queue))
    remaining = pending_records(progress)
    if not remaining:
        progress["status"] = "done"
        save_progress(progress)
        logger.info("all %s folders already complete", folder_count)
        return 0

    index_by_id = {
        str(item.get("recordId") or ""): index
        for index, item in enumerate(queue, start=1)
    }
    logger.info(
        "queue remaining=%s folders=%s pages_total=%s",
        len(remaining),
        folder_count,
        progress.get("page_count_total"),
    )

    try:
        for record_id in remaining:
            ocr_record(
                record_id,
                index_by_id.get(record_id, 0),
                folder_count,
                progress,
                extractor,
            )
    except KeyboardInterrupt:
        progress["status"] = "stopped"
        for item in progress.get("queue") or []:
            if item.get("status") == "running":
                item["status"] = "pending"
        save_progress(progress)
        logger.info("stopped; queue saved for resume")
        return 130
    except Exception:
        progress["status"] = "error"
        save_progress(progress)
        raise

    progress["status"] = "done"
    save_progress(progress)
    logger.info("done: %s/%s folders", folder_count, folder_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
