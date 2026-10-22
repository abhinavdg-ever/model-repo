"""Build Imaging Document CSV (same columns as FolderViewer Document CSV)."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from typing import Any

from app.core.schemas import ImagingDocumentResponse, ImagingPageResult, OcrRunStatus
from app.services.duplicate_label import format_duplicate_label

CSV_HEADERS = [
    "chartName",
    "pageName",
    "memberName",
    "memberID",
    "confidence",
    "memberDob",
    "handwrittenOrPrinted",
    "handwrittenOrPrintedConfidence",
    "documentType",
    "isVisible",
    "handwrittenAreaPct",
    "orientationAngle",
    "tiltAngle",
    "mirrored",
    "pageQualityTag",
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

DEFAULT_DOC_DOS = "2222-02-02"
DEFAULT_DOC_DOS_CONFIDENCE = 0.8


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def fill_doc_dos_for_download(pages: list[ImagingPageResult]) -> list[ImagingPageResult]:
    """Prefer docDos*, else the page's own DOS, else the default 2222-02-02 @ 80%.
    A page never takes the DOS of the page before it."""
    out: list[ImagingPageResult] = []
    for page in sorted(pages, key=lambda p: p.pageNumber):
        dos_from = (page.docDosFrom or page.dosFrom or "").strip() or None
        dos_to = (page.docDosTo or page.dosTo or "").strip() or None
        dos_confidence = page.dosConfidence
        if not dos_from and not dos_to:
            dos_from = dos_to = DEFAULT_DOC_DOS
            dos_confidence = DEFAULT_DOC_DOS_CONFIDENCE
        else:
            dos_from = dos_from or dos_to
            dos_to = dos_to or dos_from
            if DEFAULT_DOC_DOS in (dos_from, dos_to) and dos_confidence is None:
                dos_confidence = DEFAULT_DOC_DOS_CONFIDENCE
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
                _cell(page.documentType),
                _cell(page.isVisible),
                _cell(page.handwrittenAreaPct),
                _cell(page.orientationAngle),
                _cell(page.tiltAngle),
                _cell(page.mirrored),
                _cell(page.pageQualityTag),
                _cell(page.pageQualityConfidence),
                "NA" if blank is None or blank == "" else _cell(blank),
                format_duplicate_label(dup, page.pageTypeConfidence, page.isPartialDuplicate),
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
