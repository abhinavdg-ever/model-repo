"""Encounter setting per visit: Outpatient (F2F), Outpatient (Tele), Inpatient, Home.

Evidence is ranked, not added up. The stage finds the most authoritative
evidence a visit has and decides from that alone, so twenty passing mentions of
"follow up" never outweigh one discharge summary.

  1. Group pages into visits: runs of consecutive pages sharing a usable date.
     The DOS default is not a date; a page without one is a visit of its own,
     left unresolved.
  2. Gather findings, each recorded once however often its words appear:
       tier 1  a matched page type that exists in one setting only
       tier 2  text that names the setting ("telehealth", "hospital course")
       tier 3  hints that also occur in other settings ("follow up")
     Negatives remove the tier 2/3 findings they name. Context phrases are
     logged and never score.
  3. Decide: the highest non-empty tier of tiers 1–2 decides alone. Two
     settings in that tier is a conflict: more tier 3 hints wins, then the
     setting priority, and the visit is flagged for review.
  4. Stamp every page of the visit with the answer. An unresolved visit stays
     empty — it never inherits from a neighbouring page.

Every phrase, tier and number lives in ``encounter_canon.json``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from stages.lib.canon_store import CANON_DIR, CanonFile

CANON_PATH = CANON_DIR / "encounter_canon.json"

_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").casefold()).strip()


def _phrase_re(phrase: str) -> re.Pattern[str]:
    """Word boundaries only where the phrase starts or ends with a letter or
    digit, so ``a/p:`` and ``hpi:`` still match when text follows the colon."""
    lead = r"(?<![a-z0-9])" if phrase[:1].isalnum() else ""
    trail = r"(?![a-z0-9])" if phrase[-1:].isalnum() else ""
    return re.compile(lead + re.escape(phrase) + trail)


# --- canon -------------------------------------------------------------------


@dataclass(frozen=True)
class Phrase:
    text: str
    setting: str  # empty for context phrases
    tier: int  # 2, 3; 0 for context
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Negative:
    text: str
    pattern: re.Pattern[str]
    cancels: frozenset[str]
    cancels_setting: str


@dataclass(frozen=True)
class Canon:
    labels: dict[str, str]
    priority: dict[str, int]
    confidence: dict[str, float]
    tier1: dict[str, str]  # page type id → setting
    phrases: tuple[Phrase, ...]
    negatives: tuple[Negative, ...]


class CanonError(ValueError):
    """The canon breaks a loader rule; the message names every offence."""


def _page_type_ids() -> set[str]:
    """Every page type and sub-type name in page_taxonomy.json."""
    from stages.lib.page_classify import taxonomy

    names = taxonomy.load()
    return set(names.page_types) | set(names.subtype_page_type)


def _validate(raw: dict[str, Any], page_type_ids: Optional[set[str]]) -> None:
    settings = set(raw.get("settings") or {})
    errors: list[str] = []

    for pt_id, setting in (raw.get("tier1_page_types") or {}).items():
        if setting not in settings:
            errors.append(f"tier1_page_types.{pt_id}: unknown setting {setting!r}")
        if page_type_ids is not None and pt_id not in page_type_ids:
            errors.append(f"tier1_page_types.{pt_id}: not a page type or sub-type in page_taxonomy.json")

    owner: dict[str, str] = {}
    for tier in ("tier2", "tier3"):
        for setting, phrases in (raw.get(tier) or {}).items():
            if setting not in settings:
                errors.append(f"{tier}.{setting}: unknown setting")
            for phrase in phrases:
                key = normalize_text(phrase)
                where = f"{tier}.{setting}"
                if key in owner and owner[key] != where:
                    errors.append(f"phrase {phrase!r} is in both {owner[key]} and {where}")
                owner.setdefault(key, where)
    for phrase in raw.get("context") or []:
        key = normalize_text(phrase)
        if key in owner:
            errors.append(f"context phrase {phrase!r} also scores in {owner[key]}")

    for neg in raw.get("negatives") or []:
        for cancelled in neg.get("cancels") or []:
            if normalize_text(cancelled) not in owner:
                errors.append(
                    f"negative {neg.get('phrase')!r} cancels {cancelled!r}, "
                    "which is not a tier 2 or tier 3 phrase"
                )
        cs = neg.get("cancels_setting")
        if cs and cs not in settings:
            errors.append(f"negative {neg.get('phrase')!r}: unknown setting {cs!r}")
        if not neg.get("cancels") and not cs:
            errors.append(f"negative {neg.get('phrase')!r} cancels nothing")

    for key in ("tier1", "tier1_conflict", "tier2", "tier2_conflict"):
        if key not in (raw.get("confidence") or {}):
            errors.append(f"confidence.{key} is missing")

    if errors:
        raise CanonError("encounter_canon.json: " + "; ".join(errors))


def _parse_canon(raw: dict[str, Any], *, check_page_types: bool = True) -> Canon:
    _validate(raw, _page_type_ids() if check_page_types else None)
    settings = raw["settings"]
    phrases: list[Phrase] = []
    for tier_name, tier in (("tier2", 2), ("tier3", 3)):
        for setting, items in raw[tier_name].items():
            for text in items:
                key = normalize_text(text)
                phrases.append(Phrase(key, setting, tier, _phrase_re(key)))
    for text in raw.get("context") or []:
        key = normalize_text(text)
        phrases.append(Phrase(key, "", 0, _phrase_re(key)))
    negatives = tuple(
        Negative(
            text=normalize_text(n["phrase"]),
            pattern=_phrase_re(normalize_text(n["phrase"])),
            cancels=frozenset(normalize_text(c) for c in n.get("cancels") or []),
            cancels_setting=str(n.get("cancels_setting") or ""),
        )
        for n in raw.get("negatives") or []
    )
    return Canon(
        labels={k: str(v["label"]) for k, v in settings.items()},
        priority={k: int(v["priority"]) for k, v in settings.items()},
        confidence={k: float(v) for k, v in raw["confidence"].items()},
        tier1=dict(raw["tier1_page_types"]),
        phrases=tuple(phrases),
        negatives=negatives,
    )


# keyword-canon/encounter_canon.json — reloaded when the file changes. A bad
# edit is refused; the last good version keeps serving.
_CANON: CanonFile[Canon] = CanonFile(CANON_PATH, _parse_canon)


def load_canon(path: str | None = None) -> Canon:
    """The live catalog; ``path`` reads a specific file instead (tests/tools)."""
    if path:
        return _parse_canon(json.loads(Path(path).read_text(encoding="utf-8")))
    return _CANON.get()


load_canon.cache_clear = _CANON.reset  # type: ignore[attr-defined]


# --- step 1: visits ----------------------------------------------------------


def visit_date(
    *,
    page_from: Optional[str],
    page_to: Optional[str],
    doc_from: Optional[str],
    doc_to: Optional[str],
    default_date: str,
) -> tuple[str, str, str]:
    """(date_from, date_to, reason) for grouping.

    The visit a page belongs to is its document-level DOS, else its own
    page-level DOS. The DOS default is the absence of a date. ``reason`` is
    ``default_date`` or ``no_date`` when there is no usable date, else empty.
    """
    for d_from, d_to in ((doc_from, doc_to), (page_from, page_to)):
        d_from = str(d_from or "").strip()
        if d_from and d_from != default_date:
            return d_from, str(d_to or "").strip() or d_from, ""
    if default_date and default_date in {str(doc_from or ""), str(page_from or "")}:
        return "", "", "default_date"
    return "", "", "no_date"


def group_visits(pages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Runs of consecutive pages sharing a date. A dateless page is alone."""
    visits: list[list[dict[str, Any]]] = []
    previous: Optional[str] = None
    for page in pages:
        d_from = (page.get("dos_from") or "").strip()
        key = f"{d_from}|{(page.get('dos_to') or '').strip() or d_from}" if d_from else None
        if key is not None and key == previous and visits:
            visits[-1].append(page)
        else:
            visits.append([page])
        previous = key
    return visits


# --- step 2: findings --------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    setting: str
    tier: int
    source: str
    page_id: Any
    offset: int = 0


@dataclass
class Evidence:
    findings: list[Finding] = field(default_factory=list)  # after negatives
    cancelled: list[Finding] = field(default_factory=list)
    negatives_fired: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)


def gather(pages: list[dict[str, Any]], canon: Canon) -> Evidence:
    """Every finding once, in page order, with negatives applied."""
    seen: dict[tuple[str, int, str], Finding] = {}
    context: dict[str, None] = {}
    fired: dict[str, Negative] = {}

    for page in pages:
        pt_id = page.get("page_type_id") or ""
        if pt_id and pt_id in canon.tier1:
            source = page.get("page_type_name") or pt_id
            seen.setdefault(
                (canon.tier1[pt_id], 1, source),
                Finding(canon.tier1[pt_id], 1, source, page.get("page_id")),
            )
        hay = normalize_text(str(page.get("text") or ""))
        if not hay:
            continue
        for phrase in canon.phrases:
            if phrase.text not in hay:
                continue
            found = phrase.pattern.search(hay)
            if not found:
                continue
            if phrase.tier == 0:
                context.setdefault(phrase.text)
                continue
            seen.setdefault(
                (phrase.setting, phrase.tier, phrase.text),
                Finding(phrase.setting, phrase.tier, phrase.text, page.get("page_id"), found.start()),
            )
        for neg in canon.negatives:
            if neg.text in hay and neg.pattern.search(hay):
                fired.setdefault(neg.text, neg)

    evidence = Evidence(negatives_fired=list(fired), context=list(context))
    for finding in seen.values():
        cancelled = finding.tier > 1 and any(
            finding.source in neg.cancels or finding.setting == neg.cancels_setting
            for neg in fired.values()
        )
        (evidence.cancelled if cancelled else evidence.findings).append(finding)
    return evidence


# --- step 3: decide ----------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    setting: str  # empty when unresolved
    decided_by: str  # "tier1" | "tier2" | "unresolved"
    confidence: float
    conflict: bool
    matched_keyword: str
    reason: str = ""
    contenders: tuple[str, ...] = ()


def decide(evidence: Evidence, canon: Canon) -> Decision:
    for tier in (1, 2):
        deciding = [f for f in evidence.findings if f.tier == tier]
        if not deciding:
            continue
        settings = list(dict.fromkeys(f.setting for f in deciding))
        conflict = len(settings) > 1
        if conflict:
            hints = {
                s: sum(1 for f in evidence.findings if f.tier == 3 and f.setting == s)
                for s in settings
            }
            winner = min(settings, key=lambda s: (-hints[s], canon.priority.get(s, 999)))
        else:
            winner = settings[0]
        source = next(f.source for f in deciding if f.setting == winner)
        bucket = f"tier{tier}_conflict" if conflict else f"tier{tier}"
        return Decision(
            setting=winner,
            decided_by=f"tier{tier}",
            confidence=canon.confidence[bucket],
            conflict=conflict,
            matched_keyword=source,
            contenders=tuple(settings) if conflict else (),
        )
    return Decision("", "unresolved", 0.0, False, "", reason="no_setting_evidence")


# --- step 4: classify and stamp ---------------------------------------------


def classify_pages(
    pages: Iterable[dict[str, Any]],
    *,
    entries: Canon | None = None,
    visit_log: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """One row per page, every page of a visit carrying the visit's answer.

    Each input dict needs page_id, page_name, page_number, text, dos_from,
    dos_to, and may carry reason (``no_date`` / ``default_date``),
    page_type_id and page_type_name from the page type stage. Pages must be in chart order. ``visit_log``, when passed, receives
    one evidence record per visit.
    """
    canon = entries if entries is not None else load_canon()
    rows: list[dict[str, Any]] = []

    for visit in group_visits(list(pages)):
        first = visit[0]
        if not (first.get("dos_from") or "").strip():
            decision = Decision(
                "", "unresolved", 0.0, False, "", reason=first.get("reason") or "no_date"
            )
            evidence = Evidence()
        else:
            evidence = gather(visit, canon)
            decision = decide(evidence, canon)

        for page in visit:
            own = decide(gather([page], canon), canon).setting if decision.setting else ""
            rows.append(
                {
                    "page_id": page.get("page_id"),
                    "page_name": page.get("page_name"),
                    "page_number": page.get("page_number"),
                    "dos_from": page.get("dos_from") or "",
                    "dos_to": page.get("dos_to") or "",
                    "encounter_type": decision.setting,
                    "encounter_label": canon.labels.get(decision.setting, ""),
                    "confidence": decision.confidence if decision.setting else 0.0,
                    "matched_keyword": decision.matched_keyword,
                    "continue_applied": (
                        "y" if decision.setting and own != decision.setting else "n"
                    ),
                    "decided_by": decision.decided_by,
                    "reason": decision.reason,
                    "conflict": decision.conflict,
                }
            )

        if visit_log is not None:
            visit_log.append(
                {
                    "first_page": first.get("page_name"),
                    "pages": [p.get("page_name") for p in visit],
                    "dos_from": first.get("dos_from") or "",
                    "dos_to": first.get("dos_to") or "",
                    "encounter_type": decision.setting,
                    "decided_by": decision.decided_by,
                    "confidence": decision.confidence,
                    "conflict": decision.conflict,
                    "contenders": list(decision.contenders),
                    "reason": decision.reason,
                    "findings": [vars(f) for f in evidence.findings],
                    "cancelled": [vars(f) for f in evidence.cancelled],
                    "negatives_fired": evidence.negatives_fired,
                    "context": evidence.context,
                }
            )
    return rows
