"""Algo Generated rows for Manual Review built from the pipeline output
review-ui already shows, instead of an uploaded sheet.

PDF ID is the chart name and Page Number the page number. Values follow
the Accuracy tab: the Final value (imaging_final, after document continuity)
where the run has one, else the page's own extracted value -- for DOS,
codability and page sub-type. A page with no date is left blank, not given
the 2222-02-02 default Download Results ships. Page Type is the encounter
type (Outpatient (F2F), Inpatient, …), which is what ground truth labels as
page type; Page Subtype is the classifier's sub-type, split back out of its
``Type (Sub-type)`` display.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from app.core.schemas import ImagingDocumentResponse

COLUMNS = [
    "PDF ID",
    "Page Number",
    "Extracted Name",
    "Extracted DOB",
    "Member ID",
    "DOS From",
    "DOS To",
    "Is Blank or Junk?",
    "Duplicate",
    "Page Type",
    "Page Subtype",
    "Codable / Non-Codable",
    "Provider Signature",
    "Current Sequence",
    "Actual Sequence",
]


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _yes_no(value: bool | None) -> str:
    return "" if value is None else ("Yes" if value else "No")


def _blank_or_junk(value: str | None) -> str:
    """"Yes (Blank)" / "Yes (Junk)" / "No" -> Yes / No; not run stays blank."""
    text = _text(value)
    if not text:
        return ""
    return "Yes" if text.lower().startswith("y") else "No"


def _page_subtype(shown: str | None) -> str:
    """``Type (Sub-type)`` -> Sub-type; a bare name is its own sub-type."""
    text = _text(shown)
    if text.endswith(")") and " (" in text:
        return text[:-1].partition(" (")[2].strip()
    return text


def system_csv(docs: Iterable[tuple[str, ImagingDocumentResponse]]) -> bytes:
    """The pipeline output as the CSV an uploaded Algo Generated file would be."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    for chart_name, doc in docs:
        for page in sorted(doc.pages, key=lambda p: p.pageNumber):
            # Final first, then extracted, as accuracy.ts reads them.
            dos_from = _text(page.finalDosFrom) or _text(page.dosFrom)
            dos_to = _text(page.finalDosTo) or _text(page.finalDosFrom) or _text(page.dosTo)
            writer.writerow(
                {
                    "PDF ID": chart_name,
                    "Page Number": page.pageNumber,
                    "Extracted Name": _text(page.memberName),
                    "Extracted DOB": _text(page.memberDob),
                    "Member ID": _text(page.memberId),
                    "DOS From": dos_from,
                    "DOS To": dos_to,
                    "Is Blank or Junk?": _blank_or_junk(page.blankOrJunk),
                    "Duplicate": _yes_no(page.isDuplicate),
                    "Page Type": _text(page.encounterType),
                    "Page Subtype": _page_subtype(page.finalPageType or page.pageType),
                    "Codable / Non-Codable": _text(page.finalCodeable) or _text(page.isCodeable),
                    "Provider Signature": _text(page.providerSignature),
                    "Current Sequence": _text(page.currentSequence),
                    "Actual Sequence": _text(page.actualSequence),
                }
            )
    return buffer.getvalue().encode("utf-8")


def pdf_ids(data: bytes, filename: str, label: str = "Ground Truth") -> list[str]:
    """The PDF IDs in an uploaded file, read with the tool's own header rules."""
    from .compare import _read_file
    from .record_matcher import normalize_key

    from . import _upload

    frame = _read_file(_upload(data, filename))
    if "PDF ID" not in frame.columns:
        raise ValueError(f"{label} file is missing required column(s): PDF ID.")
    return sorted({normalize_key(value, 0)[0] for value in frame["PDF ID"]} - {""})
