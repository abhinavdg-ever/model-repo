"""Page type / codeability classification from ``codeable_canon.json``.

The page-family model names a family when its top probability is at least
0.50. Otherwise a keyword family is used when its raw score is above 0.70.
Otherwise a family that sits in both top-3 lists is used. Otherwise the page
type is Others.

Every family has one tag — Codeable, Non Codeable or Discharge —
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
``Family (Page Type)``. ``confidence`` is assigned by the step that named the
family: the model's probability, the keyword family's lead over the next
family, the shared family's model probability, or 0 for Others.
``type_confidence`` is the chosen type's probability within its family.

An entry with ``continue`` opens a span for its family. Later pages on the same
date inherit the family and tag; a page that ends the date, or carries no
date, ends the span. All the numbers live in the canon's ``matching`` block.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Optional

from stages.lib.canon_store import CANON_DIR, CanonFile
from stages.lib.page_classify.family_model import KEYWORD_WIN

CANON_PATH = CANON_DIR / "codeable_canon.json"

TAG_DISPLAY = {
    "codeable": "Codeable",
    "non_codeable": "Non Codeable",
    "discharge_frequency": "Discharge",
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

# The patient banner every page carries. These never make a page Demographics:
# only registration fields (address, phone, insurance, …) count toward it.
BANNER_KEYWORDS: frozenset[str] = frozenset({
    "patient name",
    "member name",
    "member id",
    "date of birth",
    "dob",
    "mrn",
    "medical record",
    "sex",
    "gender",
})

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
    # Spaces are optional: OCR often glues headings ("reasonforvisit").
    body = r"\s*".join(re.escape(word) for word in phrase.split())
    if not word_boundary:
        return re.compile(body)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")


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
    compact: str = ""  # the phrase without spaces, for the cheap pre-check


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
    # A one-word primary ("hematology", "cardiology") decides only in the
    # header band; in the body it is usually a ROS line or history, not a title.
    single_word_header_only: bool
    single_word_any_band: frozenset[str]  # section headers exempt from that rule
    confidence_floor: float
    # family → score at which it wins outright, whatever the others score.
    dominant_families: dict[str, float]
    # Families that fill an unmatched page sitting between two of their pages.
    fill_between_families: frozenset[str]
    # Inside a span, a page whose own family is another one, and which has no
    # primary or variant hit of the span's family, keeps its own type and ends
    # the span once its family scores at least this much.
    span_break_score: float = float("inf")


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
    for word in raw.get("single_word_any_band") or []:
        if word not in single:
            errors.append(f"single_word_any_band {word!r} is not in single_word_primary")
    owners: dict[str, list[str]] = {}
    ids: set[str] = set()
    matching = raw.get("matching") or {}
    for key in list(matching.get("dominant_families") or {}) + list(
        matching.get("fill_between_families") or []
    ):
        if key not in families:
            errors.append(f"matching: unknown family {key!r}")
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
        single_word_header_only=bool(m.get("single_word_primary_header_only", False)),
        single_word_any_band=frozenset(
            normalize_text(w) for w in raw.get("single_word_any_band") or []
        ),
        confidence_floor=float(m.get("confidence_floor", 0.0)),
        dominant_families={
            str(k): float(v) for k, v in (m.get("dominant_families") or {}).items()
        },
        fill_between_families=frozenset(m.get("fill_between_families") or []),
        span_break_score=float(m.get("span_break_score", float("inf"))),
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
                        compact=text.replace(" ", ""),
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
    # Families with at least one primary or variant hit on the page.
    deciding_families: frozenset[str] = field(default=frozenset(), compare=False)

    @property
    def display_tag(self) -> str:
        return TAG_DISPLAY.get(self.tag, self.tag)


def _band(position: int, length: int, m: Matching) -> tuple[str, float]:
    if position < m.header_fraction * length:
        return "header", m.header_multiplier
    if position >= (1.0 - m.footer_fraction) * length:
        return "footer", m.footer_multiplier
    return "body", 1.0


def _entry_hits(
    entry: CanonEntry, hay: str, m: Matching, compact: Optional[str] = None
) -> list[Hit]:
    compact = hay.replace(" ", "") if compact is None else compact
    hits: list[Hit] = []
    for phrase in entry.phrases:
        if phrase.compact not in compact:  # cheap reject before the regex
            continue
        base = m.phrase_weight[min(phrase.words, 4)]
        for found in phrase.pattern.finditer(hay):
            band, multiplier = _band(found.start(), len(hay), m)
            role = phrase.role
            if (
                m.single_word_header_only
                and phrase.words == 1
                and phrase.text not in m.single_word_any_band
                and role != "supporting"
                and band != "header"
            ):
                role = "supporting"
            hits.append(
                Hit(entry.id, phrase.text, role, band, base * multiplier, found.start())
            )
    return hits


def _family_by_label(canon: Canon, label: str) -> Optional[Family]:
    folded = label.casefold()
    for family in canon.families.values():
        if family.key.casefold() == folded or family.display.casefold() == folded:
            return family
    return None


def _family_only_match(family: Family, confidence: float, canon: Canon) -> MatchResult:
    """No subtype keyword hit: the subtype is the family name."""
    return MatchResult(
        page_type=family.display,
        tag=family.tag,
        confidence=round(confidence, 4),
        score=canon.matching.span_break_score,
        continue_="y" if family.span else "n",
        continue_applied=False,
        family=family.key,
        type_confidence=1.0,
    )


def score_text(
    text: str,
    canon: Canon | None = None,
    *,
    only_family: str | None = None,
) -> Optional[MatchResult]:
    """The winning family and its best type, or None when no family is eligible.

    ``only_family`` scores subtypes inside that family and ignores the rest.
    """
    hay = normalize_text(text)
    if not hay:
        return None
    canon = canon if canon is not None else load_canon()
    m = canon.matching

    compact = hay.replace(" ", "")
    scored: list[tuple[CanonEntry, float, list[Hit]]] = []
    for entry in canon.entries:
        if only_family is not None and entry.family != only_family:
            continue
        hits = _entry_hits(entry, hay, m, compact)
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

    # A family past its dominance threshold wins outright (the strongest of
    # them if several are); otherwise the highest family score wins.
    dominant = [
        f for f in deciding_families
        if f in m.dominant_families and family_scores[f] >= m.dominant_families[f]
    ]
    family = max(
        dominant or deciding_families,
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
        deciding_families=frozenset(deciding_families),
    )


# --- demographics ------------------------------------------------------------

_DEMOGRAPHIC_RES = tuple(
    (kw, _phrase_re(normalize_text(kw), True))
    for kw in DEMOGRAPHIC_KEYWORDS
    if kw not in BANNER_KEYWORDS
)


def demographic_hit_count(text: str) -> tuple[int, str]:
    """Distinct registration-field hits in ``text`` and the longest hit.

    Banner fields (name, DOB, sex, MRN, member id) are not counted.
    """
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


def _breaks_span(match: Optional[MatchResult], span: _Span, m: Matching) -> bool:
    """Another family wins here, the span's family is not a candidate, and it is strong."""
    return (
        match is not None
        and match.family != span.family
        and span.family not in match.deciding_families
        and match.score >= m.span_break_score
    )


OTHERS_TYPE = "Others"


def keyword_family_ranking(text: str, canon: Canon, limit: int = 3) -> list[str]:
    """Family keys with a primary or variant hit, highest keyword score first."""
    hay = normalize_text(text)
    if not hay:
        return []
    compact = hay.replace(" ", "")
    occurrences: dict[str, dict[tuple[str, int], float]] = {}
    deciding: set[str] = set()
    for entry in canon.entries:
        hits = _entry_hits(entry, hay, canon.matching, compact)
        if not hits:
            continue
        seen = occurrences.setdefault(entry.family, {})
        for hit in hits:
            key = (hit.phrase, hit.start)
            seen[key] = max(seen.get(key, 0.0), hit.weight)
            if canon.matching.supporting_can_decide or hit.role != "supporting":
                deciding.add(entry.family)
    scores = {family: sum(seen.values()) for family, seen in occurrences.items()}
    ranked = sorted(
        deciding,
        key=lambda family: (scores[family], -canon.families[family].priority),
        reverse=True,
    )
    return ranked[:limit]


def _others_match() -> MatchResult:
    return MatchResult(
        page_type=OTHERS_TYPE,
        tag="not_sure",
        confidence=0.0,
        score=0.0,
        continue_="n",
        continue_applied=False,
        family="others",
    )


def _model_top_entries(page: dict[str, Any]) -> list[tuple[str, float]]:
    """Up to three ``(family label, probability)`` pairs from the model."""
    entries: list[tuple[str, float]] = []
    for item in (page.get("model_top") or [])[:3]:
        if isinstance(item, dict):
            label = str(item.get("page_family") or "").strip()
            score = float(item.get("score") or 0.0)
        else:
            label, score = str(item).strip(), 0.0
        if label:
            entries.append((label, score))
    return entries


def _match_from_agreement(
    text: str, page: dict[str, Any], canon: Canon
) -> tuple[MatchResult, str]:
    """Keep a family that sits in both top-3 lists."""
    model_keys: list[tuple[str, float]] = []
    for label, score in _model_top_entries(page):
        family = _family_by_label(canon, label)
        if family is None or any(key == family.key for key, _ in model_keys):
            continue
        model_keys.append((family.key, score))
    keyword_keys = set(keyword_family_ranking(text, canon, 3))
    shared = next(
        ((key, score) for key, score in model_keys if key in keyword_keys),
        None,
    )
    if shared is None:
        return _others_match(), "others"
    key, confidence = shared
    family = canon.families[key]
    typed = score_text(text, canon, only_family=family.key)
    if typed is None:
        return _family_only_match(family, confidence, canon), "agree"
    return (
        replace(
            typed,
            confidence=round(confidence, 4),
            score=max(typed.score, canon.matching.span_break_score),
        ),
        "agree",
    )


def _match_from_model(
    text: str, page: dict[str, Any], canon: Canon
) -> tuple[Optional[MatchResult], str]:
    """A committed model family. Keywords then name the subtype, or the family name."""
    label = str(page.get("model_family") or "").strip()
    if not label:
        return None, ""
    family = _family_by_label(canon, label)
    if family is None:
        return None, ""
    confidence = float(page.get("model_confidence") or 0.0)
    typed = score_text(text, canon, only_family=family.key)
    if typed is None:
        return _family_only_match(family, confidence, canon), "model"
    return (
        replace(
            typed,
            confidence=round(confidence, 4),
            score=max(typed.score, canon.matching.span_break_score),
        ),
        "model",
    )


def _family_confidence(
    source: str, page: dict[str, Any], match: MatchResult
) -> float:
    """Family confidence stored for the step that named the family.

    model: that family's probability.
    keywords: its lead over the next keyword family, already on the match.
    agree: the shared family's model probability, already on the match.
    others: 0.
    """
    if source == "model":
        return round(float(page.get("model_confidence") or 0.0), 4)
    if source == "others":
        return 0.0
    return round(float(match.confidence), 4)


def classify_pages(
    pages: Iterable[dict[str, Any]],
    *,
    entries: Canon | None = None,
) -> list[dict[str, Any]]:
    """Classify pages in order with family spans.

    Each input dict needs:
      page_id, page_name, page_number (optional), text, dos_from, dos_to

    ``model_family`` (display name) and ``model_confidence``, when set by the
    page-family model, fix the family. Keywords then choose the subtype.

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

        match, source = _match_from_model(text, page, canon)
        if match is None and page.get("model_top"):
            keyword = page_type_of(text, page_number=page.get("page_number"), entries=canon)
            if keyword is not None and keyword.score > KEYWORD_WIN:
                match, source = keyword, "keywords"
            else:
                match, source = _match_from_agreement(text, page, canon)
        elif match is None:
            match = page_type_of(text, page_number=page.get("page_number"), entries=canon)
            source = "keywords" if match is not None else ""
        if match is not None:
            match = replace(match, confidence=_family_confidence(source, page, match))
        result = match
        continue_applied = False

        if source == "others":
            span = None
        elif match is not None and match.continue_ == "y":
            span = _Span(
                key, match.family, match.page_type, match.tag, match.confidence,
                match.type_confidence,
            )
        elif span is not None and _breaks_span(match, span, canon.matching):
            # A different document starts here; later pages stop inheriting.
            span = None
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
        if continue_applied and source != "model":
            source = "span"

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
            "family_source": source,
            "family_display": (
                "Others"
                if result and result.family == "others"
                else canon.families[result.family].display
                if result and result.family in canon.families
                else ""
            ),
            "entry_id": result.entry_id if result else "",
            "type_confidence": result.type_confidence if result else "",
            "type_scores": result.type_scores if result else {},
            "previous_family": previous_family,
            "filled_between": False,
            "family_scores": result.family_scores if result else {},
            "hits": [
                {"entry_id": h.entry_id, "phrase": h.phrase, "role": h.role,
                 "band": h.band, "weight": h.weight}
                for h in (result.hits if result else ())
            ],
        }
        out.append(row)
        previous_family = row["family"]
    _fill_between(out, canon)
    return out


def _fill_between(rows: list[dict[str, Any]], canon: Canon) -> None:
    """An unmatched page between two pages of a fill family takes that family.

    Runs after spans, on the final rows, so a neighbour that inherited its
    family from a span counts. The page takes the previous page's type and the
    lower of the two neighbours' confidences.
    """
    families = canon.matching.fill_between_families
    for i in range(1, len(rows) - 1):
        row, before, after = rows[i], rows[i - 1], rows[i + 1]
        if row["family"] or row.get("family_source") == "others" or before["family"] not in families:
            continue
        if after["family"] != before["family"]:
            continue
        confidences = [
            c for c in (before["confidence"], after["confidence"]) if c not in ("", None)
        ]
        row.update(
            {
                "page_type": before["page_type"],
                "tag": before["tag"],
                "is_codeable": before["is_codeable"],
                "confidence": min(confidences) if confidences else "",
                "continue": "n",
                "continue_applied": "y",
                "family": before["family"],
                "family_display": before["family_display"],
                "family_source": "span",
                "entry_id": before["entry_id"],
                "type_confidence": before["type_confidence"],
                "filled_between": True,
            }
        )
