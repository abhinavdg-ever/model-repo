"""Read a chart's staged key/value extraction for the Review pane.

The values are the ones the extraction stage selected. Nothing here applies
member verification, the DOS scorer, or any other later-stage rewrite.
Processed is the same string as extracted until that rewrite exists.
"""
from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException

from app.core.schemas import (
    ExtractionFieldRow,
    ExtractionPagesResponse,
    ExtractionReviewResponse,
    OcrSectionHeader,
)
from app.services.dates import show_dates_in

# Order is the extraction field list, then headings. Labels are what a reviewer sees.
_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "Member Name"),
    ("dob", "Member DOB"),
    ("member_id", "Member ID"),
    ("provider_name", "Provider Name"),
    ("provider_credentials", "Provider Credentials"),
    ("electronic_signature", "Provider Signature"),
    ("dos", "Date of Service"),
    ("page_no", "Page Number"),
    ("heading_heron", "Headings"),
)


def _folder_dir(data_root: Path, folder_id: str) -> Path:
    if "/" in folder_id or "\\" in folder_id or folder_id in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid folder id")
    root = data_root.resolve()
    path = (root / folder_id).resolve()
    if not str(path).startswith(str(root)):
        raise HTTPException(status_code=400, detail="Invalid folder id")
    if not path.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {folder_id}")
    return path


def _chosen(rows: object) -> list[dict]:
    if not isinstance(rows, list):
        return []
    chosen: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("accepted"):
            continue
        if row.get("selected", True):
            chosen.append(row)
    return chosen


# A print stamp is never a date of service, whatever older staging chose.
_PRINT_WORD = re.compile(r"(?i)(?<![a-z])print(?:ed|s)?(?![a-z])")


def _is_print_date(hit: dict) -> bool:
    """A print key's row, or a date written right after a print word on its line."""
    if hit.get("tier") == "printed":
        return True
    sentence = str(hit.get("sentence") or "")
    value = str(hit.get("value") or hit.get("dos_from") or "").strip()
    at = sentence.find(value) if value else -1
    if at < 0:
        # Not found as written ('10/30/20258:10 AM'): a header / footer date on a print line.
        return str(hit.get("tier")) == "keyless" and bool(_PRINT_WORD.search(sentence))
    return bool(_PRINT_WORD.search(sentence[max(0, at - 30) : at]))


def _not_printed(hits: list[dict]) -> list[dict]:
    """DOS rows minus print dates: older staging still selected 'Printed On'."""
    return [hit for hit in hits if not _is_print_date(hit)]


def _hit_text(field_id: str, hit: dict) -> str:
    if field_id == "electronic_signature":
        name = str(hit.get("provider_name") or "").strip()
        signed = show_dates_in(str(hit.get("signature_date") or "").strip())
        if name and signed:
            return f"{name} · {signed}"
        return name or signed
    if field_id == "dob":
        return show_dates_in(str(hit.get("value") or "").strip())
    if field_id == "page_no":
        number = str(hit.get("page_no") or hit.get("value") or "").strip()
        total = str(hit.get("page_total") or "").strip()
        if number and total:
            return f"{number} of {total}"
        return number
    if field_id == "heading_heron":
        return _display_header(str(hit.get("text") or ""))
    if field_id == "provider_name":
        return _provider_name(str(hit.get("value") or ""))
    value = str(hit.get("value") or "").strip()
    if field_id == "dos":
        value = show_dates_in(value)
    if value:
        return value
    start = show_dates_in(str(hit.get("dos_from") or "").strip())
    end = show_dates_in(str(hit.get("dos_to") or "").strip())
    if start and end and start != end:
        return f"{start} – {end}"
    return start or end


def _join(parts: list[str]) -> str:
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return " | ".join(seen)


def _confidence(hits: list[dict]) -> float | None:
    scores: list[float] = []
    for hit in hits:
        try:
            scores.append(float(hit.get("score")))
        except (TypeError, ValueError):
            continue
    return max(scores) if scores else None


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _page_fields(page: dict | None) -> dict:
    fields = (page or {}).get("fields") if isinstance(page, dict) else None
    return fields if isinstance(fields, dict) else {}


def _credential_rows(fields: dict) -> list[dict]:
    # Every accepted credential: staging written before every provider was selected left a
    # second provider's credential unselected. _providers merges repeats.
    return [
        row for row in fields.get("provider_credentials") or []
        if isinstance(row, dict) and row.get("accepted")
    ]


def credentials_text(page: dict | None) -> str:
    """The Provider Credentials cell of field_rows alone: one entry per provider."""
    fields = _page_fields(page)
    providers = _providers(_credential_rows(fields))
    if providers:
        return " | ".join(", ".join(creds) for _, creds in providers)
    return _join(_hit_text("provider_credentials", hit) for hit in _chosen(fields.get("provider_credentials")))


def field_rows(page: dict | None) -> list[ExtractionFieldRow]:
    fields = _page_fields(page)
    # How sure "not found" is, per field with nothing accepted (the pipeline's staging).
    absent = (page or {}).get("absent") if isinstance(page, dict) else None
    absent = absent if isinstance(absent, dict) else {}
    rows: list[ExtractionFieldRow] = []
    credential_rows = _credential_rows(fields)
    providers = _providers(credential_rows)
    for field_id, label in _FIELDS:
        chosen = _chosen(fields.get(field_id))
        if field_id == "dos":
            chosen = _not_printed(chosen)
        if field_id == "heading_heron":
            chosen = _kept_headings(fields.get(field_id))
        extracted = _join(_hit_text(field_id, hit) for hit in chosen)
        if field_id in ("provider_name", "provider_credentials") and providers:
            # Both from the credential rows, one entry per provider, in the same order. A
            # page with no credential keeps the key/value provider name.
            chosen = credential_rows
            if field_id == "provider_name":
                extracted = " | ".join(name for name, _ in providers if name)
            else:
                extracted = " | ".join(", ".join(creds) for _, creds in providers)
        rows.append(
            ExtractionFieldRow(
                id=field_id,
                label=label,
                extracted=extracted,
                processed=extracted,
                confidence=_confidence(chosen) if extracted else _number(absent.get(field_id)),
                ground_truth="",
            )
        )
    return rows


def _fraction(value: float, span: float) -> float:
    if span <= 0:
        return 0.0
    return max(0.0, min(1.0, value / span))


# A shown heading needs two of these three.
# Heron: same line as ACCEPT_SCORE in heading/extract.py.
# List: common_headings.txt, including a listed phrase contained in the line.
# Ranker: the stored accepted flag, which v002 sets from its probability (cutoff 0.30).
_HERON_MIN = 0.5
_COMMON_MIN = 0.7
# Or Heron alone, this sure, on text that reads like a heading (STRONG_SCORE and
# looks_like_heading in heading/extract.py).
_HERON_STRONG = 0.7
_HEADING_MAX_WORDS = 6
_FUZZY_MIN_CHARS = 6
_FILLER = frozenset({"and", "of", "the", "for", "to"})


def _common_key(text: str) -> str:
    """'PROGRESS NOTES:' and 'Progress note' share one key."""
    words = re.findall(r"[a-z0-9]+", (text or "").casefold())
    words = [
        word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word
        for word in words
    ]
    return " ".join(word for word in words if word not in _FILLER)


# The pipeline's credential list (MD, DO, NP, PA-C, ...). In the repo it is read
# from core-pipeline; the review-ui container has no core-pipeline, so a copy
# ships in app/data (kept identical by tests/test_contracts.py).
_BUNDLED_SUFFIXES = Path(__file__).resolve().parents[1] / "data" / "credential_suffixes.txt"


def _suffixes_file() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = (
            parent
            / "core-pipeline"
            / "stages"
            / "lib"
            / "extraction"
            / "provider_name"
            / "suffixes.txt"
        )
        if candidate.is_file():
            return candidate
    return _BUNDLED_SUFFIXES if _BUNDLED_SUFFIXES.is_file() else None


@lru_cache(maxsize=1)
def _credential_keys() -> frozenset[str]:
    path = _suffixes_file()
    if path is None:
        return frozenset()
    keys: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        text = raw.strip()
        if text and not text.startswith("#"):
            keys.add(re.sub(r"[\s.]+", "", text).casefold())
    return frozenset(keys)


def _is_credential(token: str) -> bool:
    key = re.sub(r"[\s.]+", "", token).casefold()
    return bool(key) and key in _credential_keys()


_PROVIDER_MAX_WORDS = 4


def _provider_before(sentence: str, credential: str) -> str:
    """The name in front of ``credential`` in its line: back to a colon or the line start,
    at most four words, the person's other credentials skipped (the pipeline's
    provider_credentials.provider_before, for staging written before it stored the name)."""
    tokens = (sentence or "").split()
    target = re.sub(r"[\s.]+", "", credential or "").casefold()
    index = next(
        (n for n, token in enumerate(tokens)
         if re.sub(r"[\s.,;]+", "", token).casefold() == target and n > 0),
        None,
    )
    if index is None:
        return ""
    n = index - 1
    while n >= 0 and _is_credential(tokens[n].strip(",;")):
        n -= 1
    words: list[str] = []
    while n >= 0 and len(words) < _PROVIDER_MAX_WORDS:
        text = tokens[n]
        if ":" in text:
            tail = text.rsplit(":", 1)[1]
            if tail:
                words.append(tail)
            break
        words.append(text)
        n -= 1
    return " ".join(reversed(words)).strip(" ,;")


def _providers(rows: list[dict]) -> list[tuple[str, list[str]]]:
    """(provider name, credentials) per provider, in page order. A provider with two
    credentials ('Jane Doe, MD, PhD') is one entry."""
    out: list[tuple[str, list[str]]] = []
    for row in rows:
        credential = str(row.get("value") or "").strip()
        if not credential:
            continue
        name = str(row.get("provider_name") or "").strip() or _provider_before(
            str(row.get("sentence") or ""), credential
        )
        for known, creds in out:
            if known.casefold() == name.casefold():
                if credential not in creds:
                    creds.append(credential)
                break
        else:
            out.append((name, [credential]))
    return out


def _provider_name(value: str) -> str:
    """Name only. Credentials and the comma or pipe in front of them come off."""
    pieces = [part.strip() for part in re.split(r"[,|]+", value or "") if part.strip()]
    while pieces and all(_is_credential(token) for token in pieces[0].split()):
        pieces.pop(0)
    while pieces:
        tokens = pieces[-1].split()
        if tokens and all(_is_credential(token) for token in tokens):
            pieces.pop()
            continue
        while tokens and _is_credential(tokens[-1]):
            tokens.pop()
        if tokens:
            pieces[-1] = " ".join(tokens)
        else:
            pieces.pop()
        break
    return ", ".join(pieces)


def _headings_file() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = (
            parent
            / "core-pipeline"
            / "stages"
            / "lib"
            / "extraction"
            / "heading"
            / "common_headings.txt"
        )
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=1)
def _common_headings() -> frozenset[str]:
    path = _headings_file()
    if path is None:
        return frozenset()
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return frozenset(key for key in map(_common_key, lines) if key)


def _contains_heading(key: str, known: frozenset[str]) -> bool:
    """True when a listed heading's words sit inside the line."""
    words = key.split()
    if not words:
        return False
    for item in known:
        parts = item.split()
        size = len(parts)
        if size == 0 or size > len(words):
            continue
        for start in range(len(words) - size + 1):
            if words[start : start + size] == parts:
                return True
    return False


def common_heading_score(text: str) -> float:
    """1.0 when the text is or contains a common heading, else the closest similarity, else 0."""
    return _common_key_score(_common_key(text))


# The same heading text recurs on page after page and is scored twice per row (shown
# headers and the Headings list); the list itself is read once per process.
@lru_cache(maxsize=50_000)
def _common_key_score(key: str) -> float:
    known = _common_headings()
    if not key or not known:
        return 1.0 if key and not known else 0.0
    if key in known or _contains_heading(key, known):
        return 1.0
    if len(key) < _FUZZY_MIN_CHARS:
        return 0.0
    best = 0.0
    matcher = SequenceMatcher(None, key)
    for item in known:
        if len(item) < _FUZZY_MIN_CHARS or abs(len(item) - len(key)) > 0.25 * len(key) + 2:
            continue
        matcher.set_seq2(item)
        # Both quick ratios are upper bounds of ratio(): skip what cannot beat the best.
        if matcher.real_quick_ratio() <= best or matcher.quick_ratio() <= best:
            continue
        best = max(best, matcher.ratio())
    return best


def _proper_piece(piece: str) -> str:
    """Title-case a shouted word. A short acronym (CC, HPI) and mixed forms (PMHx) stay."""
    letters = [char for char in piece if char.isalpha()]
    if letters and all(char.isupper() for char in letters) and len(letters) > 3:
        return piece[:1].upper() + piece[1:].lower()
    return piece


# A shown header is at most this many words, and names no contact detail.
_SHOWN_MAX_WORDS = 6
_CONTACT_WORDS = frozenset({
    "phone", "phones", "telephone", "tel", "fax", "faxed", "cell", "mobile", "pager",
    "email", "www", "http", "https", "ext", "call",
})


def _display_header(text: str) -> str:
    """The label a reviewer sees: proper case, with punctuation removed.

    One date or number at the start or the end is dropped and the rest shown
    ('OFFICE VISIT REPORT 03/28/2024' -> 'Office Visit Report'). No header ('') when one
    sits in the middle or there are several ('Signed by on 03/29/24 at 9:15 AM'), when it
    is more than _SHOWN_MAX_WORDS words (a sentence), or when it names a phone, fax or
    other contact detail ('Toll Free Tel', 'Fax #')."""
    words = text.split()
    numbered = [n for n, word in enumerate(words) if any(char.isdigit() for char in word)]
    if len(numbered) > 1 or (numbered and numbered[0] not in (0, len(words) - 1)):
        return ""
    if numbered:
        del words[numbered[0]]
    kept = re.sub(r"[^A-Za-z/]+", " ", " ".join(words)).split()
    if not kept or len(kept) > _SHOWN_MAX_WORDS:
        return ""
    if any(piece.casefold() in _CONTACT_WORDS for word in kept for piece in word.split("/")):
        return ""
    return " ".join("/".join(_proper_piece(piece) for piece in word.split("/")) for word in kept)


def _numeric_opening(text: str) -> bool:
    """True when the header opens on a run that is mostly digits, such as a date or an id."""
    opening: list[str] = []
    for char in text.strip():
        if char.isalpha():
            break
        if not char.isspace():
            opening.append(char)
    if not opening:
        return False
    digits = sum(char.isdigit() for char in opening)
    return digits * 2 > len(opening)


def _box_top_left(row: dict) -> tuple[float, float]:
    """Reading order: down the page, then across the line."""
    box = row.get("box")
    if isinstance(box, (list, tuple)) and len(box) >= 2:
        try:
            return (float(box[1]), float(box[0]))
        except (TypeError, ValueError):
            pass
    return (float("inf"), float("inf"))


def _heron_score(hit: dict) -> float:
    try:
        return float(hit.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _looks_like_heading(text: str) -> bool:
    """A title, not a sentence or a form line: at most six words, no digits, letters in it,
    no sentence punctuation at the end."""
    text = text.strip()
    if not text or len(text.split()) > _HEADING_MAX_WORDS:
        return False
    if any(char.isdigit() for char in text) or not any(char.isalpha() for char in text):
        return False
    return not text.endswith((".", ",", ";", "?", "!"))


def _is_heading(row: dict) -> bool:
    """Two of Heron, the common list and the ranker pass; or Heron alone at _HERON_STRONG
    on a box no rule rejected, whose text looks like a heading. Never a running header."""
    if str(row.get("det_class") or "") == "text_label":
        return False
    text = str(row.get("text") or "").strip()
    note = str(row.get("note") or "")
    if not text or _numeric_opening(text) or note == "running header":
        return False
    passed = (
        _heron_score(row) >= _HERON_MIN,
        common_heading_score(text) >= _COMMON_MIN,
        bool(row.get("accepted")),
    )
    strong = _heron_score(row) >= _HERON_STRONG and not note and _looks_like_heading(text)
    return sum(passed) >= 2 or strong


def _heading_like(row: dict) -> bool:
    """A rejected Heron box that still reads like a heading (drawn heavier than a form label):
    not a colon text label, not a running header, no numeric opening, heading-shaped text."""
    text = str(row.get("text") or "").strip()
    return (
        str(row.get("det_class") or "") != "text_label"
        and str(row.get("note") or "") != "running header"
        and not _numeric_opening(text)
        and _looks_like_heading(text)
    )


def _heading_rows(rows: object) -> list[dict]:
    if not isinstance(rows, list):
        return []
    found = [row for row in rows if isinstance(row, dict) and str(row.get("text") or "").strip()]
    found.sort(key=_box_top_left)
    return found


def _kept_headings(rows: object) -> list[dict]:
    """The headings (_is_heading), in reading order."""
    return [row for row in _heading_rows(rows) if _is_heading(row)]


def heron_headers(page: dict | None) -> list[OcrSectionHeader]:
    """Every Heron heading candidate as the same 0–1 overlay the page image already draws.
    Headings (_is_heading) come as they are; the rest are ``candidate``, ``heading_like``
    when they read like a heading (drawn a little lighter) and plain otherwise (lightest)."""
    if not isinstance(page, dict):
        return []
    try:
        page_w = float(page.get("width") or 0)
        page_h = float(page.get("height") or 0)
    except (TypeError, ValueError):
        return []
    if page_w <= 0 or page_h <= 0:
        return []
    fields = page.get("fields")
    rows = fields.get("heading_heron") if isinstance(fields, dict) else None
    headers: list[OcrSectionHeader] = []
    for hit in _heading_rows(rows):
        text = _display_header(str(hit.get("text") or ""))
        box = hit.get("box")
        if not text or not isinstance(box, (list, tuple)) or len(box) < 4:
            continue
        try:
            left, top, right, bottom = (float(part) for part in box[:4])
        except (TypeError, ValueError):
            continue
        if right <= left or bottom <= top:
            continue
        level = 1 if str(hit.get("level") or "").strip().lower() == "heading" else 2
        headers.append(
            OcrSectionHeader(
                text=text,
                level=level,
                left=_fraction(left, page_w),
                top=_fraction(top, page_h),
                width=_fraction(right - left, page_w),
                height=_fraction(bottom - top, page_h),
                candidate=not _is_heading(hit),
                heading_like=not _is_heading(hit) and _heading_like(hit),
            )
        )
    headers.sort(key=lambda header: (header.top, header.left))
    return headers


def _page_for(pages: dict, page_file: str) -> dict | None:
    if page_file in pages and isinstance(pages[page_file], dict):
        return pages[page_file]
    want = page_file.casefold()
    stem = Path(page_file).stem.casefold()
    for name, page in pages.items():
        if not isinstance(page, dict):
            continue
        if str(name).casefold() == want or Path(str(name)).stem.casefold() == stem:
            return page
    return None


def load_extraction_review(
    data_root: Path,
    folder_id: str,
    page_file: str,
) -> ExtractionReviewResponse:
    folder = _folder_dir(data_root, folder_id)
    path = folder / "staging" / "extraction.json"
    if not path.is_file():
        return ExtractionReviewResponse(
            folder_id=folder_id,
            available=False,
            page_file=page_file,
            fields=field_rows(None),
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ExtractionReviewResponse(
            folder_id=folder_id,
            available=False,
            page_file=page_file,
            fields=field_rows(None),
        )
    pages = payload.get("pages") if isinstance(payload, dict) else None
    page = _page_for(pages, page_file) if isinstance(pages, dict) else None
    return ExtractionReviewResponse(
        folder_id=folder_id,
        available=True,
        model_version=str(payload.get("model_version") or "") if isinstance(payload, dict) else "",
        page_file=page_file,
        fields=field_rows(page),
        section_headers=heron_headers(page),
    )


# Production Mode has no chart workspace, so no staging file. The extraction stage also
# writes each page's printed page number and its headings to additional_page_details;
# read from there, the Additional tab still shows Page Numbers and Section Headers.
_ADDITIONAL_SQL = """
    SELECT p.page_name, to_jsonb(a)
    FROM additional_page_details a
    JOIN page_list p ON p.id = a.page_id
    JOIN chart_list c ON c.id = a.chart_id
    WHERE c.chart_name = %s
"""


def _stored_headers(items: object) -> list[OcrSectionHeader]:
    """additional_page_details.section_headers (the stage's ``norm`` 0–1 boxes) as overlay
    headers, labelled the way the staging headers are."""
    headers: list[OcrSectionHeader] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text = _display_header(str(item.get("text") or ""))
        norm = item.get("norm")
        if not text or not isinstance(norm, dict):
            continue
        try:
            box = {key: float(norm[key]) for key in ("left", "top", "width", "height")}
            level = int(item.get("level") or 2)
        except (KeyError, TypeError, ValueError):
            continue
        if box["width"] <= 0 or box["height"] <= 0:
            continue
        headers.append(OcrSectionHeader(text=text, level=level, **box))
    headers.sort(key=lambda header: (header.top, header.left))
    return headers


def review_from_additional(
    folder_id: str, page_file: str, row: dict | None
) -> ExtractionReviewResponse:
    """One page's additional_page_details row as the Additional tab's response. A row with
    ``extraction_fields`` carries every staged field; an older row has only Page Number and
    Headings (every other field empty).

    ``heading_candidates`` (every heading row, accepted or not) goes through the same rules
    as the staging file, so the lighter candidates show too. A row written before it
    existed falls back to ``section_headers`` (the accepted headings only)."""
    if row is None:
        return ExtractionReviewResponse(
            folder_id=folder_id, available=False, page_file=page_file, fields=field_rows(None)
        )
    # Every staged field (2026-10-25): the same page the staging file holds, so the tab
    # reads exactly as in Local Mode.
    staged_page = row.get("extraction_fields")
    if isinstance(staged_page, dict) and staged_page.get("fields"):
        return ExtractionReviewResponse(
            folder_id=folder_id,
            available=True,
            model_version=str(staged_page.get("model_version") or ""),
            page_file=page_file,
            fields=field_rows(staged_page),
            section_headers=heron_headers(staged_page),
        )
    candidates = row.get("heading_candidates")
    staged = candidates if isinstance(candidates, dict) and candidates.get("fields") else None
    if staged is not None:
        headers = heron_headers(staged)
        kept = next((f for f in field_rows(staged) if f.id == "heading_heron"), None)
        names = kept.extracted if kept else ""
    else:
        headers = _stored_headers(row.get("section_headers"))
        names = " | ".join(header.text for header in headers)
    number = str(row.get("printed_page_no") or row.get("page_number_value") or "").strip()
    total = str(row.get("printed_page_total") or "").strip()
    page_no = f"{number} of {total}" if number and total else number
    try:
        confidence = float(row["confidence"]) if row.get("confidence") is not None else None
    except (TypeError, ValueError):
        confidence = None
    fields = []
    for field in field_rows(None):
        if field.id == "page_no" and page_no:
            field = field.model_copy(
                update={"extracted": page_no, "processed": page_no, "confidence": confidence}
            )
        elif field.id == "heading_heron" and names:
            field = field.model_copy(update={"extracted": names, "processed": names})
        fields.append(field)
    return ExtractionReviewResponse(
        folder_id=folder_id,
        available=True,
        page_file=page_file,
        fields=fields,
        section_headers=headers,
    )


def _additional_rows(database_url: str, db_schema: str, folder_id: str) -> dict[str, dict]:
    """The chart's additional_page_details rows by page name; {} when the table is missing
    or the database cannot be read (logged)."""
    from app.services.db import connection, table_exists

    try:
        if not table_exists(database_url, db_schema, "additional_page_details"):
            return {}
        with connection(database_url, db_schema) as conn:
            with conn.cursor() as cur:
                cur.execute(_ADDITIONAL_SQL, (folder_id,))
                return {str(name): row for name, row in cur.fetchall() if isinstance(row, dict)}
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "additional_page_details unreadable for %s: %s", folder_id, exc
        )
        return {}


def load_extraction_review_from_db(
    database_url: str, db_schema: str, folder_id: str, page_file: str
) -> ExtractionReviewResponse:
    """Production Mode's Additional tab, from additional_page_details. A database without
    the table, or one that cannot be read, answers 'not available' rather than failing."""
    pages = _additional_rows(database_url, db_schema, folder_id)
    return review_from_additional(folder_id, page_file, _page_for(pages, page_file))


def load_extraction_pages(data_root: Path, folder_id: str) -> ExtractionPagesResponse:
    """Local Mode: every staged page's review, read from extraction.json once."""
    path = _folder_dir(data_root, folder_id) / "staging" / "extraction.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        payload = None
    pages = payload.get("pages") if isinstance(payload, dict) else None
    if not isinstance(pages, dict):
        return ExtractionPagesResponse(folder_id=folder_id, available=False)
    version = str(payload.get("model_version") or "")
    return ExtractionPagesResponse(
        folder_id=folder_id,
        available=True,
        pages={
            str(name): ExtractionReviewResponse(
                folder_id=folder_id,
                available=True,
                model_version=version,
                page_file=str(name),
                fields=field_rows(page),
                section_headers=heron_headers(page),
            )
            for name, page in pages.items()
            if isinstance(page, dict)
        },
    )


def load_extraction_pages_from_db(
    database_url: str, db_schema: str, folder_id: str
) -> ExtractionPagesResponse:
    """Production Mode: every page's review from additional_page_details, one query."""
    rows = _additional_rows(database_url, db_schema, folder_id)
    return ExtractionPagesResponse(
        folder_id=folder_id,
        available=bool(rows),
        pages={name: review_from_additional(folder_id, name, row) for name, row in rows.items()},
    )
