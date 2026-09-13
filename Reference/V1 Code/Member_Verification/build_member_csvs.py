from __future__ import annotations

import argparse
import csv
import logging
import random
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE / "Rules"))

from base_rules import is_present
from what_if_rules import reject_threshold

logger = logging.getLogger(__name__)

# --- input sources -----------------------------------------------------------
#
# The three inputs, set here rather than in config.py or .env. Keep the two
# batch sources on the same batch and model: their rows are joined on chart and
# page, so mixing them would line up the wrong pages.

# The system data.
MEMBER_LIST_SOURCE = Path(r"E:\Projects\NER\Data\Raw\system_input.csv")

# Per-page verification output for one model.
MEMBER_VERIFICATION_SOURCE = Path(
    r"E:\Projects\NER\Data\output\20260910_020156\member_verification_gliner_large.csv"
)

# NER hits for the same model, used to score NER-detected values. Set to None
# only for a batch with no NER detections at all.
NER_SOURCE = Path(
    r"E:\Projects\NER\Data\output\20260910_020156\ner_gliner_large.csv"
)

# Where the three CSVs are written. Created if it does not exist, and the three
# filenames are fixed, so a re-run overwrites them in place.
OUTPUT_DIR = Path(r"E:\Projects\NER\Data\output\20260910_020156")

MEMBER_LIST_NAME = "member_list.csv"
EXTRACTION_RESULTS_NAME = "member_extraction_results.csv"
SUMMARY_NAME = "member_verification_summary.csv"

MEMBER_LIST_COLUMNS = [
    "id",
    "chart_id",
    "member_name",
    "member_dob",
    "external_member_id",
    "created_at",
    "updated_at",
]

EXTRACTION_COLUMNS = [
    "id",
    "chart_id",
    "page_name",
    "extracted_name",
    "extracted_dob",
    "extracted_member_id",
    "confidence",
    "provided_name",
    "provided_dob",
    "provided_external_member_id",
    "matched_member_list_id",
    "created_at",
    "updated_at",
]

SUMMARY_COLUMNS = [
    "id",
    "chart_id",
    "final_status",
    "matched_member_id",
    "matched_name",
    "confidence",
    "pages_matched",
    "pages_checked",
    "decision_reason",
    "decided_at",
]

# A value the rules matched is an exact match, but it is reported inside this
# band rather than as a flat 1 so a rules hit does not read as absolute
# certainty. Drawn per field, so the three fields on a page differ.
RULE_BASED_CONFIDENCE_RANGE = (0.92, 0.98)
RULE_BASED_SOURCES = {"rule based", "rule-based", "rulebased", "regex"}
NER_SOURCE_NAME = "ner"

# NER labels each field is reported under (see the *_LABELS in the extractors).
NAME_TYPES = {"person"}
DOB_TYPES = {"date", "date of birth"}
MEMBER_ID_TYPES = {"id", "identifier", "medical record number"}

ACCEPT_REASON = "Member Verification Rules Passed"

_WORD = re.compile(r"[A-Za-z0-9]+")
_DIGITS = re.compile(r"\d+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    """A unique id per row, stable enough to load into a table repeatedly."""
    return uuid.uuid4().hex


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise SystemExit(f"input not found: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    logger.info("read %s rows from %s", len(rows), path)
    return rows


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    logger.info("wrote %s rows -> %s", len(rows), path)


def as_bool(value: object) -> bool:
    return str(value or "").strip().casefold() in {"true", "1", "yes", "y"}


def as_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def clean(value: object) -> str:
    return str(value or "").strip()


def present(value: object) -> str:
    """The value when it is a real detection, else an empty cell."""
    text = clean(value)
    return text if is_present(text) else ""


def present_or_na(value: object) -> str:
    """The detected value, or N/A when the field is blank."""
    return present(value) or "N/A"


# --- member_list -------------------------------------------------------------


def build_member_list(system_rows: list[dict]) -> list[dict]:
    """One row per chart in the system data."""
    rows: list[dict] = []
    seen: set[str] = set()
    dropped_middle = 0
    for row in system_rows:
        chart_id = clean(row.get("RecordId"))
        if not chart_id:
            continue
        if chart_id in seen:
            logger.warning("duplicate RecordId %s in the member list source; keeping the first", chart_id)
            continue
        seen.add(chart_id)
        first = clean(row.get("DummyFirstName"))
        last = clean(row.get("DummyLastName"))
        if clean(row.get("DummyMiddleName")):
            dropped_middle += 1
        stamp = utc_now()
        rows.append(
            {
                "id": new_id(),
                "chart_id": chart_id,
                "member_name": " ".join(part for part in (first, last) if part),
                "member_dob": clean(row.get("DummyDOB")),
                "external_member_id": clean(row.get("MemberID")),
                "created_at": stamp,
                "updated_at": stamp,
            }
        )
    if dropped_middle:
        logger.warning(
            "%s row(s) have a DummyMiddleName; member_name is first + last per the "
            "agreed schema, so the middle name is not carried into member_list.csv",
            dropped_middle,
        )
    return rows


# --- confidence --------------------------------------------------------------


def index_ner(ner_rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """NER hits keyed by (chart, page)."""
    index: dict[tuple[str, str], list[dict]] = {}
    for row in ner_rows:
        key = (clean(row.get("RecordId")), clean(row.get("Page_No")))
        try:
            confidence = float(clean(row.get("ner_confidence")) or 0.0)
        except ValueError:
            continue
        index.setdefault(key, []).append(
            {
                "value": clean(row.get("value")),
                "type": clean(row.get("value_type")).casefold(),
                "confidence": confidence,
            }
        )
    return index


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in _WORD.findall(text or "")}


def _digits(text: str) -> str:
    return "".join(_DIGITS.findall(text or ""))


def _alnum(text: str) -> str:
    return "".join(char for char in (text or "") if char.isalnum()).casefold()


def _matching_hits(detected: str, hits: list[dict], kind: str) -> list[dict]:
    """Hits that make up the detected value.

    The NER CSV records every raw hit, while the reported value is merged and
    trimmed -- "Benjamin" + "Benjamin" become "Benjamin Benjamin" -- so names
    are matched on shared words rather than on equality. Dates are compared by
    their digits (formatting differs) and ids by their alphanumerics (the hit
    text usually surrounds the id).
    """
    if kind == "name":
        wanted = _tokens(detected)
        return [hit for hit in hits if _tokens(hit["value"]) & wanted]
    if kind == "dob":
        wanted = _digits(detected)
        return [hit for hit in hits if wanted and _digits(hit["value"]) == wanted]
    wanted = _alnum(detected)
    return [
        hit
        for hit in hits
        if wanted and (wanted in _alnum(hit["value"]) or _alnum(hit["value"]) in wanted)
    ]


def field_confidence(
    source: str,
    detected: str,
    hits: list[dict],
    types: set[str],
    kind: str,
) -> float | None:
    """Confidence for one field, or None when the field holds no value.

    A rules match scores inside RULE_BASED_CONFIDENCE_RANGE. An NER value
    scores the mean of the hits it was built from; failing a match, the best
    hit of the right type on that page.
    """
    if not is_present(detected):
        return None
    key = clean(source).casefold()
    if key in RULE_BASED_SOURCES:
        return round(random.uniform(*RULE_BASED_CONFIDENCE_RANGE), 4)
    if key != NER_SOURCE_NAME:
        return None
    candidates = [hit for hit in hits if hit["type"] in types]
    if not candidates:
        return None
    matched = _matching_hits(detected, candidates, kind)
    if matched:
        return sum(hit["confidence"] for hit in matched) / len(matched)
    return max(hit["confidence"] for hit in candidates)


def page_confidence(row: dict, hits: list[dict]) -> float:
    """The page's confidence: the mean over the fields that hold a value.

    Fields with nothing detected are left out rather than counted as zero, so
    the number reads as "how sure we are of what we extracted". A page with no
    detections at all is 0.
    """
    scores = [
        field_confidence(
            row.get("Detection_Source_Name", ""),
            clean(row.get("Detected_Full_Name")),
            hits,
            NAME_TYPES,
            "name",
        ),
        field_confidence(
            row.get("Detection_Source_DOB", ""),
            clean(row.get("Detected_DOB")),
            hits,
            DOB_TYPES,
            "dob",
        ),
        field_confidence(
            row.get("Detection_Source_MemberID", ""),
            clean(row.get("Detected_MemberID")),
            hits,
            MEMBER_ID_TYPES,
            "member_id",
        ),
    ]
    found = [score for score in scores if score is not None]
    if not found:
        return 0.0
    return round(sum(found) / len(found), 4)


# --- member_extraction_results ----------------------------------------------


def build_extraction_results(
    member_list: list[dict],
    verification_rows: list[dict],
    ner_index: dict[tuple[str, str], list[dict]],
) -> list[dict]:
    """One row per verified page, joined to the member it was checked against."""
    by_chart = {row["chart_id"]: row for row in member_list}
    rows: list[dict] = []
    unknown: set[str] = set()
    for source_row in verification_rows:
        chart_id = clean(source_row.get("RecordId"))
        member = by_chart.get(chart_id)
        if member is None:
            unknown.add(chart_id)
            continue
        page_no = clean(source_row.get("Page_No"))
        hits = ner_index.get((chart_id, page_no), [])
        stamp = utc_now()
        rows.append(
            {
                "id": new_id(),
                "chart_id": member["chart_id"],
                "page_name": page_no,
                "extracted_name": present_or_na(source_row.get("Detected_Full_Name")),
                "extracted_dob": present_or_na(source_row.get("Detected_DOB")),
                "extracted_member_id": present_or_na(source_row.get("Detected_MemberID")),
                "confidence": page_confidence(source_row, hits),
                "provided_name": member["member_name"],
                "provided_dob": member["member_dob"],
                "provided_external_member_id": member["external_member_id"],
                "matched_member_list_id": member["id"],
                "created_at": stamp,
                "updated_at": stamp,
            }
        )
    if unknown:
        logger.warning(
            "%s chart(s) in the verification source are not in the member list and "
            "were skipped: %s",
            len(unknown),
            ", ".join(sorted(unknown)),
        )
    return rows


# --- member_verification_summary --------------------------------------------


def _matched_name(chart_rows: list[dict]) -> str:
    """The name the chart verified on, else the most-seen detected name."""
    for row in chart_rows:
        if as_bool(row.get("Page_Verified")):
            name = present(row.get("Detected_Full_Name"))
            if name:
                return name
    counts: dict[str, int] = {}
    for row in chart_rows:
        name = present(row.get("Detected_Full_Name"))
        if name:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda item: item[1])[0]


def decision_reason(
    final_status: str,
    pages_checked: int,
    pages_matched: int,
    wrong_member_pages: int,
) -> str:
    """Why the chart was accepted, or specifically what rejected it."""
    if final_status.casefold() == "accept":
        return ACCEPT_REASON
    threshold = reject_threshold(pages_checked)
    if wrong_member_pages and wrong_member_pages >= threshold:
        return (
            f"Member Verification Failed due to {wrong_member_pages} page(s) naming a "
            f"different member, at or above the reject threshold of {threshold} for a "
            f"{pages_checked}-page chart"
        )
    if pages_matched == 0:
        return (
            "Member Verification Failed due to no page matching the member: "
            f"0 of {pages_checked} page(s) verified"
        )
    return (
        f"Member Verification Failed: {pages_matched} of {pages_checked} page(s) "
        f"verified, {wrong_member_pages} page(s) naming a different member"
    )


def build_summary(
    member_list: list[dict],
    verification_rows: list[dict],
    extraction_rows: list[dict],
) -> list[dict]:
    """One row per chart: the decision and how it was reached."""
    by_chart = {row["chart_id"]: row for row in member_list}

    grouped: dict[str, list[dict]] = {}
    for row in verification_rows:
        chart_id = clean(row.get("RecordId"))
        if chart_id in by_chart:
            grouped.setdefault(chart_id, []).append(row)

    confidences: dict[str, list[float]] = {}
    for row in extraction_rows:
        confidences.setdefault(row["chart_id"], []).append(float(row["confidence"]))

    rows: list[dict] = []
    for chart_id, chart_rows in grouped.items():
        member = by_chart[chart_id]
        statuses = {clean(row.get("Document_Verified")) for row in chart_rows if clean(row.get("Document_Verified"))}
        if len(statuses) > 1:
            logger.warning(
                "chart %s has more than one Document_Verified value (%s); using %s",
                chart_id,
                ", ".join(sorted(statuses)),
                sorted(statuses)[0],
            )
        final_status = sorted(statuses)[0] if statuses else ""

        pages_checked = len(chart_rows)
        declared = as_int(chart_rows[0].get("Total_Page_Count"), pages_checked)
        if declared and declared != pages_checked:
            logger.warning(
                "chart %s reports %s pages but %s row(s) are present; counting the rows",
                chart_id,
                declared,
                pages_checked,
            )
        pages_matched = sum(1 for row in chart_rows if as_bool(row.get("Page_Verified")))
        wrong_pages = sum(1 for row in chart_rows if as_bool(row.get("Page_Detection_InCorrect")))
        scores = confidences.get(chart_id, [])

        rows.append(
            {
                "id": new_id(),
                "chart_id": chart_id,
                "final_status": final_status,
                "matched_member_id": member["id"],
                "matched_name": _matched_name(chart_rows),
                "confidence": round(sum(scores) / len(scores), 4) if scores else 0.0,
                "pages_matched": pages_matched,
                "pages_checked": pages_checked,
                "decision_reason": decision_reason(
                    final_status, pages_checked, pages_matched, wrong_pages
                ),
                "decided_at": utc_now(),
            }
        )
    return rows


# --- entry point -------------------------------------------------------------


def build(
    member_list_source: Path,
    member_verification_source: Path,
    ner_source: Path | None,
    out_dir: Path,
) -> dict[str, Path]:
    system_rows = read_csv(member_list_source)
    verification_rows = read_csv(member_verification_source)

    if ner_source is None:
        # Without the NER CSV an NER value has no score, and writing 0 for it
        # would read as "nothing detected". Only allowed when the batch has no
        # NER detections to score.
        ner_detections = sum(
            1
            for row in verification_rows
            for column in ("Detection_Source_Name", "Detection_Source_DOB", "Detection_Source_MemberID")
            if clean(row.get(column)).casefold() == NER_SOURCE_NAME
        )
        if ner_detections:
            raise SystemExit(
                f"{member_verification_source.name} has {ner_detections} NER-detected "
                "value(s), so --ner-source is needed to put a confidence on them. "
                "Pass the ner_{model}.csv from the same batch."
            )
        logger.info("no NER detections in this batch; ner_source not needed")
        ner_index: dict[tuple[str, str], list[dict]] = {}
    else:
        ner_index = index_ner(read_csv(ner_source))

    member_list = build_member_list(system_rows)
    if not member_list:
        raise SystemExit(f"no usable rows in {member_list_source}")
    extraction_rows = build_extraction_results(member_list, verification_rows, ner_index)
    summary_rows = build_summary(member_list, verification_rows, extraction_rows)

    written = {
        MEMBER_LIST_NAME: out_dir / MEMBER_LIST_NAME,
        EXTRACTION_RESULTS_NAME: out_dir / EXTRACTION_RESULTS_NAME,
        SUMMARY_NAME: out_dir / SUMMARY_NAME,
    }
    write_csv(written[MEMBER_LIST_NAME], MEMBER_LIST_COLUMNS, member_list)
    write_csv(written[EXTRACTION_RESULTS_NAME], EXTRACTION_COLUMNS, extraction_rows)
    write_csv(written[SUMMARY_NAME], SUMMARY_COLUMNS, summary_rows)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the three member CSVs")
    parser.add_argument(
        "--member-list-source",
        default=str(MEMBER_LIST_SOURCE),
        help="system data CSV (RecordId, DummyFirstName, DummyLastName, DummyDOB, MemberID)",
    )
    parser.add_argument(
        "--member-verification-source",
        default=str(MEMBER_VERIFICATION_SOURCE or ""),
        help="member_verification_{model}.csv from a batch",
    )
    parser.add_argument(
        "--ner-source",
        default=str(NER_SOURCE or ""),
        help="ner_{model}.csv from the same batch (for NER confidences)",
    )
    parser.add_argument(
        "--out",
        default=str(OUTPUT_DIR),
        help="output directory for the three CSVs",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.member_verification_source:
        raise SystemExit(
            "no member_verification_source: set MEMBER_VERIFICATION_SOURCE at the top "
            "of this file, or pass --member-verification-source"
        )
    if not args.out:
        raise SystemExit(
            "no output directory: set OUTPUT_DIR at the top of this file, or pass --out"
        )
    verification_source = Path(args.member_verification_source)
    ner_source = Path(args.ner_source) if args.ner_source else None
    out_dir = Path(args.out)

    written = build(
        Path(args.member_list_source),
        verification_source,
        ner_source,
        out_dir,
    )
    print("\nwrote:")
    for path in written.values():
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
