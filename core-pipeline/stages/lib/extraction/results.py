"""Rows the extraction writes for provider signature and additional page details.

The signature columns follow the electronic-signature extractor
(Key, Region, Scale, Sentence, Ner_Text, ProviderName, SignatureDate, Score, Source).
Page number columns follow the printed-page extractor. Section headers stay the
JSON array already stored on the OCR page.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from .util.dates import canonical_date


def _text(hit: Optional[dict[str, Any]], name: str) -> Optional[str]:
    if not hit:
        return None
    value = str(hit.get(name) or "").strip()
    return value or None


def _iso_date(raw: Optional[str]) -> Optional[str]:
    """Printed signature date ('03/15/2024', '15 Mar 2024') as YYYY-MM-DD."""
    text = (raw or "").strip()
    if not text:
        return None
    iso = canonical_date(text)
    return iso if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso) else None


def _score(hit: Optional[dict[str, Any]]) -> Optional[float]:
    if not hit or hit.get("score") in (None, ""):
        return None
    try:
        return round(float(hit["score"]), 4)
    except (TypeError, ValueError):
        return None


def _as_hits(hit: Any) -> list[dict[str, Any]]:
    if hit is None:
        return []
    if isinstance(hit, list):
        return [item for item in hit if isinstance(item, dict)]
    if isinstance(hit, dict):
        return [hit]
    return []


# Credentials removed from the extracted provider name ("Jane Doe, RD" -> "Jane Doe").
DROPPED_CREDENTIALS = frozenset({"RD"})


def drop_credentials(name: Optional[str]) -> Optional[str]:
    """The provider name without any DROPPED_CREDENTIALS token (written in capitals,
    dots allowed: "RD", "R.D."). Other credentials and the name stay as printed."""
    if not name:
        return name
    pieces = []
    dropped = False
    for piece in name.split(","):
        tokens = piece.split()
        kept = [token for token in tokens if token.replace(".", "") not in DROPPED_CREDENTIALS]
        dropped = dropped or len(kept) != len(tokens)
        if kept:
            pieces.append(" ".join(kept))
    if not dropped:
        return name
    return ", ".join(pieces) or None


def signature_fields(
    hit: Any = None,
    provider_hits: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """The page's signature and its one provider name: the highest-scoring.

    ``hit`` is one signature or the page's selected signatures. Candidates are
    the provider names from those signatures, then the selected provider-name
    hits; the one with the highest ``score`` wins, ties (and unscored
    candidates) going to the earliest, so a signature's name beats a
    provider-name hit of equal score. ``signature_present`` stays false when
    the only names are provider hits and no signature was accepted.
    """
    hits = _as_hits(hit)
    primary = hits[0] if hits else None
    candidates = [
        (name, _score(item))
        for item, name in (
            [(item, _text(item, "provider_name")) for item in hits]
            + [(item, _text(item, "value")) for item in _as_hits(provider_hits)]
        )
        if name
    ]
    best = None
    for name, score in candidates:
        if best is None or (score if score is not None else -1.0) > best[1]:
            best = (name, score if score is not None else -1.0)
    printed_date = _text(primary, "signature_date")
    ner_text = _text(primary, "ner_text")
    present = any(
        _text(item, "provider_name") or _text(item, "signature_date") or _text(item, "ner_text")
        for item in hits
    )
    return {
        "signature_present": present,
        "signature_key": _text(primary, "key"),
        "region": _text(primary, "region"),
        "scale": _text(primary, "scale"),
        "sentence": _text(primary, "sentence"),
        "ner_text": ner_text,
        "provider_name": drop_credentials(best[0]) if best else None,
        "signature_date": _iso_date(printed_date),
        "confidence": _score(primary),
        "source": _text(primary, "source"),
    }


def additional_fields(
    hit: Optional[dict[str, Any]],
    section_headers: list[dict[str, Any]],
) -> dict[str, Any]:
    """Printed page number plus the section-header JSON for one page."""
    return {
        "page_number_key": _text(hit, "key"),
        "page_number_region": _text(hit, "region"),
        "page_number_sentence": _text(hit, "sentence"),
        "page_number_value": _text(hit, "value"),
        "printed_page_no": _text(hit, "page_no"),
        "printed_page_total": _text(hit, "page_total"),
        "confidence": _score(hit),
        "source": _text(hit, "source"),
        "section_headers": section_headers,
    }


def signature_csv_row(chart_name: str, page_name: str, page_number: Any, fields: dict[str, Any]) -> dict[str, Any]:
    row = {
        "chart_name": chart_name,
        "page_name": page_name,
        "page_number": page_number if page_number is not None else "",
    }
    for key, value in fields.items():
        if key == "signature_present":
            row[key] = "y" if value else "n"
        else:
            row[key] = "" if value is None else value
    return row


def additional_csv_row(chart_name: str, page_name: str, page_number: Any, fields: dict[str, Any]) -> dict[str, Any]:
    row = {
        "chart_name": chart_name,
        "page_name": page_name,
        "page_number": page_number if page_number is not None else "",
    }
    for key, value in fields.items():
        if key == "section_headers":
            row[key] = json.dumps(value or [], ensure_ascii=False)
        else:
            row[key] = "" if value is None else value
    return row


SIGNATURE_CSV_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "signature_key",
    "region",
    "scale",
    "sentence",
    "ner_text",
    "provider_name",
    "signature_date",
    "confidence",
    "source",
    "signature_present",
]

ADDITIONAL_CSV_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "page_number_key",
    "page_number_region",
    "page_number_sentence",
    "page_number_value",
    "printed_page_no",
    "printed_page_total",
    "confidence",
    "source",
    "section_headers",
]
