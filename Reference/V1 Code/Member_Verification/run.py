from __future__ import annotations

import csv
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "azure_blob"))

import config as mv_config

sys.path.insert(0, str(HERE / "Rules"))

from build_member_csvs import build as build_member_csvs

from extractors.ner_based.dob import extract_dob_ner
from extractors.ner_based.log import COLUMNS as _NER_LOG_COLUMNS
from extractors.ner_based.log import reset as reset_ner_log
from extractors.ner_based.log import rows as ner_log_rows
from extractors.ner_based.member_id import extract_member_id_ner
from extractors.ner_based.model import (
    ModelLoadError,
    by_id,
    ensure_loadable,
    missing_weights,
    use_model,
)
from extractors.ner_based.name import name_candidates, pick_name
from extractors.rule_based.dob import extract_dob
from extractors.rule_based.member_id import extract_member_id
from extractors.rule_based.name_2_words import extract_name_2_words
from extractors.rule_based.name_3_words import extract_name_3_words
from base_rules import is_present
from name_2_words_rules import verify_two_word_name
from name_3_words_rules import verify_three_word_name
from wrong_member_rules import wrong_member_on_page
from what_if_rules import (
    PAGE_NOT_VERIFIED,
    PAGE_VERIFIED,
    PAGE_WRONG_MEMBER,
    apply_what_if,
    count_wrong_member,
    page_status,
    reject_threshold,
)
import azure_blob_storage as blob_store

COLUMNS = [
    "RecordId",
    "Total_Page_Count",
    "Page_No",
    "Detection_Source_Name",
    "ner_key_source_Name",
    "Detected_Full_Name",
    "Detection_Source_DOB",
    "ner_key_source_DOB",
    "Detected_DOB",
    "Detection_Source_MemberID",
    "ner_key_source_MemberID",
    "Detected_MemberID",
    "Page_Verified",
    "Document_Verified",
    "Page_Detection_Correct",
    "Page_Detection_InCorrect",
]

# The batch NER CSV merges every record, so each row is stamped with where it
# came from before the log's own columns.
NER_COLUMNS = ["RecordId", "Page_No", *_NER_LOG_COLUMNS]

MEMBER_KIND = "member"
NER_KIND = "ner"

# Retries for a blob read whose connection was closed while idle.
BLOB_READ_ATTEMPTS = 3
BLOB_READ_RETRY_SECONDS = 3


def setup_logging() -> None:
    """INFO for our own logs, quiet for the SDKs.

    Without this the Azure SDK logs every HTTP request and response header at
    INFO, which buries the run and makes a slow blob read look like a hang.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for name in ("azure", "azure.identity", "azure.core.pipeline.policies.http_logging_policy", "msal", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- queue / progress --------------------------------------------------------


def queue_identity(mode: str, model_ids: list[str], container: str, prefix: str) -> dict:
    """Settings the queue is tied to; a change here rebuilds the tally."""
    return {
        "ocr_blob_container": container,
        "ocr_blob_prefix": prefix,
        "system_input": str(mv_config.System_Input_path),
        "output_path": str(mv_config.Output_path),
        "models": list(model_ids),
        "mode": mode,
        "max_pages": int(mv_config.MAX_PAGES) if mode == "selected" else None,
    }


def build_queue(record_ids: list[str]) -> list[dict]:
    """Tally every record folder under the OCR prefix that is in system input.

    Page counts are not fetched here: that would download every OCR JSON twice,
    so each item gets its pageCount filled in when the record is processed.
    """
    return [
        {
            "recordId": record_id,
            "pageCount": None,
            "status": "pending",
            "models_done": [],
            "error": None,
        }
        for record_id in record_ids
    ]


def new_progress(
    record_ids: list[str],
    mode: str,
    model_ids: list[str],
    container: str,
    prefix: str,
) -> dict:
    queue = build_queue(record_ids)
    return {
        **queue_identity(mode, model_ids, container, prefix),
        "record_count": len(queue),
        "queue": queue,
        "completed": [],
        "skipped": [],
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "status": "running",
    }


def load_progress(
    record_ids: list[str],
    mode: str,
    model_ids: list[str],
    container: str,
    prefix: str,
) -> dict:
    path = mv_config.PROGRESS_FILE
    identity = queue_identity(mode, model_ids, container, prefix)

    if not path.is_file():
        logger.info("building verification queue from %s/%s ...", container, prefix)
        progress = new_progress(record_ids, mode, model_ids, container, prefix)
        write_json(path, progress)
        logger.info("first run: records=%s mode=%s", progress["record_count"], mode)
        return progress

    progress = read_json(path)
    same_settings = isinstance(progress.get("queue"), list) and all(
        progress.get(key) == value for key, value in identity.items()
    )
    if not same_settings:
        logger.info("settings changed or queue missing: rebuilding tally...")
        clear_staging()
        progress = new_progress(record_ids, mode, model_ids, container, prefix)
        write_json(path, progress)
        logger.info("new queue: records=%s mode=%s", progress["record_count"], mode)
        return progress

    known = {str(item.get("recordId") or "") for item in progress["queue"]}
    added = [record_id for record_id in record_ids if record_id not in known]
    if added:
        progress["queue"].extend(build_queue(added))
        progress["record_count"] = len(progress["queue"])
        logger.info("queue grew by %s new record(s)", len(added))

    for item in progress["queue"]:
        if str(item.get("status") or "") == "running":
            item["status"] = "pending"
    progress["status"] = "running"
    save_progress(progress)
    done = sum(1 for item in progress["queue"] if item.get("status") == "done")
    logger.info("resume: %s/%s records done", done, progress.get("record_count"))
    return progress


def save_progress(progress: dict) -> None:
    progress["updated_at"] = utc_now()
    write_json(mv_config.PROGRESS_FILE, progress)


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
    if status in {"done", "skipped"}:
        key = "completed" if status == "done" else "skipped"
        bucket = list(progress.get(key) or [])
        if record_id not in bucket:
            bucket.append(record_id)
        progress[key] = bucket
    save_progress(progress)


def _mark_model_done(progress: dict, record_id: str, model_id: str) -> None:
    item = _queue_item(progress, record_id)
    if item is None:
        return
    done = list(item.get("models_done") or [])
    if model_id not in done:
        done.append(model_id)
    item["models_done"] = done
    save_progress(progress)


def _models_done(progress: dict, record_id: str) -> set[str]:
    item = _queue_item(progress, record_id)
    if item is None:
        return set()
    return {str(model_id) for model_id in (item.get("models_done") or [])}


def pending_records(progress: dict) -> list[str]:
    return [
        str(item.get("recordId") or "")
        for item in progress.get("queue") or []
        if item.get("status") in {"pending", "error", "running"} and item.get("recordId")
    ]


def queue_counts(progress: dict) -> dict[str, int]:
    tally: dict[str, int] = {}
    for item in progress.get("queue") or []:
        status = str(item.get("status") or "pending")
        tally[status] = tally.get(status, 0) + 1
    return tally


# --- batch CSV staging -------------------------------------------------------


def staging_path(kind: str, model_id: str) -> Path:
    return mv_config.STAGING_DIR / f"{kind}_{model_id}.csv"


def clear_staging() -> None:
    if mv_config.STAGING_DIR.is_dir():
        shutil.rmtree(mv_config.STAGING_DIR, ignore_errors=True)


def append_rows(kind: str, model_id: str, columns: list[str], rows: list[dict]) -> None:
    """Append one record's rows to the staged batch CSV for this model.

    Called once per record and model even when there is nothing to add, so the
    file is created with its header regardless: a batch folder always carries
    the same set of CSVs, and an empty NER file reads as "no hits" rather than
    as a missing file.
    """
    path = staging_path(kind, model_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.is_file() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def staged_files() -> list[Path]:
    if not mv_config.STAGING_DIR.is_dir():
        return []
    return sorted(path for path in mv_config.STAGING_DIR.glob("*.csv") if path.is_file())


def finalize_batch(stamp: str | None = None) -> Path | None:
    """Move staged CSVs into one batch folder named after the end timestamp."""
    files = staged_files()
    if not files:
        logger.info("nothing staged: no batch folder written")
        return None
    stamp = stamp or datetime.now().strftime(mv_config.BATCH_STAMP_FORMAT)
    batch_dir = mv_config.batch_output_dir(stamp)
    batch_dir.mkdir(parents=True, exist_ok=True)
    for path in files:
        kind, _, model_id = path.stem.partition("_")
        name = (
            mv_config.ner_csv_name(model_id)
            if kind == NER_KIND
            else mv_config.mv_csv_name(model_id)
        )
        target = batch_dir / name
        shutil.move(str(path), str(target))
        logger.info("wrote %s", target)
    clear_staging()
    return batch_dir


def build_member_csv_outputs(batch_dir: Path, model_ids: list[str]) -> None:
    """Build the three derived member CSVs inside a completed batch folder."""
    if len(model_ids) != 1:
        raise SystemExit(
            "build_member_csvs integration requires exactly one enabled NER model; "
            f"enabled models: {', '.join(model_ids) or 'none'}"
        )
    model_id = model_ids[0]
    verification_source = batch_dir / mv_config.mv_csv_name(model_id)
    ner_source = batch_dir / mv_config.ner_csv_name(model_id)
    written = build_member_csvs(
        mv_config.System_Input_path,
        verification_source,
        ner_source,
        batch_dir,
    )
    for path in written.values():
        logger.info("wrote member CSV: %s", path)


# --- verification ------------------------------------------------------------


def page_text(page: dict) -> str:
    return str(page.get("content") or "")


def detect_name_mode(fieldnames: list[str] | None) -> str:
    fields = {name.strip() for name in (fieldnames or []) if name}
    if {"DummyFirstName", "DummyMiddleName", "DummyLastName"} <= fields:
        return "3"
    if {"DummyFirstName", "DummyLastName"} <= fields:
        return "2"
    return ""


def load_system_input(path: Path) -> tuple[dict[str, dict[str, str]], str]:
    rows: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        name_mode = detect_name_mode(reader.fieldnames)
        for row in reader:
            record_id = (row.get("RecordId") or "").strip()
            if not record_id:
                continue
            rows[record_id] = {
                "DummyFirstName": (row.get("DummyFirstName") or "").strip(),
                "DummyMiddleName": (row.get("DummyMiddleName") or "").strip(),
                "DummyLastName": (row.get("DummyLastName") or "").strip(),
                "DummyDOB": (row.get("DummyDOB") or "").strip(),
                "MemberID": (row.get("MemberID") or "").strip(),
            }
    return rows, name_mode


def extract_full_name_rule(ocr_text: str, expected: dict[str, str], name_mode: str) -> str:
    if name_mode == "3":
        return extract_name_3_words(
            ocr_text,
            expected["DummyFirstName"],
            expected["DummyMiddleName"],
            expected["DummyLastName"],
        )
    if name_mode == "2":
        return extract_name_2_words(
            ocr_text,
            expected["DummyFirstName"],
            expected["DummyLastName"],
        )
    return "N/A"


def extract_page_fields(
    ocr_text: str,
    expected: dict[str, str],
    name_mode: str,
    model_id: str,
) -> tuple[dict[str, str], list[tuple[float, str, str]] | None]:
    """Return (page fields, NER name candidates or None if NER never ran).

    Every field is looked for with the rules over the whole page first. Only
    where the rules find nothing does the page go to the NER layer, which
    builds a sentence around the matching key and reads that. So a page whose
    details the rules already matched costs no model time at all.
    """
    people: list[tuple[float, str, str]] | None = None

    name = extract_full_name_rule(ocr_text, expected, name_mode)
    if is_present(name):
        name_source, name_key = "rule based", ""
    else:
        # The rules found no name on this page, so escalate to NER: build the
        # sentence around each patient-name key and read it with the model.
        people = name_candidates(ocr_text, model_id)
        name, name_key = pick_name(
            people,
            expected["DummyFirstName"],
            expected["DummyLastName"],
            expected["DummyMiddleName"],
            name_mode,
        )
        name_source = "ner" if is_present(name) else ""
        if not is_present(name):
            name_key = ""

    dob = extract_dob(ocr_text, expected["DummyDOB"])
    if is_present(dob):
        dob_source, dob_key = "rule based", ""
    else:
        dob, dob_key = extract_dob_ner(ocr_text, expected["DummyDOB"], model_id)
        dob_source = "ner" if is_present(dob) else ""
        if not is_present(dob):
            dob_key = ""

    member_id = extract_member_id(ocr_text, expected["MemberID"])
    if is_present(member_id):
        id_source, id_key = "rule based", ""
    else:
        member_id, id_key = extract_member_id_ner(ocr_text, expected["MemberID"], model_id)
        id_source = "ner" if is_present(member_id) else ""
        if not is_present(member_id):
            id_key = ""

    fields = {
        "Detected_Full_Name": name,
        "Detection_Source_Name": name_source,
        "ner_key_source_Name": name_key,
        "Detected_DOB": dob,
        "Detection_Source_DOB": dob_source,
        "ner_key_source_DOB": dob_key,
        "Detected_MemberID": member_id,
        "Detection_Source_MemberID": id_source,
        "ner_key_source_MemberID": id_key,
    }
    return fields, people


def verify_page(expected: dict[str, str], name_mode: str, found_name: str, dob: str, member_id: str) -> bool:
    dob_ok = is_present(dob)
    id_ok = is_present(member_id)
    if name_mode == "3":
        status = verify_three_word_name(
            found_name,
            expected["DummyFirstName"],
            expected["DummyMiddleName"],
            expected["DummyLastName"],
            dob_ok,
            id_ok,
        )
    elif name_mode == "2":
        status = verify_two_word_name(
            found_name,
            expected["DummyFirstName"],
            expected["DummyLastName"],
            dob_ok,
            id_ok,
        )
    else:
        status = "Reject"
    return status == "Accept"


def classify_page(
    ocr_text: str,
    ner_names: list[str],
    expected: dict[str, str],
    name_mode: str,
    verified: bool,
) -> str:
    """Verified / wrong member / not verified for one page.

    A page counts as wrong member only when a patient-name context on the page
    names someone other than the expected member. A page with nothing detected
    is not considered.
    """
    wrong_member = not verified and wrong_member_on_page(
        ocr_text,
        expected,
        name_mode,
        ner_names,
    )
    return page_status(verified=verified, wrong_member=wrong_member)


def document_verified(page_statuses: list[str], total: int) -> str:
    return apply_what_if(page_statuses, total)


def ocr_record(record_id: str) -> list[dict]:
    """Load OCR pages for a record from Azure Blob Storage."""
    if not blob_store.storage_configured():
        raise SystemExit("Azure Blob Storage is not configured in .env")
    try:
        blob_store.require_write_prefix()
    except RuntimeError as err:
        raise SystemExit(str(err)) from err
    pages = blob_store.load_ocr_pages(record_id)
    blob_name = blob_store.record_ocr_blob_name(record_id)
    if not pages:
        raise SystemExit(
            f"OCR JSON not found or empty in blob: "
            f"{blob_store.AZURE_STORAGE_CONTAINER}/{blob_name}"
        )
    pages = sorted(pages, key=lambda page: int(page.get("pageNumber") or 0))
    logger.info(
        "ocr source=blob record=%s pages=%s path=%s/%s",
        record_id,
        len(pages),
        blob_store.AZURE_STORAGE_CONTAINER,
        blob_name,
    )
    return pages


def page_count_of(document: dict | None, pages: list[dict]) -> int:
    if document and document.get("pageCount") is not None:
        try:
            return int(document["pageCount"])
        except (TypeError, ValueError):
            logger.warning(
                "pageCount %r in the OCR JSON is not a number; counting %s pages instead",
                document.get("pageCount"),
                len(pages),
            )
    return len(pages)


def load_blob_document(record_id: str) -> tuple[dict | None, list[dict]]:
    document = blob_store.load_ocr_document(record_id)
    pages = list((document or {}).get("pages") or []) if document else []
    pages = sorted(pages, key=lambda page: int(page.get("pageNumber") or 0))
    return document, pages


def verify_record(
    record_id: str,
    pages: list[dict],
    expected: dict[str, str],
    name_mode: str,
    model_id: str,
) -> tuple[list[dict], list[dict]]:
    """Verify one record with one model; return (member rows, ner rows)."""
    use_model(model_id)
    total = len(pages)
    rows: list[dict[str, str | int | bool]] = []
    ner_rows: list[dict] = []
    for index, page in enumerate(pages, start=1):
        text = page_text(page)
        page_no = int(page.get("pageNumber") or index)
        # One batch CSV holds every record, so each NER hit has to carry the
        # record and page it was read from.
        reset_ner_log()
        fields, people = extract_page_fields(text, expected, name_mode, model_id)
        page_ok = verify_page(
            expected,
            name_mode,
            fields["Detected_Full_Name"],
            fields["Detected_DOB"],
            fields["Detected_MemberID"],
        )
        if not page_ok and people is None:
            # The rules matched a name but the page still failed. Escalate to
            # NER to find out whether another member is named on it.
            people = name_candidates(text, model_id)
        ner_rows.extend(
            {"RecordId": record_id, "Page_No": page_no, **row} for row in ner_log_rows()
        )
        ner_names = [name for _score, name, _key in people or []]
        status = classify_page(text, ner_names, expected, name_mode, page_ok)
        rows.append(
            {
                "RecordId": record_id,
                "Total_Page_Count": total,
                "Page_No": page_no,
                **fields,
                "Page_Verified": page_ok,
                "Page_Detection_Correct": status == PAGE_VERIFIED,
                "Page_Detection_InCorrect": status == PAGE_WRONG_MEMBER,
                "_Page_Status": status,
            }
        )

    rows.sort(key=lambda row: int(row.get("Page_No") or 0))
    statuses = [str(row["_Page_Status"]) for row in rows]
    doc_status = document_verified(statuses, total)
    for row in rows:
        row["Document_Verified"] = doc_status
        row.pop("_Page_Status", None)

    logger.info(
        "record %s model %s: pages=%s verified=%s wrong_member=%s not_verified=%s "
        "reject_at=%s document=%s",
        record_id,
        model_id,
        total,
        statuses.count(PAGE_VERIFIED),
        count_wrong_member(statuses),
        statuses.count(PAGE_NOT_VERIFIED),
        reject_threshold(total),
        doc_status,
    )
    return rows, ner_rows


def _require_blob() -> tuple[str, str]:
    try:
        prefix = blob_store.require_write_prefix()
    except RuntimeError as err:
        raise SystemExit(str(err)) from err
    if not blob_store.storage_configured():
        raise SystemExit("Azure Blob Storage is not configured in .env")
    return blob_store.AZURE_STORAGE_CONTAINER, prefix


# --- OCR source --------------------------------------------------------------
#
# The three seams the run reads its OCR through. They point at Azure Blob and
# that is what production uses; run_local.py rebinds them to read the same JSON
# off disk so the pipeline can be exercised without a storage account.


def ocr_source() -> tuple[str, str]:
    """Where OCR JSON is read from, as (container, prefix), for logs + queue.

    Authenticates and warms the client up front, the way Azure_OCR does, so
    the browser prompt happens once at the start rather than lazily part-way
    through a long run.
    """
    container, prefix = _require_blob()
    try:
        blob_store.ensure_blob_ready()
    except Exception as err:
        logger.exception("Azure Blob auth failed")
        raise SystemExit(f"Azure Blob auth failed: {err}") from err
    return container, prefix


def list_ocr_records() -> list[str]:
    return blob_store.list_ocr_record_ids()


def load_ocr_document(record_id: str) -> tuple[dict | None, list[dict]]:
    """Read one record's OCR JSON, retrying a dropped connection.

    Records take minutes of NER compute each, so the pooled HTTPS connection
    sits idle in between and the service is entitled to close it. The next read
    then fails on a stale socket; retrying gets a fresh one.
    """
    last_error: Exception | None = None
    for attempt in range(1, BLOB_READ_ATTEMPTS + 1):
        try:
            return load_blob_document(record_id)
        except Exception as exc:
            last_error = exc
            if attempt < BLOB_READ_ATTEMPTS:
                logger.warning(
                    "blob read for %s failed (%s); retry %s/%s in %ss",
                    record_id,
                    exc,
                    attempt,
                    BLOB_READ_ATTEMPTS,
                    BLOB_READ_RETRY_SECONDS,
                )
                time.sleep(BLOB_READ_RETRY_SECONDS)
    raise RuntimeError(f"could not read OCR JSON for {record_id}: {last_error}") from last_error


def run(mode: str = "all") -> pd.DataFrame:
    """Work the verification queue, then write one batch of CSVs.

    mode "all" verifies every queued record; mode "selected" skips OCR JSON
    docs with more pages than MAX_PAGES.
    """
    if mode not in {"all", "selected"}:
        raise ValueError(f"unknown mode {mode!r}")

    system_path = mv_config.System_Input_path
    if not system_path.is_file():
        raise SystemExit(f"system_input.csv not found at {system_path}")
    system_rows, name_mode = load_system_input(system_path)
    if not system_rows:
        raise SystemExit(f"No records in {system_path}")

    enabled_ids = [by_id(model_id)["id"] for model_id in mv_config.enabled_model_ids()]
    if not enabled_ids:
        raise SystemExit("No NER models enabled in config/.env")

    # A model with no checkpoint on disk logs one error and then silently
    # detects nothing, which also silently disables wrong-member detection.
    # Fail before the queue runs instead of writing a batch of empty NER CSVs.
    absent = missing_weights(enabled_ids)
    if absent:
        raise SystemExit(
            "NER weights missing for enabled model(s): "
            + ", ".join(absent)
            + ". Download them with "
            "Member_Verification/Models/model_downloader/__main__.py, or "
            "disable them in .env (GLINER_LARGE / GLINER_MEDIUM / GLINER_LOW)."
        )
    # Resolve and authenticate the OCR source before loading any model, so a
    # bad prefix or a failed sign-in does not cost minutes of model loading.
    container, prefix = ocr_source()

    # Weights on disk are not proof the model loads, so load each one now.
    logger.info("loading %s NER model(s) ...", len(enabled_ids))
    try:
        ensure_loadable(enabled_ids)
    except ModelLoadError as err:
        logger.exception("NER model(s) could not be loaded")
        raise SystemExit(f"NER model(s) could not be loaded: {err}") from err
    logger.info("NER models ready: %s", enabled_ids)
    max_pages = int(mv_config.MAX_PAGES)
    record_ids = [
        record_id for record_id in list_ocr_records() if record_id in system_rows
    ]
    record_source = f"{container}/{prefix}"
    logger.info("OCR read from: %s", record_source)
    logger.info("output root: %s", mv_config.Output_path)
    logger.info("mode=%s models=%s", mode, enabled_ids)
    if mode == "selected":
        logger.info("MAX_PAGES=%s", max_pages)

    progress = load_progress(record_ids, mode, enabled_ids, container, prefix)
    remaining = pending_records(progress)
    logger.info(
        "queue remaining=%s of %s records",
        len(remaining),
        progress.get("record_count"),
    )

    try:
        for record_id in remaining:
            _set_item_status(progress, record_id, "running", error=None)
            try:
                document, pages = load_ocr_document(record_id)
                if not pages:
                    logger.info("skip %s (empty OCR JSON)", record_id)
                    _set_item_status(progress, record_id, "skipped", pageCount=0)
                    continue

                count = page_count_of(document, pages)
                if mode == "selected" and count > max_pages:
                    logger.info("skip %s (pages=%s > MAX_PAGES=%s)", record_id, count, max_pages)
                    _set_item_status(progress, record_id, "skipped", pageCount=count)
                    continue

                already = _models_done(progress, record_id)
                for model_id in enabled_ids:
                    if model_id in already:
                        logger.info("skip %s model %s (already staged)", record_id, model_id)
                        continue
                    logger.info("ner model %s on %s (pages=%s)", model_id, record_id, count)
                    member_rows, ner_rows = verify_record(
                        record_id,
                        pages,
                        system_rows[record_id],
                        name_mode,
                        model_id,
                    )
                    append_rows(MEMBER_KIND, model_id, COLUMNS, member_rows)
                    append_rows(NER_KIND, model_id, NER_COLUMNS, ner_rows)
                    _mark_model_done(progress, record_id, model_id)
                _set_item_status(progress, record_id, "done", pageCount=count)
            except Exception as exc:
                logger.exception("record %s failed: %s", record_id, exc)
                _set_item_status(progress, record_id, "error", error=str(exc))
                raise
    except KeyboardInterrupt:
        progress["status"] = "stopped"
        for item in progress.get("queue") or []:
            if item.get("status") == "running":
                item["status"] = "pending"
        save_progress(progress)
        done = sum(1 for item in progress.get("queue") or [] if item.get("status") == "done")
        staged = staged_files()
        # No batch folder exists yet: it is only written when the queue
        # finishes. Say where the finished rows are so an interrupted run does
        # not look like it produced nothing.
        logger.info(
            "stopped after %s/%s records. No batch folder was written yet -- "
            "rows for the finished records are staged in %s (%s file(s)). "
            "Rerun to carry on from here; the batch is written when the queue "
            "completes.",
            done,
            progress.get("record_count"),
            mv_config.STAGING_DIR,
            len(staged),
        )
        raise
    except Exception:
        progress["status"] = "error"
        save_progress(progress)
        raise

    progress["status"] = "done"
    save_progress(progress)
    logger.info("queue finished: %s", queue_counts(progress))

    batch_dir = finalize_batch()
    if batch_dir is None:
        raise SystemExit(f"No verified records under {record_source}")
    logger.info("batch output: %s", batch_dir)
    build_member_csv_outputs(batch_dir, enabled_ids)

    frames = [
        pd.read_csv(batch_dir / mv_config.mv_csv_name(model_id), encoding="utf-8-sig")
        for model_id in enabled_ids
        if (batch_dir / mv_config.mv_csv_name(model_id)).is_file()
    ]
    if not frames:
        raise SystemExit(f"No verified records under {record_source}")
    return pd.concat(frames, ignore_index=True)


def main() -> int:
    setup_logging()
    try:
        frame = run("all")
    except SystemExit:
        raise
    except BaseException:
        # Log the whole traceback where the run log will keep it, then stop.
        logger.exception("member verification failed")
        raise
    print(frame.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
