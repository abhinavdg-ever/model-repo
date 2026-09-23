"""Term-frequency page-type / codeability classification.

Loads ``codeable_canon.json`` (page_type, tag, continue y/n, keywords) and
scores each page's OCR text by keyword hit frequency. Longer phrases weigh
more. Visit Report / Progress Note / Discharge Report families have
``continue=y``: once matched, their tag carries forward to later pages that
share the same page-level DOS until the DOS changes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

CANON_PATH = Path(__file__).with_name("codeable_canon.json")

TAG_DISPLAY = {
    "codeable": "Codeable",
    "non_codeable": "Non Codeable",
    "discharge_frequency": "Discharge Frequency",
    "not_sure": "Not Sure",
}

# Patient-data / demographic field phrases. Pages 1–2 with several of these
# (or any page with many) prefer page_type=Demographics over weaker matches.
DEMOGRAPHIC_KEYWORDS: tuple[str, ...] = (
    "patient information",
    "demographic",
    "demographics",
    "patient demographics",
    "date of birth",
    "dob",
    "patient name",
    "member name",
    "member id",
    "mrn",
    "medical record",
    "address",
    "phone number",
    "home phone",
    "cell phone",
    "emergency contact",
    "sex",
    "gender",
    "marital status",
    "insurance",
    "subscriber",
    "guarantor",
    "ssn",
    "social security",
    "face sheet",
    "facesheet",
    "registration",
)

DEMOGRAPHICS_PAGE_TYPE = "Demographics"
# Pages 1–2: this many distinct demographic hits → Demographics.
_DEMO_EARLY_PAGE_MIN_HITS = 2
# Any page: this many distinct hits → Demographics.
_DEMO_ANY_PAGE_MIN_HITS = 4
_EARLY_PAGE_MAX = 2

_WS_RE = re.compile(r"\s+")
_SLASH_RE = re.compile(r"\\+")


def normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").casefold()).strip()


def _clean_label(text: str) -> str:
    """CSV used backslashes as separators (Patient Information\\Demographic)."""
    return _SLASH_RE.sub(" / ", (text or "").strip()).strip(" /")


@dataclass(frozen=True)
class CanonEntry:
    page_type: str
    tag: str
    continue_: str  # "y" | "n"
    keywords: tuple[str, ...]

    @property
    def carries(self) -> bool:
        return self.continue_.casefold() in {"y", "yes", "true", "1"}


@dataclass(frozen=True)
class MatchResult:
    page_type: str
    tag: str
    confidence: float
    score: float
    continue_: str
    continue_applied: bool
    matched_keyword: str = ""

    @property
    def display_tag(self) -> str:
        return TAG_DISPLAY.get(self.tag, self.tag)


@lru_cache(maxsize=1)
def load_canon(path: str | None = None) -> tuple[CanonEntry, ...]:
    canon_path = Path(path) if path else CANON_PATH
    raw = json.loads(canon_path.read_text(encoding="utf-8"))
    display = raw.get("display") or {}
    if display:
        TAG_DISPLAY.update({str(k): str(v) for k, v in display.items()})
    entries: list[CanonEntry] = []
    for item in raw.get("entries") or []:
        page_type = _clean_label(str(item.get("page_type") or ""))
        tag = str(item.get("tag") or "").strip()
        if not page_type or not tag:
            continue
        kws = item.get("keywords") or [page_type]
        normalized = tuple(
            normalize_text(_clean_label(str(k)))
            for k in kws
            if normalize_text(_clean_label(str(k)))
        )
        if not normalized:
            continue
        entries.append(
            CanonEntry(
                page_type=page_type,
                tag=tag,
                continue_=str(item.get("continue") or "n").strip().casefold()[:1] or "n",
                keywords=normalized,
            )
        )
    return tuple(entries)


def demographic_hit_count(text: str) -> tuple[int, str]:
    """Distinct demographic keyword hits in ``text`` and the longest hit."""
    hay = normalize_text(text)
    if not hay:
        return 0, ""
    hits = 0
    best_kw = ""
    for kw in DEMOGRAPHIC_KEYWORDS:
        needle = normalize_text(kw)
        if needle and needle in hay:
            hits += 1
            if len(needle) > len(best_kw):
                best_kw = needle
    return hits, best_kw


def demographics_match(
    text: str,
    *,
    page_number: Optional[int] = None,
) -> Optional[MatchResult]:
    """Prefer Demographics when patient-data keywords cluster (esp. pages 1–2)."""
    hits, best_kw = demographic_hit_count(text)
    if hits <= 0:
        return None
    early = (
        page_number is not None
        and 1 <= int(page_number) <= _EARLY_PAGE_MAX
    )
    if early and hits >= _DEMO_EARLY_PAGE_MIN_HITS:
        pass
    elif hits >= _DEMO_ANY_PAGE_MIN_HITS:
        pass
    else:
        return None
    score = float(hits * hits)
    confidence = round(min(0.99, score / (score + 4.0)), 4)
    return MatchResult(
        page_type=DEMOGRAPHICS_PAGE_TYPE,
        tag="codeable",
        confidence=confidence,
        score=score,
        continue_="n",
        continue_applied=False,
        matched_keyword=best_kw,
    )


def _prefer_demographics(
    demo: MatchResult,
    other: Optional[MatchResult],
) -> MatchResult:
    """Demographics wins unless Progress/Office Visit (or stronger continue) also matched."""
    if other is None:
        return demo
    if _clinical_priority(other.page_type) > 0:
        return other
    if other.continue_ == "y" and other.score > demo.score:
        return other
    if other.page_type.casefold() in {
        DEMOGRAPHICS_PAGE_TYPE.casefold(),
        "face sheet / registration",
        "patient information / demographic",
    }:
        return other if other.score >= demo.score else demo
    return demo


def _phrase_weight(phrase: str) -> int:
    """Longer phrases beat short accidental tokens."""
    tokens = max(1, phrase.count(" ") + 1)
    return tokens * tokens


def _clinical_priority(page_type: str) -> int:
    """Prefer Progress Note / Office Visit when several types match.

    Higher wins. 0 = no clinical boost.
    """
    n = _clean_label(page_type).casefold()
    if "progress note" in n:
        return 100
    if "office visit" in n or n in {
        "visit report",
        "initial office note",
        "office visits/followup visits",
    }:
        return 90
    if n == "visit report" or n.startswith("visit report"):
        return 90
    return 0


def _better_match(a: MatchResult, b: MatchResult) -> MatchResult:
    """Pick the winner when two types both scored on the same page."""
    pa, pb = _clinical_priority(a.page_type), _clinical_priority(b.page_type)
    if pa != pb:
        return a if pa > pb else b
    if a.score != b.score:
        return a if a.score > b.score else b
    # Prefer continue=y, then longer page_type name, on remaining ties.
    if a.continue_ == "y" and b.continue_ != "y":
        return a
    if b.continue_ == "y" and a.continue_ != "y":
        return b
    if len(a.page_type) != len(b.page_type):
        return a if len(a.page_type) > len(b.page_type) else b
    return a


def score_text(text: str, entries: Sequence[CanonEntry] | None = None) -> Optional[MatchResult]:
    """Return the best TF keyword match for ``text``, or None."""
    hay = normalize_text(text)
    if not hay:
        return None
    catalog = entries if entries is not None else load_canon()
    best: Optional[MatchResult] = None
    for entry in catalog:
        score = 0.0
        best_kw = ""
        best_kw_hits = 0
        for kw in entry.keywords:
            hits = hay.count(kw)
            if not hits:
                continue
            score += hits * _phrase_weight(kw)
            if hits > best_kw_hits or (hits == best_kw_hits and len(kw) > len(best_kw)):
                best_kw = kw
                best_kw_hits = hits
        if score <= 0:
            continue
        # Softmax-ish confidence from raw score; capped for display.
        confidence = round(min(0.99, score / (score + 4.0)), 4)
        candidate = MatchResult(
            page_type=entry.page_type,
            tag=entry.tag,
            confidence=confidence,
            score=score,
            continue_="y" if entry.carries else "n",
            continue_applied=False,
            matched_keyword=best_kw,
        )
        best = candidate if best is None else _better_match(best, candidate)
    return best


def dos_key(
    dos_from: Optional[str] = None,
    dos_to: Optional[str] = None,
    *,
    page_id: Optional[int] = None,
) -> str:
    """Identity for continue-carry. Missing DOS → page-scoped (no carry)."""
    left = (dos_from or "").strip()
    right = (dos_to or "").strip() or left
    if left or right:
        return f"{left}|{right}"
    if page_id is not None:
        return f"page:{page_id}"
    return ""


@dataclass
class _CarryState:
    dos: str
    page_type: str
    tag: str
    continue_: str
    confidence: float


def classify_pages(
    pages: Iterable[dict[str, Any]],
    *,
    entries: Sequence[CanonEntry] | None = None,
) -> list[dict[str, Any]]:
    """Classify pages in order with DOS-scoped continue carry-forward.

    Each input dict needs:
      page_id, page_name, page_number (optional), text, dos_from, dos_to
    """
    catalog = entries if entries is not None else load_canon()
    carry: Optional[_CarryState] = None
    out: list[dict[str, Any]] = []

    for page in pages:
        text = str(page.get("text") or "")
        key = dos_key(
            page.get("dos_from"),
            page.get("dos_to"),
            page_id=page.get("page_id"),
        )
        if carry is not None and carry.dos != key:
            carry = None

        match = score_text(text, catalog)
        demo = demographics_match(
            text, page_number=page.get("page_number")
        )
        if demo is not None:
            match = _prefer_demographics(demo, match)

        continue_applied = False
        result: Optional[MatchResult] = match

        if match is not None and match.continue_ == "y":
            carry = _CarryState(
                dos=key,
                page_type=match.page_type,
                tag=match.tag,
                continue_="y",
                confidence=match.confidence,
            )
            result = match
        elif carry is not None and carry.dos == key:
            # Inherit Visit/Progress/Discharge tag for the rest of this DOS.
            result = MatchResult(
                page_type=carry.page_type,
                tag=carry.tag,
                confidence=carry.confidence,
                score=match.score if match else 0.0,
                continue_="y",
                continue_applied=True,
                matched_keyword=(match.matched_keyword if match else ""),
            )
            continue_applied = True
        elif match is None:
            result = None

        if result is None:
            page_type = "Not Available"
            tag = "not_sure"
            is_codeable = TAG_DISPLAY["not_sure"]
            confidence: Any = ""
            continue_flag = "n"
            matched_keyword = ""
            score: Any = 0.0
        else:
            page_type = result.page_type
            tag = result.tag
            is_codeable = result.display_tag
            confidence = result.confidence
            continue_flag = result.continue_
            matched_keyword = result.matched_keyword
            score = result.score

        row = {
            "page_id": page.get("page_id"),
            "page_name": page.get("page_name"),
            "page_number": page.get("page_number"),
            "dos_from": page.get("dos_from") or "",
            "dos_to": page.get("dos_to") or "",
            "page_type": page_type,
            "tag": tag,
            "is_codeable": is_codeable,
            "confidence": confidence,
            "continue": continue_flag,
            "continue_applied": "y" if continue_applied else "n",
            "matched_keyword": matched_keyword,
            "score": score,
        }
        out.append(row)
    return out
