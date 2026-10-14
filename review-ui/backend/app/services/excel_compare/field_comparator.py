import pandas as pd

from .normalizer import (
    clean_text,
    clean_date,
    credentials_match,
    display_date,
    normalize_yes_no,
    normalize_codable,
)


# =============================================================
# COMPARISON FIELDS
#
# ACTIVE_COMPARISON_FIELDS are the fields actually compared
# between a matched Ground Truth row and Algo Generated row
# (subject also to being present in both files -- see
# generate_report.py for the per-run "present in both" filter).
#
# ALWAYS_EXEMPT_FIELDS are recognized canonical columns that are
# never compared, by design: they hold subjective/judgment-call
# values where an automated match/mismatch wouldn't produce a
# meaningful signal. They are still recognized by header
# normalization and always listed in the exemption notice.
#
# PDF ID and Page Number are matching keys, not comparison
# fields, and never appear in either list.
# =============================================================

ACTIVE_COMPARISON_FIELDS = [
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
    "Provider Credentials",
    "Current Sequence",
    "Actual Sequence",
]

ALWAYS_EXEMPT_FIELDS = [
    "Type (Printed/HW)",
    "Handwritten %",
    "Visibility",
    "Quality",
    "Orient. (CW)",
    "Tilt",
    "Mirrored",
]

ALWAYS_EXEMPT_REASON = (
    "Subjective field; comparison would not produce meaningful insights."
)

# All canonical fields in this family, recognized by header
# normalization regardless of whether they end up compared.
COMPARISON_FIELDS = ACTIVE_COMPARISON_FIELDS + ALWAYS_EXEMPT_FIELDS


DATE_FIELDS = {
    "Extracted DOB",
    "DOS From",
    "DOS To",
}

YES_NO_FIELDS = {
    "Is Blank or Junk?",
    "Duplicate",
}

CREDENTIAL_FIELDS = {
    "Provider Credentials",
}

CODABLE_FIELDS = {
    "Codable / Non-Codable",
}


def _raw_value(row, field_name):

    value = row.get(field_name, "")

    if pd.isna(value):
        return ""

    return str(value).strip()


def normalize_field_value(field_name, value):
    """
    Normalize a single field's value for comparison purposes
    only. The raw value shown in the report is never replaced
    by this normalized form.
    """

    if field_name in DATE_FIELDS:
        return clean_date(value)

    if field_name in YES_NO_FIELDS:
        return normalize_yes_no(value)

    if field_name in CODABLE_FIELDS:
        return normalize_codable(value)

    return clean_text(value)


def compare_fields(gt_row, system_row, fields_to_compare=None):
    """
    Compare a matched Ground Truth row against its System
    Generated row across the given fields (defaults to every
    active comparison field).

    `fields_to_compare` lets the caller pass the subset of
    ACTIVE_COMPARISON_FIELDS that is actually present in both
    files for this run -- fields missing from either file are
    exempted upstream and never reach this function.

    This is the ONLY place correctness is decided. It is run
    after matching and does not know or care how the two rows
    were determined to correspond.
    """

    if fields_to_compare is None:
        fields_to_compare = ACTIVE_COMPARISON_FIELDS

    field_results = {}

    for field_name in fields_to_compare:

        gt_raw = _raw_value(gt_row, field_name)
        system_raw = _raw_value(system_row, field_name)

        gt_normalized = normalize_field_value(field_name, gt_raw)
        system_normalized = normalize_field_value(field_name, system_raw)

        if field_name in CREDENTIAL_FIELDS:
            is_match = credentials_match(gt_raw, system_raw)
        else:
            is_match = gt_normalized == system_normalized

        if field_name in DATE_FIELDS:
            gt_raw = display_date(gt_raw)
            system_raw = display_date(system_raw)

        # Credentials can match while written differently ("MD" / "MD, PhD"):
        # both sides are shown then, so the reader sees what each one said.
        differs = field_name in CREDENTIAL_FIELDS and (
            clean_text(gt_raw) != clean_text(system_raw)
        )

        if is_match and not differs:
            display_value = gt_raw
        else:
            display_value = f"GT: {gt_raw} | Algo: {system_raw}"

        field_results[field_name] = {
            "match": is_match,
            "display": display_value,
        }

    return field_results


def determine_status(field_results):
    """
    CORRECT only if every compared field matches; otherwise
    ERROR. This never considers association/matching evidence.
    """

    if all(result["match"] for result in field_results.values()):
        return "CORRECT"

    return "ERROR"


def build_reason(status, field_results):
    """
    Human-readable explanation of the Status.
    """

    if status == "CORRECT":
        return "All fields match."

    mismatched_fields = [
        field_name
        for field_name, result in field_results.items()
        if not result["match"]
    ]

    return "Mismatched fields: " + ", ".join(mismatched_fields)
