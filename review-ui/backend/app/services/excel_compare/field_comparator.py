import re

import pandas as pd

from .normalizer import (
    clean_text,
    clean_date,
    clean_identifier,
    credentials_match,
    display_date,
    display_name,
    has_value,
    identifiers_match,
    names_match,
    normalize_yes_no,
    normalize_codable,
    normalize_labelled_yes_no,
    normalize_name,
    normalize_print_type,
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
    "Type (Printed/HW)",
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

# Ground truth may label these as present / absent (Yes / No, as
# page_ground_truth does) rather than with the value itself.
PRESENCE_FIELDS = {
    "Extracted Name",
    "Extracted DOB",
    "Member ID",
}

CODABLE_FIELDS = {
    "Codable / Non-Codable",
}

PRINT_TYPE_FIELDS = {
    "Type (Printed/HW)",
}

# The same words in any order are the same name; shown as display_name()
# on both sides ("Lisa, Anderson" reads "Lisa Anderson").
NAME_FIELDS = {
    "Extracted Name",
}

# Read as text, but a float-read "204568.0" is the ID 204568 -- in the
# comparison and in what is shown.
IDENTIFIER_FIELDS = {
    "Member ID",
    "Current Sequence",
    "Actual Sequence",
}


# Free-text category labels: a plural or a one-letter typo is the same label
# (2026-10-26, from the standalone ExcelCompare; its whole-phrase subset rule was not
# taken: "Note" would match "Operative Note").
TEXT_CLASSIFICATION_FIELDS = {
    "Page Type",
    "Page Subtype",
}

BLANK_OR_JUNK_FIELD = "Is Blank or Junk?"

# The chart view's ground truth for a blank / junk page: every field Skipped
# (2026-10-29, user). A Skipped field agrees with an algo that skipped the page too.
SKIPPED = "skipped"

# Labels that say "nothing to score" for a page type: not scored, as on the Accuracy tab.
_UNSCORED_TYPE_LABELS = {"", "-", "--", "—", "na", "n/a", "not available", "none"}

# A second algo value a field also matches (the pipeline's Extracted value beside its
# Final one), as the Accuracy tab takes either. Only the database Algo side has them.
ALTERNATE_PREFIX = "Extracted Value: "
ALTERNATE_FIELDS = {
    "DOS From",
    "DOS To",
    "Page Type",
    "Page Subtype",
    "Codable / Non-Codable",
}

UNVALIDATED_REASON = (
    "No comparison fields were available for this record. PDF ID + "
    "Page Number were matched, but no field-level validation was "
    "performed -- this is not evidence that field values agree."
)


def _raw_value(row, field_name):

    value = row.get(field_name, "")

    if pd.isna(value):
        return ""

    if field_name in IDENTIFIER_FIELDS:
        return clean_identifier(value)

    if field_name in NAME_FIELDS:
        return display_name(value)

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
        return normalize_labelled_yes_no(value)

    if field_name in CODABLE_FIELDS:
        return normalize_codable(value)

    if field_name in PRINT_TYPE_FIELDS:
        return normalize_print_type(value)

    if field_name in NAME_FIELDS:
        return normalize_name(value)

    return clean_text(value)


# Values with no content: never matched as a plural or typo of real text.
_BLANK_LIKE_TEXT_VALUES = {"", "-", "--", "n/a", "na"}
_WORD_TOKEN_RE = re.compile(r"[a-z0-9]+")
# A typo is only forgiven on words this long or longer ("ED" vs "ID" never is).
_MIN_TYPO_TOLERANT_TOKEN_LENGTH = 5


def _is_one_edit_away(token_a, token_b):
    """Exactly one inserted, deleted or substituted character."""

    len_a, len_b = len(token_a), len(token_b)

    if abs(len_a - len_b) > 1:
        return False

    if len_a == len_b:
        return sum(1 for x, y in zip(token_a, token_b) if x != y) == 1

    shorter, longer = (token_a, token_b) if len_a < len_b else (token_b, token_a)
    i = j = 0
    skipped = False

    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
            continue
        if skipped:
            return False
        skipped = True
        j += 1

    return True


def _tokens_equivalent(token_a, token_b):

    if token_a == token_b:
        return True

    if min(len(token_a), len(token_b)) < _MIN_TYPO_TOLERANT_TOKEN_LENGTH:
        return False

    return _is_one_edit_away(token_a, token_b)


def compare_text_classification(gt_raw, system_raw):
    """
    Page Type / Page Subtype: equal after clean_text; else, when neither side is
    blank-like, the same label with or without a trailing "s" ("Procedure Note" /
    "Procedure Notes"), or the same words where a word of 5+ letters may differ by
    one character ("Imaging" / "Imahing"). Unrelated labels never match. Symmetric.
    """

    gt_clean = clean_text(gt_raw)
    system_clean = clean_text(system_raw)

    if gt_clean == system_clean:
        return True

    if gt_clean in _BLANK_LIKE_TEXT_VALUES or system_clean in _BLANK_LIKE_TEXT_VALUES:
        return False

    gt_tokens = _WORD_TOKEN_RE.findall(gt_clean)
    system_tokens = _WORD_TOKEN_RE.findall(system_clean)

    if not gt_tokens or not system_tokens:
        return False

    gt_norm, system_norm = " ".join(gt_tokens), " ".join(system_tokens)

    if gt_norm == system_norm or gt_norm + "s" == system_norm or system_norm + "s" == gt_norm:
        return True

    return len(gt_tokens) == len(system_tokens) and all(
        _tokens_equivalent(a, b) for a, b in zip(gt_tokens, system_tokens)
    )


def classify_blank_or_junk(row):
    """
    "blank" / "junk" / None from Is Blank or Junk?: "Yes (Blank)" or "Blank" is blank,
    "Yes (Junk)" or "Junk" is junk. A plain "Yes", "No", "-", N/A or an empty cell is
    neither -- nothing is guessed.
    """

    text = clean_text(_raw_value(row, BLANK_OR_JUNK_FIELD))
    match = re.fullmatch(r"(?:yes\s*\(\s*)?(blank|junk)\s*\)?", text)

    return match.group(1) if match else None


def says_blank_or_junk(row):
    """The row calls the page blank or junk: Is Blank or Junk? is Yes (any note), Blank,
    Junk or Skipped, or the Page Type is Blank / Junk."""

    if classify_blank_or_junk(row) is not None:
        return True
    text = clean_text(_raw_value(row, BLANK_OR_JUNK_FIELD))
    if text == SKIPPED or normalize_labelled_yes_no(text) == "yes":
        return True
    return clean_text(_raw_value(row, "Page Type")) in ("blank", "junk")


def mutual_blank_or_junk(gt_row, system_row):
    """
    "blank" or "junk" when both sides call the page blank or junk: such a page is
    excluded from field-level evaluation (2026-10-26, from the standalone
    ExcelCompare). Blank against Junk counts as both junk-like, as on the Accuracy tab
    (2026-10-29, user: "junk things should be Skipped, same as the UI"). Either side
    neither is evaluated as usual.
    """

    if not (says_blank_or_junk(gt_row) and says_blank_or_junk(system_row)):
        return None

    return classify_blank_or_junk(gt_row) or classify_blank_or_junk(system_row) or "junk"


def _alternate(row, field_name):
    """The algo row's second value for a field ("" when none)."""

    return _raw_value(row, ALTERNATE_PREFIX + field_name) if field_name in ALTERNATE_FIELDS else ""


def _values_match(field_name, gt_raw, system_raw, gt_normalized, system_normalized):
    if field_name in CREDENTIAL_FIELDS:
        return credentials_match(gt_raw, system_raw)
    if field_name in TEXT_CLASSIFICATION_FIELDS:
        return compare_text_classification(gt_raw, system_raw)
    if field_name in NAME_FIELDS:
        return gt_normalized == system_normalized or names_match(gt_raw, system_raw)
    if field_name == "Member ID":
        return identifiers_match(gt_raw, system_raw)
    return gt_normalized == system_normalized


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
    system_skipped = says_blank_or_junk(system_row)

    for field_name in fields_to_compare:

        gt_raw = _raw_value(gt_row, field_name)
        system_raw = _raw_value(system_row, field_name)

        gt_normalized = normalize_field_value(field_name, gt_raw)
        system_normalized = normalize_field_value(field_name, system_raw)
        presence = normalize_yes_no(gt_raw) if field_name in PRESENCE_FIELDS else ""

        if not gt_raw:
            # Not labelled: ground truth says nothing, so it is not an error.
            # ("-" or "N/A" is a label -- "none" -- and is compared.)
            is_match = True
        elif clean_text(gt_raw) == SKIPPED:
            # The reviewer skipped the page as blank / junk: right when the algo did too.
            is_match = system_skipped
        elif field_name in TEXT_CLASSIFICATION_FIELDS and clean_text(gt_raw) in _UNSCORED_TYPE_LABELS:
            # "-" / NA / Not Available page type: not scored, as on the Accuracy tab.
            is_match = True
        elif presence in ("yes", "no"):
            # "Is a member ID on the page?" -- found or not, not the value.
            # An algo "-" or "N/A" is "none found", as a blank is.
            is_match = (presence == "yes") == has_value(system_raw)
        elif field_name in PRESENCE_FIELDS and clean_text(gt_raw) == "not found":
            # Not Found: none on the page.
            is_match = not has_value(system_raw)
        else:
            is_match = _values_match(field_name, gt_raw, system_raw, gt_normalized, system_normalized)
            alternate = _alternate(system_row, field_name)
            if not is_match and alternate:
                # The pipeline's Extracted value is right: a match, as on the Accuracy tab.
                is_match = _values_match(
                    field_name, gt_raw, alternate, gt_normalized, normalize_field_value(field_name, alternate)
                )

        if field_name in DATE_FIELDS:
            gt_raw = display_date(gt_raw)
            system_raw = display_date(system_raw)

        # Both sides are shown when they read differently but still count as a
        # match: credentials ("MD" / "MD, PhD"), a Yes / No presence label, or
        # an unlabelled field next to an algo value. A matching name is one
        # value: the ground truth's, normalized.
        differs = (
            field_name in CREDENTIAL_FIELDS
            or field_name in NAME_FIELDS
            or field_name == "Member ID"
            or field_name in ALTERNATE_FIELDS
            or presence in ("yes", "no")
            or not gt_raw
        ) and normalize_field_value(field_name, gt_raw) != normalize_field_value(field_name, system_raw)

        if is_match and not differs:
            display_value = gt_raw
        else:
            display_value = f"GT: {gt_raw} | Algo: {system_raw}"

        field_results[field_name] = {
            "match": is_match,
            "display": display_value,
            "gt_raw": gt_raw,
            "system_raw": system_raw,
        }

    return field_results


def determine_status(field_results):
    """
    UNVALIDATED if there were zero fields to compare -- a record
    matched by key alone is NOT evidence that its fields agree,
    so this must never collapse into CORRECT.

    Otherwise: CORRECT only if every compared field matches;
    ERROR otherwise. This never considers association/matching
    evidence.
    """

    if not field_results:
        return "UNVALIDATED"

    if all(result["match"] for result in field_results.values()):
        return "CORRECT"

    return "ERROR"


def build_reason(status, field_results):
    """
    Human-readable explanation of the Status.
    """

    if status == "UNVALIDATED":
        return UNVALIDATED_REASON

    if status == "CORRECT":
        return "All fields match."

    mismatched_fields = [
        field_name
        for field_name, result in field_results.items()
        if not result["match"]
    ]

    return "Mismatched fields: " + ", ".join(mismatched_fields)
