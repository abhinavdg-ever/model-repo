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
    "is duplicate": "Duplicate",
    "member name": "Extracted Name",
    "member dob": "Extracted DOB",
    # The legacy 9-field sheet's Yes / No labels: the same presence
    # reading as page_ground_truth's (field_comparator.PRESENCE_FIELDS).
    "member name yes no": "Extracted Name",
    "member dob yes no": "Extracted DOB",
    "tilt y n": "Tilt",
    "dup": "Duplicate",
    "page type": "Page Type",
    "encounter type": "Page Type",
    "encounter": "Page Type",
    "page subtype": "Page Subtype",
    "pagesubtype": "Page Subtype",
    "codable non codable": "Codable / Non-Codable",
    "codeable": "Codable / Non-Codable",
    "codable": "Codable / Non-Codable",
    "codable or non codable": "Codable / Non-Codable",
    "codable status": "Codable / Non-Codable",
    "current sequence": "Current Sequence",
    "current seq": "Current Sequence",
    "actual sequence": "Actual Sequence",
    "actual seq": "Actual Sequence",
    "sequence": "Actual Sequence",
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


# When several headers in one file name the same canonical column, these
# spellings win (then an exact canonical header, then file order).
MERGE_PRIORITY = {
    "Page Type": ["encounter type", "encounter"],
}

# A file with one of these columns uses it alone: the others are dropped, not
# used as a fallback. An offline ground-truth CSV's Page Type is its
# encounter_type; its page_type is ignored.
EXCLUSIVE = {
    "Page Type": ["encounter type", "encounter"],
}


def _merge_order(canonical, columns):
    preferred = [_normalize_for_matching(alias) for alias in MERGE_PRIORITY.get(canonical, [])]
    exact = _normalize_for_matching(canonical)

    def rank(position_and_column):
        position, column = position_and_column
        key = _normalize_for_matching(column)
        if key in preferred:
            return (0, preferred.index(key), position)
        if key == exact:
            return (1, 0, position)
        return (2, 0, position)

    return [column for _, column in sorted(enumerate(columns), key=rank)]


def normalize_headers(df):
    """
    Return a copy of df with recognized headers renamed to their
    canonical form. Unrecognized headers are left untouched.

    Several headers naming the same canonical column (an export with
    both page_type and encounter_type) become one column holding, per
    row, the first non-blank value in MERGE_PRIORITY order -- never two
    columns with one name, which would make every row lookup ambiguous.

    This never modifies the caller's DataFrame (and therefore
    never touches the uploaded file it was read from).
    """

    import pandas as pd

    rename_map = build_header_rename_map(df.columns)

    if not rename_map:
        return _with_signature_credentials(_with_blank_or_junk(df))

    sources = {}
    for column in df.columns:
        canonical = rename_map.get(column, column)
        sources.setdefault(canonical, []).append(column)

    merged = {}
    for canonical, columns in sources.items():
        only = [_normalize_for_matching(alias) for alias in EXCLUSIVE.get(canonical, [])]
        chosen = [column for column in columns if _normalize_for_matching(column) in only]
        ordered = _merge_order(canonical, chosen or columns)
        value = df[ordered[0]]
        for column in ordered[1:]:
            blank = value.isna() | (value.astype(str).str.strip() == "")
            value = value.mask(blank, df[column])
        merged[canonical] = value

    frame = pd.DataFrame(merged, index=df.index)
    return _with_signature_credentials(_with_blank_or_junk(frame))


# Headers no canonical column is named after, but that one is built from.
DERIVED_SOURCES = {"blank page", "junk page", "provider signature"}


def unrecognized_columns(frame):
    """
    Headers of an already-normalized frame that nothing reads: no
    canonical column, alias or derived column uses them. Listed in
    the master summary so a misspelt header is seen, not silently
    left out of the comparison.
    """

    return [
        str(column)
        for column in frame.columns
        if _normalize_for_matching(column) not in _HEADER_LOOKUP
        and _normalize_for_matching(column) not in DERIVED_SOURCES
    ]


def _with_blank_or_junk(frame):
    """A page_ground_truth export has blank_page / junk_page, not one
    "Is Blank or Junk?" column: derive it as database ground truth does."""

    from .database_ground import BLANK_OR_JUNK, _blank_or_junk

    if BLANK_OR_JUNK in frame.columns:
        return frame

    columns = {
        _normalize_for_matching(column): column for column in frame.columns
    }
    blank, junk = columns.get("blank page"), columns.get("junk page")

    if blank is None and junk is None:
        return frame

    def cell(row, column):
        value = row[column] if column is not None else None
        return None if value is None or value != value else value  # NaN -> None

    frame = frame.copy()
    frame[BLANK_OR_JUNK] = [
        _blank_or_junk({"blank_page": cell(row, blank), "junk_page": cell(row, junk)})
        for _, row in frame.iterrows()
    ]
    return frame


def _with_signature_credentials(frame):
    """The legacy sheet's Provider Signature holds credentials (MD, DO): read
    it as Provider Credentials, as database ground truth reads
    provider_signature. Yes / No signatures carry no credentials, so a column
    of only those adds nothing."""

    from .database_ground import CREDENTIALS, _credentials

    if CREDENTIALS in frame.columns:
        return frame

    column = next(
        (c for c in frame.columns if _normalize_for_matching(c) == "provider signature"),
        None,
    )

    if column is None:
        return frame

    values = [
        _credentials({"provider_signature": None if value != value else value})  # NaN -> None
        for value in frame[column]
    ]

    if not any(values):
        return frame

    frame = frame.copy()
    frame[CREDENTIALS] = values
    return frame
