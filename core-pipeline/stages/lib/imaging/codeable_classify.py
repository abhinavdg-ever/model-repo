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
}

_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").casefold()).strip()


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
        page_type = str(item.get("page_type") or "").strip()
        tag = str(item.get("tag") or "").strip()
        if not page_type or not tag:
            continue
        kws = item.get("keywords") or [page_type]
        normalized = tuple(
            normalize_text(str(k)) for k in kws if normalize_text(str(k))
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


def _phrase_weight(phrase: str) -> int:
    """Longer phrases beat short accidental tokens."""
    tokens = max(1, phrase.count(" ") + 1)
    return tokens * tokens


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
        if best is None or candidate.score > best.score:
            best = candidate
            continue
        if candidate.score == best.score:
            # Prefer continue=y, then longer page_type name, on ties.
            if candidate.continue_ == "y" and best.continue_ != "y":
                best = candidate
            elif len(candidate.page_type) > len(best.page_type) and (
                candidate.continue_ == best.continue_
            ):
                best = candidate
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

        row = {
            "page_id": page.get("page_id"),
            "page_name": page.get("page_name"),
            "page_number": page.get("page_number"),
            "dos_from": page.get("dos_from") or "",
            "dos_to": page.get("dos_to") or "",
            "page_type": result.page_type if result else "",
            "tag": result.tag if result else "",
            "is_codeable": result.display_tag if result else "",
            "confidence": result.confidence if result else "",
            "continue": result.continue_ if result else "n",
            "continue_applied": "y" if continue_applied else "n",
            "matched_keyword": result.matched_keyword if result else "",
            "score": result.score if result else 0.0,
        }
        out.append(row)
    return out
