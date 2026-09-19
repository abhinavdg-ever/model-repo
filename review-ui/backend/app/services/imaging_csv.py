"""Build Imaging Document CSV (same columns as FolderViewer Document CSV)."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from typing import Any

from app.core.schemas import ImagingDocumentResponse, ImagingPageResult, OcrRunStatus

CSV_HEADERS = [
    "chartName",
    "pageName",
    "memberName",
    "memberID",
    "confidence",
    "memberDob",
    "handwrittenOrPrinted",
    "handwrittenOrPrintedConfidence",
    "orientationAngle",
    "tiltAngle",
    "mirrored",
    "pageQualityConfidence",
    "blankOrJunk",
    "isDuplicate",
    "pageType",
    "pageTypeConfidence",
    "dosFrom",
    "dosTo",
    "dosConfidence",
    "member_verification_status",
]

DEFAULT_DOC_DOS = "2/2/2022"
DEFAULT_DOC_DOS_CONFIDENCE = 0.8


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def fill_doc_dos_for_download(pages: list[ImagingPageResult]) -> list[ImagingPageResult]:
    """Prefer docDos*; else carry-forward / default 2/2/2022 @ 80%."""
    prev_from: str | None = None
    prev_to: str | None = None
    prev_conf: float | None = None
    out: list[ImagingPageResult] = []
    for page in sorted(pages, key=lambda p: p.pageNumber):
        dos_from = (page.docDosFrom or page.dosFrom or "").strip() or None
        dos_to = (page.docDosTo or page.dosTo or "").strip() or None
        dos_confidence = page.dosConfidence
        if not dos_from and not dos_to:
            if prev_from:
                dos_from = prev_from
                dos_to = prev_to or prev_from
                dos_confidence = prev_conf
            else:
                dos_from = DEFAULT_DOC_DOS
                dos_to = DEFAULT_DOC_DOS
                dos_confidence = DEFAULT_DOC_DOS_CONFIDENCE
        else:
            if not dos_from:
                dos_from = dos_to or prev_from or DEFAULT_DOC_DOS
            if not dos_to:
                dos_to = dos_from or prev_to or DEFAULT_DOC_DOS
            if (
                dos_from == DEFAULT_DOC_DOS or dos_to == DEFAULT_DOC_DOS
            ) and dos_confidence is None:
                dos_confidence = DEFAULT_DOC_DOS_CONFIDENCE
        prev_from = dos_from
        prev_to = dos_to
        prev_conf = dos_confidence if dos_confidence is not None else prev_conf
        out.append(
            page.model_copy(
                update={
                    "docDosFrom": dos_from,
                    "docDosTo": dos_to,
                    "dosConfidence": dos_confidence,
                }
            )
        )
    return out


def _verification_status(doc: ImagingDocumentResponse) -> str:
    if doc.verifications:
        return doc.verifications[0].finalStatus or ""
    if doc.verification and doc.verification.finalStatus:
        return doc.verification.finalStatus
    return ""


def document_rows(chart_name: str, doc: ImagingDocumentResponse) -> list[list[str]]:
    status = _verification_status(doc)
    rows: list[list[str]] = []
    for page in fill_doc_dos_for_download(doc.pages):
        blank = page.blankOrJunk
        dup = page.isDuplicate
        rows.append(
            [
                chart_name,
                page.fileName,
                _cell(page.memberName),
                _cell(page.memberId),
                _cell(page.memberConfidence),
                _cell(page.memberDob),
                _cell(page.handwrittenOrPrinted),
                _cell(page.handwrittenOrPrintedConfidence),
                _cell(page.orientationAngle),
                _cell(page.tiltAngle),
                _cell(page.mirrored),
                _cell(page.pageQualityConfidence),
                "NA" if blank is None or blank == "" else _cell(blank),
                "NA" if dup is None else ("Yes" if dup else "No"),
                page.pageType or "Not Available",
                _cell(page.pageTypeConfidence),
                _cell(page.docDosFrom or page.dosFrom),
                _cell(page.docDosTo or page.dosTo),
                _cell(page.dosConfidence),
                status,
            ]
        )
    return rows


def iter_csv_lines(
    docs: Iterable[tuple[str, ImagingDocumentResponse]],
) -> Iterator[str]:
    """Yield CSV text lines including header."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADERS)
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)

    for chart_name, doc in docs:
        for row in document_rows(chart_name, doc):
            writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)


def build_csv_bytes(
    docs: Iterable[tuple[str, ImagingDocumentResponse]],
) -> bytes:
    return "".join(iter_csv_lines(docs)).encode("utf-8")


def filter_folder(
    *,
    name: str,
    ocr_status: OcrRunStatus,
    status: str | None,
    q: str | None,
) -> bool:
    if status and status != "ALL" and ocr_status != status:
        return False
    if q and q.strip().lower() not in name.lower():
        return False
    return True
