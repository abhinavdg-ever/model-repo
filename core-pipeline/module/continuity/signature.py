"""A signature block on a page, not the word appearing inside a sentence."""
from __future__ import annotations

import csv
import re

_SIGNATURE = re.compile(
    r"(?i)(?:"
    r"electronically\s+signed|"
    r"digitally\s+signed|"
    r"electronic(?:ally)?\s+signature|"
    r"provider\s+signature|"
    r"physician\s+signature|"
    r"signature\s+of\s+(?:the\s+)?(?:provider|physician|attending)|"
    r"signed\s+by\b|"
    r"authenticated\s+by\b|"
    r"(?:^|\n)\s*signature\s*[:\-]"
    r")"
)


def has_signature_section(text: str) -> bool:
    return bool(_SIGNATURE.search(text or ""))


def signed_page_names(chart_name: str) -> set[str]:
    """Page names whose key/value extraction row says a signature is present."""
    from db.paths import imaging_csv

    path = imaging_csv(chart_name, "provider_signature")
    if not path.is_file():
        return set()
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            str(row.get("page_name") or "").strip()
            for row in csv.DictReader(handle)
            if str(row.get("signature_present") or "").strip().casefold() == "y"
            and str(row.get("page_name") or "").strip()
        }
