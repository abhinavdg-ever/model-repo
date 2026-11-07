"""Load imaging pipeline CSV outputs and overlay onto page rows (no dummy values).

Sources (first hit wins):
  1) data/folders/<chart>/imaging/<chart>_*.csv
  2) data/pipeline/<filename>.csv  (Docker: /data/pipeline) — flat drop
  3) data/pipeline/<pack>/…/output/<filename>.csv — mirrored packs
  4) monorepo 01-ocr-extraction / 02-imaging-pipeline pack outputs
"""

from __future__ import annotations

import json
import re
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.core.schemas import ImagingPageResult
from app.services.dates import show_date

_WS_RE = re.compile(r"\s+")


# Display-only names: the pipeline and taxonomy keep the canonical name.
_DISPLAY_NAMES = {"social history": "History"}


def display_page_type(label: str) -> str:
    """Page Type as the pipeline wrote it: ``Family (Page Type)``.

    The pipeline already drops the catalog's own parenthetical notes, so the
    one parenthesis left is the type within the family and is kept. A few
    names are shown shorter (``_DISPLAY_NAMES``: Social History -> History).
    """
    text = _WS_RE.sub(" ", (label or "").strip()).strip(" /")
    return _DISPLAY_NAMES.get(text.casefold(), text)


def compose_page_type(page_type: str | None, page_subtype: str | None) -> str:
    """``Page Type (Sub-type)`` from the taxonomy columns; just the name when
    the sub-type is the generic one. A row from before the taxonomy holds the
    whole ``Family (Type)`` string in one of them and is returned as is."""
    page_type = display_page_type(page_type or "")
    page_subtype = display_page_type(page_subtype or "")
    if page_type and page_subtype and page_type != page_subtype:
        return f"{page_type} ({page_subtype})"
    return page_type or page_subtype


def _fmt_dos_display(raw: str | date | None) -> str | None:
    return show_date(raw)


def _parse_float(raw: str | None) -> float | None:
    """Parse a number as-is (angles, counts, etc.)."""
    value = (raw or "").strip()
    if not value or value.upper() in {"N/A", "NA", "NULL", "NONE"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_confidence(raw: str | None) -> float | None:
    """Parse confidence; values in (1, 100] treated as percent → 0–1."""
    num = _parse_float(raw)
    if num is None:
        return None
    if num > 1.0 and num <= 100.0:
        return round(num / 100.0, 4)
    return num


def _parse_bool(raw: str | None) -> bool | None:
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value in {"1", "true", "t", "yes", "y"}:
        return True
    if value in {"0", "false", "f", "no", "n"}:
        return False
    return None


def page_type_fields(
    document_type: Any,
    is_visible: Any,
    handwritten_area_pct: Any,
) -> dict[str, Any]:
    """ocr_quality_results page-type columns (or their CSV cells) → API fields.

    Empty values are left out so they do not overwrite another source.
    """
    doc = _text(document_type).strip()
    fields: dict[str, Any] = {
        "documentType": doc.capitalize() if doc else None,
        "isVisible": _parse_bool(_text(is_visible)),
        "handwrittenAreaPct": _parse_float(_text(handwritten_area_pct)),
    }
    return {k: v for k, v in fields.items() if v is not None}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def empty_imaging_pages(pages: list[tuple[int, Path]]) -> list[ImagingPageResult]:
    """Skeleton rows only — all extraction fields stay null until CSV overlay."""
    return [
        ImagingPageResult(pageNumber=num, fileName=path.name) for num, path in pages
    ]


def inherit_duplicate_blank_junk(pages: list[ImagingPageResult]) -> list[ImagingPageResult]:
    """A duplicate page shows its original's Blank/Junk tag when the original is
    blank (or junk); otherwise it keeps its own "No" (a duplicate is not junk).

    Duplicates are found among Main pages, but the original can be junked later
    (pass 2), so the duplicate then reads as the page it copies.
    ``duplicateOf`` is the original's file name; a page whose original is not found
    keeps its own tag.
    """
    by_name: dict[str, ImagingPageResult] = {}
    for page in pages:
        by_name[page.fileName.lower()] = page
        by_name.setdefault(Path(page.fileName).stem.lower(), page)
    out: list[ImagingPageResult] = []
    for page in pages:
        original_name = (page.duplicateOf or "").strip().lower()
        original = (
            (by_name.get(original_name) or by_name.get(Path(original_name).stem))
            if original_name
            else None
        )
        if (
            page.isDuplicate
            and original is not None
            and original is not page
            and (original.blankOrJunk or "").strip().lower().startswith("yes")
        ):
            page = page.model_copy(update={"blankOrJunk": original.blankOrJunk})
        out.append(page)
    return out


def signature_people(text: str) -> tuple[str, str]:
    """Name and credentials of the one provider in a ``provider_name`` cell.

    The pipeline writes only the highest-scoring provider. A CSV from an
    earlier run lists several, separated by `` | ``; only the first is shown.
    A credential is a suffix on that person (``PA``, ``MD``, ``DO``).
    """
    from app.services.extraction_review import _is_credential

    names: list[str] = []
    credentials: list[str] = []
    for person in (text or "").split("|")[:1]:
        person = person.strip()
        if not person:
            continue
        name, creds = _split_person(person, _is_credential)
        if name:
            names.append(name)
        credentials.extend(creds)
    return " | ".join(names), " | ".join(credentials)


def _split_person(person: str, is_credential) -> tuple[str, list[str]]:
    pieces = [part.strip() for part in person.split(",") if part.strip()]
    creds: list[str] = []
    name_bits: list[str] = []
    for piece in pieces:
        tokens = piece.split()
        if tokens and all(is_credential(token) for token in tokens):
            creds.extend(tokens)
            continue
        trail: list[str] = []
        while tokens and is_credential(tokens[-1]):
            trail.append(tokens.pop())
        trail.reverse()
        if tokens:
            name_bits.append(" ".join(tokens))
        creds.extend(trail)
    return " ".join(name_bits), creds


def undotted(credentials: str) -> str:
    """Credentials shown without dots: 'M.D.' -> 'MD', 'Ph.D.' -> 'PhD', 'R.N.' -> 'RN'. A dot
    between two longer credentials separates them: 'APRN.CNP' -> 'APRN CNP'."""
    text = re.sub(r"(?<=[A-Za-z]{2})\.(?=[A-Za-z]{2})", " ", credentials)
    return re.sub(r"[ \t]{2,}", " ", text.replace(".", "")).strip()


# 2026-11-01 (user): a signature block's role words are never part of a provider's name
# (core-pipeline modules/names.py PROVIDER_ROLE_WORDS; rows written before it still have them).
_PROVIDER_ROLE_WORDS = (
    "legal authenticator", "legal authority", "signing authority", "signing authenticator",
    "authenticator", "authenticated by", "authenticated", "authority", "authorized by",
    "legal", "signing", "signed by", "e-signed by", "electronically signed by", "signer",
    # 2026-11-03 (user): "Provider Dianne Kreptowski".
    "rendering provider", "attending provider", "provider name", "provider",
)
_PROVIDER_ROLE = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in
                                   sorted(_PROVIDER_ROLE_WORDS, key=len, reverse=True)) + r")(?![A-Za-z])\s*:?",
    re.IGNORECASE,
)


# 2026-11-02 (user): a leading By / Street / Road is never part of a name (core-pipeline
# modules/names.py NAME_PREFIX_WORDS; rows written before it still have them).
_NAME_PREFIX = re.compile(r"^(?:(?:by|street|road)(?![A-Za-z])[\s:.,;\-]*)+", re.IGNORECASE)


def without_name_prefix(name: str) -> str:
    return _NAME_PREFIX.sub("", (name or "").strip()).strip(" ,;:-")


def without_role_words(name: str) -> str:
    text = re.sub(r"\s{2,}", " ", _PROVIDER_ROLE.sub(" ", name or "")).strip()
    return without_name_prefix(re.sub(r"\s+,", ",", text).strip(" ,;:"))


def provider_fields(name: str | None, credentials: str | None) -> tuple[str, str]:
    """A provider's name and credentials as the pipeline stored them. Rows written since the
    provider-signature rule (2026-10-18) carry ``provider_credentials`` beside a bare name;
    an older row (``credentials`` None: no such column) keeps them inside the name. Role
    words (Legal Authenticator, Signing Authority ...) come off the name."""
    bare_name, credentials_in_name = signature_people(without_role_words(name or ""))
    if credentials is None:
        return bare_name, undotted(credentials_in_name)
    # The name is shown without credentials even if some slipped into it: the known ones,
    # and the row's own (DNP, APRN.CNP) wherever they sit.
    own = {re.sub(r"[\s.]+", "", c).casefold() for c in re.split(r"[,|/]", credentials) if c.strip()}
    if own:
        kept = [
            token for token in re.split(r"(\s+|,)", bare_name)
            if re.sub(r"[\s.,]+", "", token).casefold() not in own
        ]
        bare_name = re.sub(r"\s*,\s*$", "", re.sub(r"\s+", " ", "".join(kept))).strip(" ,")
    return bare_name, undotted(credentials)


def provider_version(value: Any) -> int:
    """The provider-signature rule a row was written by: 2 (2026-10-18) or 1 (older; also
    a row with no version at all)."""
    try:
        return int(str(value).strip()) if value not in (None, "") else 1
    except ValueError:
        return 1


def _name_words(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", name.casefold())


def same_person(a: str, b: str) -> bool:
    """Two spellings of one signer: the same words ignoring case and punctuation
    ("Lynda J Storm" / "Lynda J. Storm"), the same first and last name with or
    without middle initials ("Lynda Storm"), or an OCR slip (≥ 90% alike)."""
    wa, wb = _name_words(a), _name_words(b)
    if not wa or not wb:
        return False
    if wa == wb or sorted(wa) == sorted(wb):
        return True
    if len(wa) > 1 and len(wb) > 1 and (wa[0], wa[-1]) == (wb[0], wb[-1]):
        return True
    return SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio() >= 0.9


def _distinct_signers(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each signer once: a later spelling of someone already listed is dropped,
    lending its credentials and date when the first had none."""
    kept: list[dict[str, Any]] = []
    for person in people:
        twin = next((k for k in kept if same_person(k["name"], person["name"])), None)
        if twin is None:
            kept.append(dict(person))
            continue
        for key in ("credentials", "signatureDate"):
            if not twin.get(key) and person.get(key):
                twin[key] = person[key]
    return kept


def signer_fields(signers: Any, name: str | None, credentials: str | None, version: Any = None) -> dict[str, Any]:
    """providerName / providerCredentials / providerSigners from a signature row, read by
    the rule that wrote it (``provider_version``).

    Version 2: every signer in ``signers`` (a JSON list, or its text) is listed,
    providerName is the first and providerCredentials all of their credentials; the
    ``provider_credentials`` column beside a bare name otherwise. Version 1 (or none): the
    one provider, its credentials split off the name.
    """
    if provider_version(version) < 2:
        who, creds = provider_fields(name, None)
        return {"providerName": who or None, "providerCredentials": creds or None}
    if isinstance(signers, str):
        try:
            signers = json.loads(signers) if signers.strip() else None
        except ValueError:
            signers = None
    people: list[dict[str, str | None]] = []
    for item in signers if isinstance(signers, list) else []:
        if not isinstance(item, dict):
            continue
        who, creds = provider_fields(item.get("name"), item.get("credentials") or "")
        if who:
            people.append({"name": who, "credentials": creds or None,
                           "signatureDate": item.get("signature_date") or None})
    people = _distinct_signers(people)
    if people:
        joined = " | ".join(p["credentials"] for p in people if p["credentials"])
        return {"providerName": people[0]["name"], "providerCredentials": joined or None,
                "providerSigners": people}
    who, creds = provider_fields(name, credentials or "")
    return {"providerName": who or None, "providerCredentials": creds or None}


def subtype_confidence(l2: Any, l2_conf: Any, l3: Any, l3_conf: Any) -> float | None:
    """The subtype's own confidence: the deepest named level's (L3, else L2; Others is not
    named), as core-pipeline's axes.subtype_confidence."""
    for name, value in ((l3, l3_conf), (l2, l2_conf)):
        if name and str(name).strip() and str(name).strip() != "Others":
            parsed = _parse_confidence(value) if isinstance(value, str) else (float(value) if value is not None else None)
            if parsed is not None:
                return parsed
    return None


_DUP_PAGE_RE = re.compile(r"duplicate_of_page:(\S+)")


_TAG = {"first": "New", "single": "Standalone", "continue": "Continue", "last": "Last"}


def continuity_label(position: str | None, document: int | None, confidence_level: str | None) -> str | None:
    """'New (Doc 3)', 'Continue? (Doc 3)' from page_continuity_results' position: the gap
    scorer's tags (first = New, single = Standalone); '?' when the gap before was low."""
    if not position or document is None:
        return None
    unsure = "?" if (confidence_level or "") == "low" else ""
    return f"{_TAG.get(position, position.capitalize())}{unsure} (Doc {document})"


_CATEGORY_DISPLAY = {
    "codeable": "Codable",
    "non_codeable": "Non-Codable",
    "discharge_summary": "Discharge",
}


