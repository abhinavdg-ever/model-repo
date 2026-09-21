"""Encounter-type classification (one type for an entire DOS).

Loads ``encounter_canon.json`` and scores OCR text by keyword term frequency.
Pages that share the same page-level DOS receive the same encounter type:
scores are aggregated across the DOS, then every page in that DOS is stamped
with the winner.

``establishes=false`` keywords (diagnostic imaging) never set a type on their
own — they only count when the DOS already has establishing evidence for a
type (typically Outpatient F2F), so imaging that rides along a face-to-face
visit stays under that encounter.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

CANON_PATH = Path(__file__).with_name("encounter_canon.json")

TYPE_DISPLAY = {
    "outpatient_f2f": "Outpatient (F2F)",
    "outpatient_tele": "Outpatient (Tele)",
    "inpatient": "Inpatient",
    "home": "Home",
}

# Prefer more specific settings on near-ties.
TYPE_PRIORITY = {
    "home": 4,
    "outpatient_tele": 3,
    "inpatient": 2,
    "outpatient_f2f": 1,
}

_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").casefold()).strip()


@dataclass(frozen=True)
class CanonEntry:
    encounter_type: str
    label: str
    establishes: bool
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class TypeScore:
    encounter_type: str
    score: float
    establishing_score: float
    matched_keyword: str


def dos_key(
    dos_from: Optional[str] = None,
    dos_to: Optional[str] = None,
    *,
    page_id: Optional[int] = None,
) -> str:
    left = (dos_from or "").strip()
    right = (dos_to or "").strip() or left
    if left or right:
        return f"{left}|{right}"
    if page_id is not None:
        return f"page:{page_id}"
    return ""


@lru_cache(maxsize=1)
def load_canon(path: str | None = None) -> tuple[CanonEntry, ...]:
    canon_path = Path(path) if path else CANON_PATH
    raw = json.loads(canon_path.read_text(encoding="utf-8"))
    display = raw.get("display") or {}
    if display:
        TYPE_DISPLAY.update({str(k): str(v) for k, v in display.items()})
    entries: list[CanonEntry] = []
    for item in raw.get("entries") or []:
        et = str(item.get("encounter_type") or "").strip()
        if not et:
            continue
        kws = tuple(
            normalize_text(str(k))
            for k in (item.get("keywords") or [])
            if normalize_text(str(k))
        )
        if not kws:
            continue
        establishes = item.get("establishes", True)
        entries.append(
            CanonEntry(
                encounter_type=et,
                label=str(item.get("label") or et),
                establishes=bool(establishes),
                keywords=kws,
            )
        )
    return tuple(entries)


def _phrase_weight(phrase: str) -> int:
    tokens = max(1, phrase.count(" ") + 1)
    return tokens * tokens


def score_text(
    text: str, entries: Sequence[CanonEntry] | None = None
) -> dict[str, TypeScore]:
    """Per-type TF scores for one text blob."""
    hay = normalize_text(text)
    catalog = entries if entries is not None else load_canon()
    totals: dict[str, float] = {}
    establishing: dict[str, float] = {}
    best_kw: dict[str, tuple[float, str]] = {}

    if not hay:
        return {}

    for entry in catalog:
        et = entry.encounter_type
        for kw in entry.keywords:
            hits = hay.count(kw)
            if not hits:
                continue
            add = hits * _phrase_weight(kw)
            totals[et] = totals.get(et, 0.0) + add
            if entry.establishes:
                establishing[et] = establishing.get(et, 0.0) + add
            prev = best_kw.get(et)
            if prev is None or add > prev[0] or (add == prev[0] and len(kw) > len(prev[1])):
                best_kw[et] = (add, kw)

    out: dict[str, TypeScore] = {}
    for et, score in totals.items():
        out[et] = TypeScore(
            encounter_type=et,
            score=score,
            establishing_score=establishing.get(et, 0.0),
            matched_keyword=best_kw.get(et, (0.0, ""))[1],
        )
    return out


def pick_encounter(scores: dict[str, TypeScore]) -> Optional[TypeScore]:
    """Choose a type that has establishing evidence (score > 0)."""
    candidates = [
        s for s in scores.values() if s.establishing_score > 0
    ]
    if not candidates:
        return None

    def _key(s: TypeScore) -> tuple[float, int]:
        return (s.establishing_score, TYPE_PRIORITY.get(s.encounter_type, 0))

    return max(candidates, key=_key)


def classify_pages(
    pages: Iterable[dict[str, Any]],
    *,
    entries: Sequence[CanonEntry] | None = None,
) -> list[dict[str, Any]]:
    """Classify pages so every page in a DOS gets the same encounter type.

    Each input dict needs:
      page_id, page_name, page_number (optional), text, dos_from, dos_to
    """
    catalog = entries if entries is not None else load_canon()
    page_list = list(pages)

    # Aggregate scores per DOS.
    dos_scores: dict[str, dict[str, TypeScore]] = {}
    dos_texts: dict[str, list[str]] = {}
    for page in page_list:
        key = dos_key(
            page.get("dos_from"),
            page.get("dos_to"),
            page_id=page.get("page_id"),
        )
        dos_texts.setdefault(key, []).append(str(page.get("text") or ""))

    for key, texts in dos_texts.items():
        combined = "\n".join(texts)
        dos_scores[key] = score_text(combined, catalog)

    # If diagnostic-imaging-only IP score exists with no establishing IP, drop it
    # (establishes=false already excluded from pick). F2F parent keeps the DOS.
    dos_winner: dict[str, Optional[TypeScore]] = {
        key: pick_encounter(scores) for key, scores in dos_scores.items()
    }

    out: list[dict[str, Any]] = []
    for page in page_list:
        key = dos_key(
            page.get("dos_from"),
            page.get("dos_to"),
            page_id=page.get("page_id"),
        )
        winner = dos_winner.get(key)
        page_scores = score_text(str(page.get("text") or ""), catalog)
        page_hit = pick_encounter(page_scores)
        continue_applied = bool(
            winner is not None
            and (page_hit is None or page_hit.encounter_type != winner.encounter_type)
        )
        conf = 0.0
        if winner is not None:
            conf = round(
                min(0.99, winner.establishing_score / (winner.establishing_score + 4.0)),
                4,
            )
        out.append(
            {
                "page_id": page.get("page_id"),
                "page_name": page.get("page_name"),
                "page_number": page.get("page_number"),
                "dos_from": page.get("dos_from") or "",
                "dos_to": page.get("dos_to") or "",
                "encounter_type": winner.encounter_type if winner else "",
                "encounter_label": (
                    TYPE_DISPLAY.get(winner.encounter_type, winner.encounter_type)
                    if winner
                    else ""
                ),
                "confidence": conf if winner else "",
                "matched_keyword": winner.matched_keyword if winner else "",
                "continue_applied": "y" if continue_applied and winner else "n",
                "score": winner.establishing_score if winner else 0.0,
            }
        )
    return out
