"""Page type / codeability classification from ``codeable_canon.json``.

Every family has one tag — Codeable, Non Codeable or Discharge Frequency —
and each type takes its tag from its family, so a family never mixes them.
Each canon entry has keywords in three roles. ``primary`` and ``variants``
decide the type; ``supporting`` only adds score. Every hit is matched on word
boundaries and weighted by phrase length and by where it sits on the page — a
type name in the header is the document's title, the same phrase in the body is
usually a cross-reference.

The decision is made per family. A family's score is the sum of every hit of
every type in it; the highest-scoring family wins (ties go to the lower
priority number), as long as at least one of its hits is a primary or variant.
Inside that family each eligible type's share of the family's type scores is
its probability, and the most likely type wins. The output is
``Family (Page Type)``. ``confidence`` is the margin between the winning
family's score and the next family's; ``type_confidence`` is the chosen type's
probability within its family.

An entry with ``continue`` opens a span for its family. Later pages on the same
date inherit the family and tag; a page that ends the date, or carries no
date, ends the span. All the numbers live in the canon's ``matching`` block.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from stages.lib.canon_store import CANON_DIR, CanonFile

CANON_PATH = CANON_DIR / "codeable_canon.json"

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
_PAREN_RE = re.compile(r"\([^)]*\)")


def normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").casefold().replace("\\", "/")).strip()


def _strip_parentheticals(text: str) -> str:
    """Drop ``(...)`` annotations from display labels (and nested leftovers)."""
    prev = None
    while prev != text:
        prev = text
        text = _PAREN_RE.sub("", text)
    return text


def _clean_label(text: str) -> str:
    """Normalize separators; hide parenthetical annotations from page_type."""
    text = _SLASH_RE.sub(" / ", (text or "").strip())
    text = _strip_parentheticals(text)
    return _WS_RE.sub(" ", text).strip(" /")


def _phrase_re(phrase: str, word_boundary: bool) -> re.Pattern[str]:
    if not word_boundary:
        return re.compile(re.escape(phrase))
    return re.compile(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])")


# --- canon -------------------------------------------------------------------


@dataclass(frozen=True)
class Family:
    key: str
    display: str
    tag: str  # every type in the family is this tag
    priority: int
    span: bool


@dataclass(frozen=True)
class Phrase:
    text: str
    role: str  # "primary" | "variant" | "supporting"
    words: int
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class CanonEntry:
    id: str
    page_type: str
    family: str
    tag: str
    carries: bool
    phrases: tuple[Phrase, ...]

    @property
    def continue_(self) -> str:
        return "y" if self.carries else "n"


@dataclass(frozen=True)
class Matching:
    phrase_weight: dict[int, float]  # word count (4 = four or more) → weight
    header_fraction: float
    header_multiplier: float
    footer_fraction: float
    footer_multiplier: float
    supporting_can_decide: bool
    confidence_floor: float


@dataclass(frozen=True)
class Canon:
    entries: tuple[CanonEntry, ...]
    families: dict[str, Family]
    matching: Matching

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


class CanonError(ValueError):
    """The canon breaks a loader rule; the message names every offending entry."""


def _validate(raw: dict[str, Any]) -> None:
    families = raw.get("families") or {}
    tags = set(raw.get("tags") or [])
    single = set(raw.get("single_word_primary") or [])
    errors: list[str] = []
    owners: dict[str, list[str]] = {}
    ids: set[str] = set()
    for key, fam in families.items():
        if fam.get("tag") not in tags:
            errors.append(f"family {key}: unknown tag {fam.get('tag')!r}")
    for item in raw.get("entries") or []:
        eid = str(item.get("id") or "")
        if not eid:
            errors.append(f"entry {item.get('display')!r}: no id")
        elif eid in ids:
            errors.append(f"{eid}: duplicate id")
        ids.add(eid)
        primary = (item.get("match") or {}).get("primary") or []
        if not primary:
            errors.append(f"{eid}: no primary keyword")
        for phrase in primary:
            owners.setdefault(normalize_text(phrase), []).append(eid)
            if " " not in normalize_text(phrase) and normalize_text(phrase) not in single:
                errors.append(f"{eid}: one-word primary {phrase!r} not in single_word_primary")
        if item.get("family") not in families:
            errors.append(f"{eid}: unknown family {item.get('family')!r}")
        if "tag" in item:
            errors.append(f"{eid}: carries its own tag; the tag belongs to the family")
    for phrase, claimed in owners.items():
        if len(claimed) > 1:
            errors.append(f"primary {phrase!r} claimed by {claimed}")
    if errors:
        raise CanonError("codeable_canon.json: " + "; ".join(errors))


def _parse_canon(raw: dict[str, Any]) -> Canon:
    _validate(raw)
    display = raw.get("display") or {}
    if display:
        TAG_DISPLAY.update({str(k): str(v) for k, v in display.items()})

    m = raw["matching"]
    word_boundary = bool(m.get("word_boundary", True))
    weights = {int(k.rstrip("+")): float(v) for k, v in m["phrase_weight"].items()}
    matching = Matching(
        phrase_weight=weights,
        header_fraction=float(m["header_band"]["top_fraction"]),
        header_multiplier=float(m["header_band"]["multiplier"]),
        footer_fraction=float(m["footer_band"]["bottom_fraction"]),
        footer_multiplier=float(m["footer_band"]["multiplier"]),
        supporting_can_decide=bool(m.get("supporting_can_decide", False)),
        confidence_floor=float(m.get("confidence_floor", 0.0)),
    )
    families = {
        key: Family(
            key=key,
            display=str(f.get("display") or key),
            tag=str(f["tag"]),
            priority=int(f.get("priority", 100)),
            span=bool(f.get("span", False)),
        )
        for key, f in raw["families"].items()
    }

    entries: list[CanonEntry] = []
    for item in raw["entries"]:
        phrases: list[Phrase] = []
        seen: set[str] = set()
        for role, key in (("primary", "primary"), ("variant", "variants"), ("supporting", "supporting")):
            for kw in item["match"].get(key) or []:
                text = normalize_text(kw)
                if not text or text in seen:
                    continue
                seen.add(text)
                phrases.append(
                    Phrase(
                        text=text,
                        role=role,
                        words=len(text.split()),
                        pattern=_phrase_re(text, word_boundary),
                    )
                )
        entries.append(
            CanonEntry(
                id=str(item["id"]),
                page_type=_clean_label(str(item["display"])),
                family=str(item["family"]),
                tag=families[str(item["family"])].tag,
                carries=bool(item.get("continue", False)),
                phrases=tuple(phrases),
            )
        )
    return Canon(entries=tuple(entries), families=families, matching=matching)


# keyword-canon/codeable_canon.json — reloaded when the file changes. A bad
# edit is refused: the last good version keeps serving, and a bad file on
# first load raises.
_CANON: CanonFile[Canon] = CanonFile(CANON_PATH, _parse_canon)


def load_canon(path: str | None = None) -> Canon:
    """The live catalog; ``path`` reads a specific file instead (tests/tools)."""
    if path:
        return _parse_canon(json.loads(Path(path).read_text(encoding="utf-8")))
    return _CANON.get()


# Forces a re-read on next use.
load_canon.cache_clear = _CANON.reset  # type: ignore[attr-defined]


# --- scoring -----------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    entry_id: str
    phrase: str
    role: str
    band: str  # "header" | "body" | "footer"
    weight: float
    start: int = 0


@dataclass(frozen=True)
class MatchResult:
    page_type: str
    tag: str
    confidence: float
    score: float
    continue_: str
    continue_applied: bool
    matched_keyword: str = ""
    family: str = ""
    entry_id: str = ""
    hits: tuple[Hit, ...] = field(default=(), compare=False)
    family_scores: dict[str, float] = field(default_factory=dict, compare=False)
    # Within the family: the chosen type's share of the family's type scores,
    # and every candidate type's share.
    type_confidence: float = 1.0
    type_scores: dict[str, float] = field(default_factory=dict, compare=False)

    @property
    def display_tag(self) -> str:
        return TAG_DISPLAY.get(self.tag, self.tag)


def _band(position: int, length: int, m: Matching) -> tuple[str, float]:
    if position < m.header_fraction * length:
        return "header", m.header_multiplier
    if position >= (1.0 - m.footer_fraction) * length:
        return "footer", m.footer_multiplier
    return "body", 1.0


def _entry_hits(entry: CanonEntry, hay: str, m: Matching) -> list[Hit]:
    hits: list[Hit] = []
    for phrase in entry.phrases:
        if phrase.text not in hay:  # cheap reject before the regex
            continue
        base = m.phrase_weight[min(phrase.words, 4)]
        for found in phrase.pattern.finditer(hay):
            band, multiplier = _band(found.start(), len(hay), m)
            hits.append(
                Hit(entry.id, phrase.text, phrase.role, band, base * multiplier, found.start())
            )
    return hits


def score_text(text: str, canon: Canon | None = None) -> Optional[MatchResult]:
    """The winning family and its best type, or None when no family is eligible."""
    hay = normalize_text(text)
    if not hay:
        return None
    canon = canon if canon is not None else load_canon()
    m = canon.matching

    scored: list[tuple[CanonEntry, float, list[Hit]]] = []
    for entry in canon.entries:
        hits = _entry_hits(entry, hay, m)
        if hits:
            scored.append((entry, sum(h.weight for h in hits), hits))
    if not scored:
        return None

    # The decision is made per family: a family's score is every hit of every
    # type in it. A phrase two types share counts once at each position.
    occurrences: dict[str, dict[tuple[str, int], float]] = {}
    deciding_families: set[str] = set()
    for entry, _, entry_hits in scored:
        seen = occurrences.setdefault(entry.family, {})
        for h in entry_hits:
            key = (h.phrase, h.start)
            seen[key] = max(seen.get(key, 0.0), h.weight)
            if m.supporting_can_decide or h.role != "supporting":
                deciding_families.add(entry.family)
    if not deciding_families:
        return None
    family_scores = {f: sum(o.values()) for f, o in occurrences.items()}

    family = max(
        deciding_families,
        key=lambda f: (family_scores[f], -canon.families[f].priority),
    )
    # The type inside the winning family: each eligible type's share of the
    # family is its probability; the most likely one wins, then the longer name.
    candidates = [
        s for s in scored
        if s[0].family == family
        and (m.supporting_can_decide or any(h.role != "supporting" for h in s[2]))
    ]
    entry, entry_score, hits = max(candidates, key=lambda s: (s[1], len(s[0].page_type)))
    type_total = sum(s[1] for s in candidates)
    type_scores = {s[0].id: round(s[1] / type_total, 4) for s in candidates}
    score = family_scores[family]
    runner_up = max(
        (family_scores[f] for f in deciding_families if f != family), default=None
    )
    margin = 1.0 if runner_up is None else (score - runner_up) / score
    confidence = round(max(m.confidence_floor, margin), 4)

    deciding = [h for h in hits if h.role != "supporting"] or hits
    best_hit = max(deciding, key=lambda h: (h.weight, len(h.phrase)))
    return MatchResult(
        page_type=entry.page_type,
        tag=entry.tag,
        confidence=confidence,
        score=score,
        continue_=entry.continue_,
        continue_applied=False,
        matched_keyword=best_hit.phrase,
        family=entry.family,
        entry_id=entry.id,
        hits=tuple(h for _, _, hs in scored for h in hs),
        family_scores={f: round(v, 4) for f, v in family_scores.items()},
        type_confidence=round(entry_score / type_total, 4),
        type_scores=type_scores,
    )


# --- demographics ------------------------------------------------------------

_DEMOGRAPHIC_RES = tuple(
    (kw, _phrase_re(normalize_text(kw), True)) for kw in DEMOGRAPHIC_KEYWORDS
)


def demographic_hit_count(text: str) -> tuple[int, str]:
    """Distinct demographic keyword hits in ``text`` and the longest hit."""
    hay = normalize_text(text)
    if not hay:
        return 0, ""
    hits = 0
    best_kw = ""
    for kw, pattern in _DEMOGRAPHIC_RES:
        if pattern.search(hay):
            hits += 1
            if len(kw) > len(best_kw):
                best_kw = kw
    return hits, best_kw


def demographics_match(
    text: str,
    *,
    page_number: Optional[int] = None,
    canon: Canon | None = None,
) -> Optional[MatchResult]:
    """Prefer Demographics when patient-data keywords cluster (esp. pages 1–2)."""
    hits, best_kw = demographic_hit_count(text)
    if hits <= 0:
        return None
    early = page_number is not None and 1 <= int(page_number) <= _EARLY_PAGE_MAX
    if not (
        (early and hits >= _DEMO_EARLY_PAGE_MIN_HITS) or hits >= _DEMO_ANY_PAGE_MIN_HITS
    ):
        return None
    canon = canon if canon is not None else load_canon()
    entry = next((e for e in canon.entries if e.page_type == DEMOGRAPHICS_PAGE_TYPE), None)
    score = float(hits * hits)
    return MatchResult(
        page_type=DEMOGRAPHICS_PAGE_TYPE,
        tag=entry.tag if entry else "codeable",
        confidence=round(min(0.99, score / (score + 4.0)), 4),
        score=score,
        continue_="n",
        continue_applied=False,
        matched_keyword=best_kw,
        family=entry.family if entry else "",
        entry_id=entry.id if entry else "",
    )


def _prefer_demographics(
    demo: MatchResult,
    other: Optional[MatchResult],
    canon: Canon,
) -> MatchResult:
    """Demographics wins unless a span family (progress note, discharge) matched."""
    if other is None:
        return demo
    family = canon.families.get(other.family)
    if family is not None and family.span:
        return other
    if other.family == demo.family and other.page_type != DEMOGRAPHICS_PAGE_TYPE:
        # Face sheet / registration: the canon's own keywords decide.
        return other if other.score >= demo.score else demo
    return demo


def page_type_of(
    text: str,
    *,
    page_number: Optional[int] = None,
    entries: Canon | None = None,
) -> Optional[MatchResult]:
    """This page's own type, before any span carry-forward.

    DOS runs before this stage and reads page types through here, so the
    per-page decision must not depend on DOS.
    """
    canon = entries if entries is not None else load_canon()
    match = score_text(text, canon)
    demo = demographics_match(text, page_number=page_number, canon=canon)
    if demo is not None:
        match = _prefer_demographics(demo, match, canon)
    return match


# --- spans -------------------------------------------------------------------


def dos_key(
    dos_from: Optional[str] = None,
    dos_to: Optional[str] = None,
    *,
    page_id: Optional[int] = None,
) -> str:
    """Span identity. Missing DOS → page-scoped, so the page shares no span."""
    left = (dos_from or "").strip()
    right = (dos_to or "").strip() or left
    if left or right:
        return f"{left}|{right}"
    if page_id is not None:
        return f"page:{page_id}"
    return ""


@dataclass
class _Span:
    key: str
    family: str
    page_type: str
    tag: str
    confidence: float
    type_confidence: float


def classify_pages(
    pages: Iterable[dict[str, Any]],
    *,
    entries: Canon | None = None,
) -> list[dict[str, Any]]:
    """Classify pages in order with family spans.

    Each input dict needs:
      page_id, page_name, page_number (optional), text, dos_from, dos_to

    A span runs only while consecutive pages share its date, so a page on
    another date — or with no date — ends it.
    """
    canon = entries if entries is not None else load_canon()
    span: Optional[_Span] = None
    previous_family = ""
    out: list[dict[str, Any]] = []

    for page in pages:
        text = str(page.get("text") or "")
        key = dos_key(page.get("dos_from"), page.get("dos_to"), page_id=page.get("page_id"))
        if span is not None and span.key != key:
            span = None

        match = page_type_of(text, page_number=page.get("page_number"), entries=canon)
        result = match
        continue_applied = False

        if match is not None and match.continue_ == "y":
            span = _Span(
                key, match.family, match.page_type, match.tag, match.confidence,
                match.type_confidence,
            )
        elif span is not None:
            # Inherit the family and tag; keep the page's own type when it
            # matched something in the same family.
            own = match is not None and match.family == span.family
            result = MatchResult(
                page_type=match.page_type if own else span.page_type,
                tag=span.tag,
                confidence=match.confidence if own else span.confidence,
                score=match.score if match else 0.0,
                continue_="y",
                continue_applied=True,
                matched_keyword=match.matched_keyword if match else "",
                family=span.family,
                entry_id=match.entry_id if own else "",
                hits=match.hits if match else (),
                family_scores=match.family_scores if match else {},
                type_confidence=match.type_confidence if own else span.type_confidence,
                type_scores=match.type_scores if own else {},
            )
            continue_applied = True

        row = {
            "page_id": page.get("page_id"),
            "page_name": page.get("page_name"),
            "page_number": page.get("page_number"),
            "dos_from": page.get("dos_from") or "",
            "dos_to": page.get("dos_to") or "",
            "page_type": result.page_type if result else "Not Available",
            "tag": result.tag if result else "not_sure",
            "is_codeable": result.display_tag if result else TAG_DISPLAY["not_sure"],
            "confidence": result.confidence if result else "",
            "continue": result.continue_ if result else "n",
            "continue_applied": "y" if continue_applied else "n",
            "matched_keyword": result.matched_keyword if result else "",
            "score": result.score if result else 0.0,
            "family": result.family if result else "",
            "family_display": (
                canon.families[result.family].display
                if result and result.family in canon.families
                else ""
            ),
            "entry_id": result.entry_id if result else "",
            "type_confidence": result.type_confidence if result else "",
            "type_scores": result.type_scores if result else {},
            "previous_family": previous_family,
            "family_scores": result.family_scores if result else {},
            "hits": [
                {"entry_id": h.entry_id, "phrase": h.phrase, "role": h.role,
                 "band": h.band, "weight": h.weight}
                for h in (result.hits if result else ())
            ],
        }
        out.append(row)
        previous_family = row["family"]
    return out
