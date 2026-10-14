import re

from .record_matcher import KEY_COLUMNS
from .field_comparator import COMPARISON_FIELDS


# =============================================================
# CANONICAL COLUMNS
#
# Reuses the canonical names already defined for matching and
# comparison elsewhere in the project. This module never
# introduces new schema -- it only recognizes real-world header
# variations of these exact names.
# =============================================================

CANONICAL_COLUMNS = KEY_COLUMNS + COMPARISON_FIELDS


# =============================================================
# EXPLICIT ALIASES
#
# Known variations and common typos that don't collapse onto a
# canonical header through whitespace/case/punctuation
# normalization alone. Keys are matched after the same
# normalization applied to real headers, so exact casing/
# spacing here doesn't matter.
# =============================================================

COLUMN_ALIASES = {
    "pdf id": "PDF ID",
    "pdfid": "PDF ID",
    "pdf number": "PDF ID",
    "pdf no": "PDF ID",
    "pdf#": "PDF ID",
    "chart": "PDF ID",
    "chart name": "PDF ID",
    "chart id": "PDF ID",
    "chartname": "PDF ID",
    "page number": "Page Number",
    "page no": "Page Number",
    "page num": "Page Number",
    "pg number": "Page Number",
    "pg no": "Page Number",
    "page#": "Page Number",
    "extracted name": "Extracted Name",
    "ext name": "Extracted Name",
    "name": "Extracted Name",
    "extracted dob": "Extracted DOB",
    "ext dob": "Extracted DOB",
    "extracted date of birth": "Extracted DOB",
    "dob": "Extracted DOB",
    "member id": "Member ID",
    "member number": "Member ID",
    "memberid": "Member ID",
    "type printed hw": "Type (Printed/HW)",
    "type printed handwritten": "Type (Printed/HW)",
    "printed hw": "Type (Printed/HW)",
    "handwritten": "Handwritten %",
    "handwritten percent": "Handwritten %",
    "handwritten percentage": "Handwritten %",
    "visibility": "Visibility",
    "quality": "Quality",
    "orientation cw": "Orient. (CW)",
    "orientation": "Orient. (CW)",
    "orient cw": "Orient. (CW)",
    "tilt": "Tilt",
    "mirrored": "Mirrored",
    "mirror": "Mirrored",
    "dos from": "DOS From",
    "date of service from": "DOS From",
    "service date from": "DOS From",
    "dos to": "DOS To",
    "date of service to": "DOS To",
    "service date to": "DOS To",
    "blank or junk": "Is Blank or Junk?",
    "is blank or junk": "Is Blank or Junk?",
    "blank junk": "Is Blank or Junk?",
    "provider credentials": "Provider Credentials",
    "provider credential": "Provider Credentials",
    "credentials": "Provider Credentials",
    "credential": "Provider Credentials",
    "duplicate": "Duplicate",
    "dup": "Duplicate",
    "page type": "Page Type",
    "encounter type": "Page Type",
    "encounter": "Page Type",
    "page subtype": "Page Subtype",
    "pagesubtype": "Page Subtype",
    "codable non codable": "Codable / Non-Codable",
    "codable or non codable": "Codable / Non-Codable",
    "codable status": "Codable / Non-Codable",
    "current sequence": "Current Sequence",
    "current seq": "Current Sequence",
    "actual sequence": "Actual Sequence",
    "actual seq": "Actual Sequence",
}


def _normalize_for_matching(header):
    """
    Reduce a header to a whitespace/case/punctuation-insensitive
    key for comparison. Any run of non-alphanumeric characters
    (spaces, underscores, hyphens, slashes, parentheses, periods,
    ...) collapses to a single space.
    """

    text = str(header).strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text


def _build_lookup():

    lookup = {}

    for canonical in CANONICAL_COLUMNS:
        lookup[_normalize_for_matching(canonical)] = canonical

    for alias, canonical in COLUMN_ALIASES.items():
        key = _normalize_for_matching(alias)
        lookup.setdefault(key, canonical)

    return lookup


_HEADER_LOOKUP = _build_lookup()


def build_header_rename_map(columns):
    """
    Map original column headers to their canonical form using
    whitespace/case/punctuation-insensitive matching plus
    explicit aliases for known variations and typos.

    Headers with no recognized canonical match are left out of
    the map entirely -- unexpected columns are never guessed at
    or silently coerced.
    """

    rename_map = {}

    for column in columns:

        key = _normalize_for_matching(column)
        canonical = _HEADER_LOOKUP.get(key)

        if canonical is not None and canonical != column:
            rename_map[column] = canonical

    return rename_map


def normalize_headers(df):
    """
    Return a copy of df with recognized headers renamed to their
    canonical form. Unrecognized headers are left untouched.

    This never modifies the caller's DataFrame (and therefore
    never touches the uploaded file it was read from) -- it
    returns a new object via pandas' rename().
    """

    rename_map = build_header_rename_map(df.columns)

    if not rename_map:
        return df

    return df.rename(columns=rename_map)
