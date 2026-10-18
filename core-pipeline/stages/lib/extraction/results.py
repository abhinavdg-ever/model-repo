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
# Confidence = the signature's score, lowered when the name or credentials came from
# outside the signature (the page's provider_name / provider_credentials rows), and when
# the page has several signatures.
OUTSIDE_FACTOR = 0.75
SEVERAL_FACTOR = 0.9
# A row counts (electronic signature, provider name, credentials) when the rules accepted
# it, or its score is above this.
COUNTS_ABOVE = 0.7


def _counts(row: dict[str, Any]) -> bool:
    """Accepted, or scored above COUNTS_ABOVE."""
    return bool(row.get("accepted", True)) or (_score(row) or 0.0) > COUNTS_ABOVE


def credentials_in_sentence(sentence: Optional[str]) -> Optional[str]:
    """Every credential of provider_credentials_canon.json in the sentence, in order
    ('… WILLIAM J WALL III, MD on …' -> 'MD'), joined with ', '."""
    from .provider_credentials.extract import load_credentials

    catalog = set(load_credentials())
    found: list[str] = []
    for token in (sentence or "").split():
        if _is_credential(token, catalog):
            bare = token.strip(",;:()[]")
            if "." not in bare.rstrip("."):
                bare = bare.rstrip(".")
            if bare not in found:
                found.append(bare)
    return ", ".join(found) or None


def _signer(row: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """One accepted signature's name and credentials: its ``provider_name`` (without
    credentials) and ``designation`` (else the credentials in the name); a missing name
    from its ``ner_text``, missing credentials from its ``sentence`` (the credentials canon)."""
    # A mis-cased credential ('Rd', a road; 'Md', a state) is not one: dropped first.
    name, creds = split_name_credentials(caps_credentials_only(_text(row, "provider_name")))
    creds = _text(row, "designation") or creds
    if not name:
        name = split_name_credentials(_text(row, "ner_text"))[0]
    if not creds:
        creds = credentials_in_sentence(_text(row, "sentence"))
    return name, creds


def signature_fields(
    hit: Any = None,
    provider_hits: Optional[list[dict[str, Any]]] = None,
    credential_hits: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """The page's provider signature (docs/PROVIDER_LOGIC.md).

    ``hit`` is the page's electronic-signature rows; one counts when accepted or scored
    above COUNTS_ABOVE (0.7). Signature present = at least one signer was found (a name or
    credentials); a signature with neither is No. Without one, no provider name, no
    credentials.

    Each accepted signature: name = its ``provider_name`` without credentials, credentials =
    its ``designation``; a missing name from its ``ner_text``, missing credentials from its
    ``sentence`` (the credentials canon).

    - **Several accepted:** that is all. A signer still missing its name or its
      credentials is removed.
    - **One counted:** a missing name from a counted ``provider_credentials`` row that
      names its provider (``credential_hits``), else the page's counted ``provider_name``
      rows (``provider_hits``, the best-scoring); missing credentials from the counted
      ``provider_credentials`` (the same provider's first).

    ``signers`` lists them, the selected one first, then by score; the same person once.
    ``provider_name`` / ``provider_credentials`` are the first signer's.

    Confidence is the signature's score: x0.75 when the name or credentials came from
    outside it (OUTSIDE_FACTOR), x0.9 each when there are several signatures
    (SEVERAL_FACTOR). Each signer carries its own; the page's is their average.
    """
    rows = [row for row in _as_hits(hit) if _counts(row)]
    empty = {
        "signature_present": False,
        "signature_key": None, "region": None, "scale": None, "sentence": None,
        "ner_text": None, "provider_name": None, "provider_credentials": None,
        "signature_date": None, "confidence": None, "source": None, "signers": [],
        "provider_version": PROVIDER_VERSION,
    }
    if not rows:
        return empty
    primary = _best([row for row in rows if row.get("selected")]) or _best(rows)
    ordered = [primary, *sorted((row for row in rows if row is not primary), key=lambda row: -(_score(row) or 0.0))]

    several = len(ordered) > 1
    signers: list[dict[str, Any]] = []
    for row in ordered:
        name, creds = _signer(row)
        outside = False
        if not several:
            counted_creds = [item for item in _as_hits(credential_hits) if _counts(item) and _text(item, "value")]
            if not name:
                # A credential row that names its provider ('Meghan Newman' + 'FNP'), then the
                # page's provider_name rows.
                named = _best([item for item in counted_creds if _text(item, "provider_name")])
                if named is not None:
                    name = split_name_credentials(_text(named, "provider_name"))[0]
                    creds = creds or _text(named, "value")
                    outside = True
            if not name:
                best = _best([item for item in _as_hits(provider_hits) if _counts(item) and _text(item, "value")])
                if best is not None:
                    name, from_name = split_name_credentials(_text(best, "value"))
                    creds = creds or from_name
                    outside = True
            if not creds:
                accepted = counted_creds
                same = [
                    item for item in accepted
                    if name and _text(item, "provider_name")
                    and _text(item, "provider_name").casefold() in name.casefold()
                ]
                chosen = (same or accepted)[:1]
                if chosen:
                    creds = _text(chosen[0], "value")
                    outside = True
        elif not (name and creds):
            continue  # several signatures: one without a name and credentials is removed
        if not name and not creds:
            continue
        twin = next((s for s in signers if name and s["name"] and s["name"].casefold() == name.casefold()), None)
        if twin is not None:
            twin["credentials"] = twin["credentials"] or creds
            continue
        score = _score(row)
        if score is not None:
            score = round(score * (OUTSIDE_FACTOR if outside else 1.0) * (SEVERAL_FACTOR if several else 1.0), 4)
        signers.append({
            "name": name, "credentials": creds,
            "signature_date": _iso_date(_text(row, "signature_date")), "confidence": score,
        })

    if not signers:
        # A signature with no name and no credentials anywhere is not a provider signature.
        return {**empty, "signature_present": False}
    lead = signers[0]
    # One signer: its confidence. Several: the average over the listed signers.
    scores = [s["confidence"] for s in signers if s["confidence"] is not None]
    page_confidence = round(sum(scores) / len(scores), 4) if scores else None
    return {
        **empty,
        "signature_present": True,
        "signature_key": _text(primary, "key"),
        "region": _text(primary, "region"),
        "scale": _text(primary, "scale"),
        "sentence": _text(primary, "sentence"),
        "ner_text": _text(primary, "ner_text"),
        "provider_name": lead["name"],
        "provider_credentials": lead["credentials"],
        "signature_date": lead["signature_date"],
        "confidence": page_confidence,
        "source": _text(primary, "source"),
        "signers": signers,
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
