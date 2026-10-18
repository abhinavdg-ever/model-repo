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


# Credentials from provider_name/suffixes.txt that are normally printed in mixed case
# (normalized: lower case, no dots or spaces). Accepted when they start with a capital.
MIXED_CASE_CREDENTIALS = frozenset({
    "phd", "psy-d", "psyd", "pharmd", "edd", "med", "chb", "mbchb", "bch", "bchir", "cpht",
})


def _is_caps(token: str) -> bool:
    letters = [char for char in token if char.isalpha()]
    return bool(letters) and all(char.isupper() for char in letters)


def _printed_as_credential(token: str, norm: str) -> bool:
    """In capitals, or one of MIXED_CASE_CREDENTIALS starting with a capital."""
    if _is_caps(token):
        return True
    letters = [char for char in token if char.isalpha()]
    return norm in MIXED_CASE_CREDENTIALS and bool(letters) and letters[0].isupper()


def caps_credentials_only(name: Optional[str]) -> Optional[str]:
    """The provider name with its trailing credentials kept only when the document
    prints them in capitals ("RD", "R.D.", "MD"), or in their usual mixed case for
    MIXED_CASE_CREDENTIALS ("PhD", "PharmD"). A credential spelled otherwise is
    rejected and removed: "Rd" is a road, "Md" a state. The name keeps at least two
    words before its credentials, so "Doe, Ed" is left alone."""
    if not name:
        return name
    from .provider_name.extract import _norm_suffix, load_suffixes

    catalog = load_suffixes()
    pieces = [piece.split() for piece in name.split(",")]
    flat = [(at, token) for at, tokens in enumerate(pieces) for token in tokens]
    start = len(flat)
    while start > 0 and _norm_suffix(flat[start - 1][1]) in catalog:
        start -= 1
    if start < 2:
        return name
    rejected = {
        index
        for index in range(start, len(flat))
        if not _printed_as_credential(flat[index][1], _norm_suffix(flat[index][1]))
    }
    if not rejected:
        return name
    kept: list[list[str]] = [[] for _ in pieces]
    for index, (at, token) in enumerate(flat):
        if index not in rejected:
            kept[at].append(token)
    return ", ".join(" ".join(tokens) for tokens in kept if tokens) or None


def _credential_catalog() -> set[str]:
    """Normalized credentials: provider_name/suffixes.txt and provider_credentials_canon.json."""
    from .provider_credentials.extract import load_credentials
    from .provider_name.extract import load_suffixes

    return set(load_suffixes()) | set(load_credentials())


def _is_credential(token: str, catalog: set[str]) -> bool:
    """A credential token as printed: in the catalog, and in capitals (or its usual mixed
    case, 'PhD'), so 'Md' (a state) and 'Rd' (a road) are not."""
    from .provider_name.extract import _norm_suffix

    bare = token.strip(",.;:()[]")
    norm = _norm_suffix(bare)
    if norm and norm in catalog and _printed_as_credential(bare, norm):
        return True
    # Combined credentials: 'APRN.CNP', 'MD/PhD'.
    parts = [part for part in re.split(r"[./]", bare) if part]
    return len(parts) > 1 and all(
        _norm_suffix(part) in catalog and _printed_as_credential(part, _norm_suffix(part)) for part in parts
    )


def split_name_credentials(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'WILLIAM J WALL III, MD' -> ('WILLIAM J WALL III', 'MD'); 'Meghan Newman , DNP' ->
    ('Meghan Newman', 'DNP'); 'LAUBERT CNP, MATTHEW' -> ('LAUBERT, MATTHEW', 'CNP'). The
    credentials are every credential token, in order, joined with ', '."""
    if not text or not text.strip():
        return None, None
    catalog = _credential_catalog()
    name_parts: list[str] = []
    creds: list[str] = []
    for piece in text.split(","):
        kept = []
        for token in piece.split():
            if kept or name_parts:
                if _is_credential(token, catalog):
                    bare = token.strip(",;:()[]")
                    # 'M.D.' keeps its dots; a sentence's full stop after 'MD.' does not stay.
                    if "." not in bare.rstrip("."):
                        bare = bare.rstrip(".")
                    if bare not in creds:
                        creds.append(bare)
                    continue
            kept.append(token)
        if kept:
            name_parts.append(" ".join(kept))
    name = ", ".join(name_parts).strip(" ,") or None
    return name, ", ".join(creds) or None


# Where a signer's name stops in the sentence: ' on ', ' at ', a date, a time, '|', '(', ';'.
_NAME_STOP = re.compile(
    r"(?i)\s(?:on|at)\s|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}:\d{2}|[|(;]|\s-\s|\bsign\s*off\b"
)
_NAME_TOKEN = re.compile(r"^(?:[A-Z][A-Za-z'\-]*\.?|[A-Z]\.?|[A-Z][A-Za-z.\-]*),?$")
_MAX_NAME_TOKENS = 7


def name_from_sentence(sentence: Optional[str], key: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """The signer's name and credentials read from the signature's sentence: the words right
    after the key ('Electronically signed by Meghan Newman , DNP on 05/19/2025' ->
    ('Meghan Newman', 'DNP')), up to ' on ', ' at ', a date or time. (None, None) when no
    two-word name follows."""
    text = (sentence or "").strip()
    if not text:
        return None, None
    words = (key or "").split()
    if words:
        found = re.search(r"\s*".join(re.escape(w) for w in words), text, re.IGNORECASE)
        if found:
            text = text[found.end():]
    text = text.lstrip(" :-–|")
    stop = _NAME_STOP.search(text)
    if stop:
        text = text[: stop.start()]
    tokens: list[str] = []
    for token in re.sub(r"\s*,\s*", ", ", text).split():
        if token == ",":
            continue
        if not _NAME_TOKEN.match(token) or len(tokens) >= _MAX_NAME_TOKENS:
            break
        tokens.append(token)
    name, creds = split_name_credentials(" ".join(tokens))
    if not name or len(name.replace(",", " ").split()) < 2:
        return None, creds
    return name, creds


def _best(rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The highest-scoring row; a tie (or no score) goes to the earliest."""
    best = None
    for row in rows:
        if best is None or (_score(row) or -1.0) > (_score(best) or -1.0):
            best = row
    return best


# The provider-signature rule a row was written by. 1 (or none): the provider name carries
# its credentials, one provider. 2 (2026-10-18): bare name, provider_credentials and every
# accepted signer in signers. review-ui reads a row by its version.
PROVIDER_VERSION = 2


def _signer(row: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """One signature row's name and credentials: its own fields (``provider_name``
    without credentials, ``designation``), else read from its ``sentence``."""
    # A mis-cased credential ('Rd', a road; 'Md', a state) is not one: dropped first.
    name, creds = split_name_credentials(caps_credentials_only(_text(row, "provider_name")))
    creds = _text(row, "designation") or creds
    if not name or not creds:
        said_name, said_creds = name_from_sentence(_text(row, "sentence"), _text(row, "key"))
        name = name or said_name
        creds = creds or said_creds
    return name, creds


def signature_fields(
    hit: Any = None,
    provider_hits: Optional[list[dict[str, Any]]] = None,
    credential_hits: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """The page's provider signature (docs/PROVIDER_LOGIC.md).

    ``hit`` is the page's **accepted** electronic-signature rows. Signature present = any
    is accepted. Without one, there is no provider name and no credentials either.

    Every accepted signer is listed (``signers``), the selected one first (the
    highest-scoring when several are selected, else the highest-scoring accepted one), then
    the rest by score; one without a name is left out. Each signer's name and credentials:
    the row's own fields, else its sentence. The first signer also falls back to the page's accepted ``provider_credentials``
    (``credential_hits``, the same provider's first) and, for a missing name, to the page's
    ``provider_name`` rows (``provider_hits``); the others only to a credential row of the
    same provider. ``provider_name`` / ``provider_credentials`` are the first signer's.
    """
    rows = [row for row in _as_hits(hit) if row.get("accepted", True)]
    if not rows:
        return {
            "signature_present": False,
            "signature_key": None, "region": None, "scale": None, "sentence": None,
            "ner_text": None, "provider_name": None, "provider_credentials": None,
            "signature_date": None, "confidence": None, "source": None, "signers": [],
            "provider_version": PROVIDER_VERSION,
        }
    primary = _best([row for row in rows if row.get("selected")]) or _best(rows)
    others = sorted((row for row in rows if row is not primary), key=lambda row: -(_score(row) or 0.0))
    accepted_creds = [row for row in _as_hits(credential_hits) if row.get("accepted", True) and _text(row, "value")]

    def same_provider_credentials(name: Optional[str]) -> Optional[str]:
        if not name:
            return None
        for row in accepted_creds:
            who = _text(row, "provider_name")
            if who and who.casefold() in name.casefold():
                return _text(row, "value")
        return None

    signers: list[dict[str, Any]] = []
    for index, row in enumerate([primary, *others]):
        name, creds = _signer(row)
        main = index == 0
        creds = creds or same_provider_credentials(name)
        if main and not creds and accepted_creds:
            creds = _text(accepted_creds[0], "value")
        if main and not name:
            best = _best([item for item in _as_hits(provider_hits) if _text(item, "value")])
            if best is not None:
                name, from_name = split_name_credentials(_text(best, "value"))
                creds = creds or from_name
        if not main and not name:
            continue
        # The same person signing twice is one signer.
        twin = next((s for s in signers if name and s["name"] and s["name"].casefold() == name.casefold()), None)
        if twin is not None:
            twin["credentials"] = twin["credentials"] or creds
            continue
        signers.append({
            "name": name,
            "credentials": creds,
            "signature_date": _iso_date(_text(row, "signature_date")),
        })

    lead = signers[0]
    return {
        "signature_present": True,
        "signature_key": _text(primary, "key"),
        "region": _text(primary, "region"),
        "scale": _text(primary, "scale"),
        "sentence": _text(primary, "sentence"),
        "ner_text": _text(primary, "ner_text"),
        "provider_name": lead["name"],
        "provider_credentials": lead["credentials"],
        "signature_date": lead["signature_date"],
        "confidence": _score(primary),
        "source": _text(primary, "source"),
        "signers": signers,
        "provider_version": PROVIDER_VERSION,
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
        elif isinstance(value, (list, dict)):
            row[key] = json.dumps(value, ensure_ascii=False)
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
    "provider_credentials",
    "signature_date",
    "confidence",
    "source",
    "signature_present",
    # Every accepted signer, the selected one first: [{"name", "credentials", "signature_date"}] (JSON).
    "signers",
    "provider_version",
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
