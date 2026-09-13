"""Member verification engine.

Ported from ``Reference/V1 Code/Member_Verification/run.py``. Function names,
call order and decision logic match the reference so a pipeline run is diffable
against a V1 run page for page.

What the reference did that this keeps
--------------------------------------
* **Expected-member driven.** Every field is looked for *because the manifest
  says what to look for* — the page is searched for this member's name, DOB and
  MemberID. It is not a blind "extract a name, then compare".
* **Rules first, NER only on a miss.** ``extract_page_fields`` runs the whole-page
  rules for each of the three fields; only a field the rules did not find
  escalates to the NER layer, which builds a sentence around the matching key
  ("Patient Name: Robert Smith") and reads that. A page the rules already
  matched costs no model time.
* **The wrong-member escalation.** When the rules found a name but the page
  still failed verification, NER runs anyway — to find out whether the page
  names *a different* member. That is the only way a page becomes
  ``wrong_member``, and wrong-member pages are what reject a document.
* **what-if thresholds.** Document decision is Accept/Reject on
  ``wrong pages >= min(5, ceil(10% of pages))``, from ``what_if_rules``.

What differs, and why
---------------------
* ``name_mode`` comes from the manifest row's populated name parts rather than
  from a CSV header set — same two/three-word semantics.
* The NER layer can be switched off (``MEMBER_NER_ENABLED=false``). When it is,
  ``ner_enabled=False`` is stamped on the result so a rules-only run is never
  read as a full one. With the layer on, the reference's fail-loud contract on
  an unloadable model is unchanged.
* Results are returned as dataclasses for the stage to persist, instead of being
  written straight to CSV rows. The reference's CSV column names are preserved
  in :func:`page_result_to_v1_row` for diffing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .extractors.ner_based.config import ner_enabled as NER_ENABLED
from .extractors.ner_based.dob import extract_dob_ner
from .extractors.ner_based.member_id import extract_member_id_ner
from .extractors.ner_based.name import name_candidates, pick_name
from .extractors.rule_based.dob import extract_dob
from .extractors.rule_based.member_id import extract_member_id
from .extractors.rule_based.name_2_words import extract_name_2_words
from .extractors.rule_based.name_3_words import extract_name_3_words
from .rules.base_rules import is_present
from .rules.name_2_words_rules import verify_two_word_name
from .rules.name_3_words_rules import verify_three_word_name
from .rules.what_if_rules import (
    ACCEPT,
    PAGE_NOT_VERIFIED,
    PAGE_VERIFIED,
    PAGE_WRONG_MEMBER,
    REJECT,
    apply_what_if,
    count_wrong_member,
    page_status,
    reject_threshold,
)
from .rules.wrong_member_rules import wrong_member_on_page

logger = logging.getLogger(__name__)

# V1 page bucket -> member_extraction_results.page_status
PAGE_STATUS_DB = {
    PAGE_VERIFIED: "verified",
    PAGE_WRONG_MEMBER: "wrong_member",
    PAGE_NOT_VERIFIED: "not_verified",
}

# V1 detection source -> member_extraction_results.detection_source_*
DETECTION_SOURCE_DB = {"rule based": "rule_based", "ner": "ner", "": ""}


@dataclass
class PageResult:
    """One page's verification outcome (one member_extraction_results row)."""

    page_no: int
    page_name: str
    detected_name: str
    detected_dob: str
    detected_member_id: str
    detection_source_name: str
    detection_source_dob: str
    detection_source_member_id: str
    ner_key_source_name: str
    ner_key_source_dob: str
    ner_key_source_member_id: str
    page_verified: bool
    page_status: str          # V1 bucket: Verified / Wrong_Member / Not_Verified
    ner_names: list[str] = field(default_factory=list)

    @property
    def db_page_status(self) -> str:
        return PAGE_STATUS_DB.get(self.page_status, "not_verified")


@dataclass
class RecordResult:
    """A chart's verification outcome (one member_verification_summary row)."""

    record_id: str
    name_mode: str
    total_pages: int
    pages_checked: int
    pages_verified: int
    pages_wrong_member: int
    pages_not_verified: int
    reject_threshold: int
    document_decision: str    # V1: Accept / Reject
    ner_enabled: bool
    model_id: Optional[str]
    pages: list[PageResult] = field(default_factory=list)
    expected: dict[str, str] = field(default_factory=dict)

    @property
    def db_document_decision(self) -> str:
        return "accept" if self.document_decision == ACCEPT else "reject"


def expected_from_manifest(row: dict[str, Any]) -> dict[str, str]:
    """manifest_member_list row -> the reference's `expected` dict.

    Key names are the reference's (DummyFirstName, ...) so every ported rule
    reads them unchanged.
    """

    def _s(value: Any) -> str:
        return "" if value is None else str(value).strip()

    dob = row.get("member_dob")
    dob_text = ""
    if dob:
        # The reference's rule extractor parses DummyDOB as MM/DD/YYYY.
        if hasattr(dob, "strftime"):
            dob_text = dob.strftime("%m/%d/%Y")
        else:
            dob_text = _s(dob)

    first = _s(row.get("first_name"))
    middle = _s(row.get("middle_name"))
    last = _s(row.get("last_name"))

    # Fall back to splitting member_name when the parts were never populated.
    if not first and not last:
        parts = [p for p in _s(row.get("member_name")).split() if p]
        if len(parts) == 2:
            first, last = parts
        elif len(parts) >= 3:
            first, middle, last = parts[0], parts[1], parts[-1]
        elif len(parts) == 1:
            first = parts[0]

    return {
        "DummyFirstName": first,
        "DummyMiddleName": middle,
        "DummyLastName": last,
        "DummyDOB": dob_text,
        "MemberID": _s(row.get("external_member_id")),
    }


def detect_name_mode(expected: dict[str, str]) -> str:
    """'3' when a middle name is known, '2' for first+last, '' when unusable.

    The reference read this off the system-input CSV's header set; the manifest
    carries the same information in which name parts are populated.
    """
    first = (expected.get("DummyFirstName") or "").strip()
    middle = (expected.get("DummyMiddleName") or "").strip()
    last = (expected.get("DummyLastName") or "").strip()
    if first and middle and last:
        return "3"
    if first and last:
        return "2"
    return ""


# --- ported verbatim from run.py -------------------------------------------


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
    model_id: Optional[str],
) -> tuple[dict[str, str], Optional[list[tuple[float, str, str]]]]:
    """Return (page fields, NER name candidates or None if NER never ran).

    Every field is looked for with the rules over the whole page first. Only
    where the rules find nothing does the page go to the NER layer, which
    builds a sentence around the matching key and reads that. So a page whose
    details the rules already matched costs no model time at all.
    """
    people: Optional[list[tuple[float, str, str]]] = None

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


def verify_page(
    expected: dict[str, str],
    name_mode: str,
    found_name: str,
    dob: str,
    member_id: str,
) -> bool:
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


# --- chart-level driver -----------------------------------------------------


def verify_record(
    record_id: str,
    pages: list[dict[str, Any]],
    expected: dict[str, str],
    name_mode: str,
    model_id: Optional[str] = None,
    total_pages: Optional[int] = None,
) -> RecordResult:
    """Verify one chart. `pages` is [{page_no, page_name, text}, ...].

    Mirrors run.py::verify_record. `total_pages` is the chart's full page count,
    which can exceed len(pages) once blank/junk pages are excluded — the
    reject threshold is a proportion of the *document*, so it must be computed
    on the real total, not on the subset that reached this stage.
    """
    if model_id is not None and NER_ENABLED:
        from .extractors.ner_based.model import use_model

        use_model(model_id)

    total = int(total_pages if total_pages is not None else len(pages))
    results: list[PageResult] = []

    for index, page in enumerate(pages, start=1):
        text = str(page.get("text") or "")
        page_no = int(page.get("page_no") or index)

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

        ner_names = [name for _score, name, _key in people or []]
        status = classify_page(text, ner_names, expected, name_mode, page_ok)

        results.append(
            PageResult(
                page_no=page_no,
                page_name=str(page.get("page_name") or ""),
                detected_name=fields["Detected_Full_Name"],
                detected_dob=fields["Detected_DOB"],
                detected_member_id=fields["Detected_MemberID"],
                detection_source_name=fields["Detection_Source_Name"],
                detection_source_dob=fields["Detection_Source_DOB"],
                detection_source_member_id=fields["Detection_Source_MemberID"],
                ner_key_source_name=fields["ner_key_source_Name"],
                ner_key_source_dob=fields["ner_key_source_DOB"],
                ner_key_source_member_id=fields["ner_key_source_MemberID"],
                page_verified=page_ok,
                page_status=status,
                ner_names=ner_names,
            )
        )

    results.sort(key=lambda r: r.page_no)
    statuses = [r.page_status for r in results]
    doc_status = document_verified(statuses, total)
    threshold = reject_threshold(total)

    logger.info(
        "record %s model %s: pages=%s checked=%s verified=%s wrong_member=%s "
        "not_verified=%s reject_at=%s document=%s ner=%s",
        record_id,
        model_id,
        total,
        len(results),
        statuses.count(PAGE_VERIFIED),
        count_wrong_member(statuses),
        statuses.count(PAGE_NOT_VERIFIED),
        threshold,
        doc_status,
        NER_ENABLED,
    )

    return RecordResult(
        record_id=record_id,
        name_mode=name_mode,
        total_pages=total,
        pages_checked=len(results),
        pages_verified=statuses.count(PAGE_VERIFIED),
        pages_wrong_member=count_wrong_member(statuses),
        pages_not_verified=statuses.count(PAGE_NOT_VERIFIED),
        reject_threshold=threshold,
        document_decision=doc_status,
        ner_enabled=NER_ENABLED,
        model_id=model_id,
        pages=results,
        expected=expected,
    )


def summary_status(result: RecordResult) -> tuple[str, str]:
    """(member_verification_summary.final_status, decision_reason).

    The reference produced Accept/Reject; the schema also carries a
    verified/failed/needs_review triage value for the review UI.
    """
    if result.document_decision == REJECT:
        return "failed", "wrong_member_threshold"
    if result.pages_verified > 0:
        return "verified", "pages_verified"
    if result.pages_checked == 0:
        return "needs_review", "no_pages_checked"
    if not result.name_mode:
        return "needs_review", "manifest_name_incomplete"
    return "needs_review", "no_page_verified"


def page_result_to_v1_row(
    result: RecordResult, page: PageResult
) -> dict[str, Any]:
    """One row in the reference's Member CSV column order, for diffing."""
    return {
        "RecordId": result.record_id,
        "Total_Page_Count": result.total_pages,
        "Page_No": page.page_no,
        "Detection_Source_Name": page.detection_source_name,
        "ner_key_source_Name": page.ner_key_source_name,
        "Detected_Full_Name": page.detected_name,
        "Detection_Source_DOB": page.detection_source_dob,
        "ner_key_source_DOB": page.ner_key_source_dob,
        "Detected_DOB": page.detected_dob,
        "Detection_Source_MemberID": page.detection_source_member_id,
        "ner_key_source_MemberID": page.ner_key_source_member_id,
        "Detected_MemberID": page.detected_member_id,
        "Page_Verified": page.page_verified,
        "Document_Verified": result.document_decision,
        "Page_Detection_Correct": page.page_status == PAGE_VERIFIED,
        "Page_Detection_InCorrect": page.page_status == PAGE_WRONG_MEMBER,
    }
