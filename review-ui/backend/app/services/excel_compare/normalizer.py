import pandas as pd
import re


def clean_text(value):
    """
    Standardize text for comparison without changing
    the original value stored in the source file.
    """

    if pd.isna(value):
        return ""

    value = str(value).strip().lower()

    # Normalize whitespace
    value = re.sub(r"\s+", " ", value)

    return value


_NO_DATE_VALUES = {"", "-", "--", "na", "n/a", "none", "null", "nil"}

# What an extracted value reads when nothing was found.
_NOTHING_FOUND = _NO_DATE_VALUES | {"not found"}


def has_value(value):
    """True unless the cell is blank or says nothing was found ("-", N/A, ...)."""

    return clean_text(value) not in _NOTHING_FOUND


def clean_date(value):
    """
    Normalize dates into YYYY-MM-DD where possible. "-", "N/A" and blank
    all mean "no date" and compare equal.
    """

    if pd.isna(value):
        return ""

    if str(value).strip().lower() in _NO_DATE_VALUES:
        return ""

    parsed = pd.to_datetime(value, errors="coerce")

    if pd.isna(parsed):
        return clean_text(value)

    return parsed.strftime("%Y-%m-%d")


def display_date(value):
    """
    A date as MM/DD/YYYY for the screen and the report, so the two
    files' spellings (03/15/2024, 2024-03-15) read alike. Anything
    that is not a date ("-", blank, free text) is shown as given.
    """

    if pd.isna(value):
        return ""

    text = str(value).strip()

    if not text:
        return ""

    parsed = pd.to_datetime(text, errors="coerce")

    if pd.isna(parsed):
        return text

    return parsed.strftime("%m/%d/%Y")


# =============================================================
# PROVIDER CREDENTIALS
#
# As the Accuracy tab scores them: punctuation and order ignored
# ("M.D., Ph.D." = "PhD MD"), and sharing some credentials counts
# as a match. NA / None / blank mean "no credentials".
# =============================================================

_NO_CREDENTIALS = {"", "na", "n/a", "none", "not found", "null", "nil", "-"}


def credential_tokens(value):
    """ "M.D., Ph.D." -> {"MD", "PHD"}; nothing for NA / blank."""

    if pd.isna(value):
        return frozenset()

    text = str(value).strip()

    if text.lower() in _NO_CREDENTIALS:
        return frozenset()

    return frozenset(
        token
        for token in re.split(r"[^A-Z0-9]+", text.upper().replace(".", ""))
        if token
    )


def credentials_match(gt_value, system_value):
    gt_tokens = credential_tokens(gt_value)
    system_tokens = credential_tokens(system_value)

    if not gt_tokens or not system_tokens:
        return gt_tokens == system_tokens

    return bool(gt_tokens & system_tokens)


# =============================================================
# YES/NO NORMALIZATION
#
# Recognizes explicit equivalent spellings of Yes/No only.
# Blank, "-", and "N/A"-style values are treated as their own
# not-applicable state (mapped to "") and are never coerced into
# "No" -- they only match other blank/not-applicable values.
# =============================================================

_YES_VALUES = {"yes", "y", "true", "t", "1"}
_NO_VALUES = {"no", "n", "false", "f", "0"}
_NOT_APPLICABLE_VALUES = {"na", "none", "null", "nil", "notapplicable"}


def normalize_labelled_yes_no(value):
    """
    Yes / No with a note in brackets reads as the Yes / No alone: "Yes (Junk)",
    "Yes (Blank)" and the Duplicate column's "Duplicate (To Page 4)" are all "yes", so a duplicate matches a
    page labelled junk; "Yes (To Page 4)" is "yes". Anything else goes through
    normalize_yes_no.
    """

    text = clean_text(value)
    match = re.fullmatch(r"(yes|no)\s*\(.*\)", text)

    if match:
        return match.group(1)

    # The screen's Duplicate column: "Duplicate (To Page 4)" is a Yes.
    if re.fullmatch(r"duplicate(\s*\(.*\))?", text):
        return "yes"

    return normalize_yes_no(value)


def normalize_yes_no(value):
    """
    Normalize equivalent Yes/No representations for comparison
    purposes only. The raw value shown in the report is never
    replaced by this normalized form.
    """

    text = clean_text(value)

    if text == "":
        return ""

    # Collapse punctuation/spacing (e.g. "N/A", "N.A.", "n a")
    # so equivalent not-applicable spellings compare equal.
    collapsed = re.sub(r"[^a-z0-9]+", "", text)

    if collapsed in _NOT_APPLICABLE_VALUES or collapsed == "":
        return ""

    if collapsed in _YES_VALUES:
        return "yes"

    if collapsed in _NO_VALUES:
        return "no"

    # Unrecognized spelling: leave normalized text as-is rather
    # than guessing via fuzzy matching.
    return text


# =============================================================
# CODABLE / NON CODABLE NORMALIZATION
#
# Recognizes explicit equivalent spellings/typos of
# "Codable" and "Non Codable" only.
# =============================================================

_CODABLE_ALIASES = {
    "codable": "codable",
    "codeable": "codable",
    "non codable": "non codable",
    "non codeable": "non codable",
    "noncodable": "non codable",
    "noncodeable": "non codable",
    "not codable": "non codable",
    "not codeable": "non codable",
}


def normalize_codable(value):
    """
    Normalize equivalent Codable/Non Codable representations for
    comparison purposes only. The raw value shown in the report
    is never replaced by this normalized form.
    """

    text = clean_text(value)

    if text == "":
        return ""

    collapsed = re.sub(r"[^a-z0-9]+", " ", text)
    collapsed = re.sub(r"\s+", " ", collapsed).strip()

    return _CODABLE_ALIASES.get(collapsed, collapsed)

# =============================================================
# PRINTED / HANDWRITTEN NORMALIZATION
#
# The sheet's "Type (Printed/HW)" spells handwritten as HW. Only
# these explicit spellings are folded; anything else (Mixed,
# Form, ...) compares as its own text.
# =============================================================

_PRINT_TYPE_ALIASES = {
    "printed": "printed",
    "print": "printed",
    "hw": "handwritten",
    "handwritten": "handwritten",
    "handwriting": "handwritten",
    "hand written": "handwritten",
}


def normalize_print_type(value):
    """
    Normalize equivalent Printed / Handwritten representations for
    comparison purposes only.
    """

    text = clean_text(value)
    collapsed = re.sub(r"[^a-z0-9]+", " ", text).strip()

    return _PRINT_TYPE_ALIASES.get(collapsed, collapsed)


# =============================================================
# NAME NORMALIZATION
#
# The same words in any order, case or punctuation are the same
# name: "Lisa, Anderson" = "Anderson Lisa" = "LISA ANDERSON". A
# different or missing word (an initial, a middle name) is not.
# =============================================================

def normalize_name(value):
    """A name's words, sorted, for comparison purposes only."""

    return " ".join(sorted(re.findall(r"[a-z0-9]+", clean_text(value))))


def display_name(value):
    """
    A name as Manual Review shows it, on both sides alike: commas and
    other punctuation dropped, spaces collapsed, Title Case, words in
    their own order. Apostrophes and hyphens inside a word stay
    ("O'Brien", "Smith-Jones").
    """

    if pd.isna(value):
        return ""

    # "-" / N/A say "no name": kept as written, never blanked into "unlabelled".
    if not has_value(value):
        return str(value).strip()

    text = re.sub(r"[^\w'\-]+", " ", str(value))
    words = [word.strip("'-") for word in text.split()]

    return " ".join(word.title() for word in words if word)


# =============================================================
# IDENTIFIER NORMALIZATION
#
# A numeric column with a blank cell is read as floats, so an ID
# 204568 arrives as "204568.0". Only that trailing ".0" is dropped;
# anything else (leading zeros, letters) is kept as written.
# =============================================================

_FLOAT_INTEGER = re.compile(r"[+-]?\d+\.0+")


def clean_identifier(value):
    """ "204568.0" -> "204568"; any other value unchanged."""

    text = str(value).strip()

    if _FLOAT_INTEGER.fullmatch(text):
        return text.split(".", 1)[0]

    return text
