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


def clean_date(value):
    """
    Normalize dates into YYYY-MM-DD where possible.
    """

    if pd.isna(value):
        return ""

    parsed = pd.to_datetime(value, errors="coerce")

    if pd.isna(parsed):
        return clean_text(value)

    return parsed.strftime("%Y-%m-%d")


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
    "non codable": "non codable",
    "noncodable": "non codable",
    "not codable": "non codable",
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