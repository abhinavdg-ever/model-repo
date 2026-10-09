"""Read a chart's staged key/value extraction for the Review pane.

The values are the ones the extraction stage selected. Nothing here applies
member verification, the DOS scorer, or any other later-stage rewrite.
Processed is the same string as extracted until that rewrite exists.
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException

from app.core.schemas import ExtractionFieldRow, ExtractionReviewResponse, OcrSectionHeader
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


def field_rows(page: dict | None) -> list[ExtractionFieldRow]:
    fields = (page or {}).get("fields") if isinstance(page, dict) else None
    if not isinstance(fields, dict):
        fields = {}
    rows: list[ExtractionFieldRow] = []
    for field_id, label in _FIELDS:
        chosen = _chosen(fields.get(field_id))
        if field_id == "heading_heron":
            chosen = _kept_headings(fields.get(field_id))
        extracted = _join(_hit_text(field_id, hit) for hit in chosen)
        rows.append(
            ExtractionFieldRow(
                id=field_id,
                label=label,
                extracted=extracted,
                processed=extracted,
                confidence=_confidence(chosen),
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
    return None


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
    key = _common_key(text)
    known = _common_headings()
    if not key or not known:
        return 1.0 if key and not known else 0.0
    if key in known or _contains_heading(key, known):
        return 1.0
    if len(key) < _FUZZY_MIN_CHARS:
        return 0.0
    return max(
        (
            SequenceMatcher(None, key, item).ratio()
            for item in known
            if len(item) >= _FUZZY_MIN_CHARS and abs(len(item) - len(key)) <= 0.25 * len(key) + 2
        ),
        default=0.0,
    )


def _proper_piece(piece: str) -> str:
    """Title-case a shouted word. A short acronym (CC, HPI) and mixed forms (PMHx) stay."""
    letters = [char for char in piece if char.isalpha()]
    if letters and all(char.isupper() for char in letters) and len(letters) > 3:
        return piece[:1].upper() + piece[1:].lower()
    return piece


def _display_header(text: str) -> str:
    """The label a reviewer sees: proper case, with punctuation removed."""
    cleaned = re.sub(r"[^A-Za-z0-9/]+", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    words = []
    for word in cleaned.split(" "):
        words.append("/".join(_proper_piece(piece) for piece in word.split("/")))
    return " ".join(words)


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


def _kept_headings(rows: object) -> list[dict]:
    """A heading stays when two of Heron, the common list, and the ranker pass."""
    if not isinstance(rows, list):
        return []
    kept: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("det_class") or "") == "text_label":
            continue
        text = str(row.get("text") or "").strip()
        if not text or _numeric_opening(text):
            continue
        passed = (
            _heron_score(row) >= _HERON_MIN,
            common_heading_score(text) >= _COMMON_MIN,
            bool(row.get("accepted")),
        )
        if sum(passed) >= 2:
            kept.append(row)
    kept.sort(key=_box_top_left)
    return kept


def heron_headers(page: dict | None) -> list[OcrSectionHeader]:
    """Heron heading boxes as the same 0–1 overlay the page image already draws."""
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
    for hit in _kept_headings(rows):
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
