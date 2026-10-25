"""Encounter setting per visit: Outpatient (F2F), Outpatient (Tele), Inpatient, Home.

Evidence is ranked, not added up. The stage finds the most authoritative
evidence a visit has and decides from that alone, so twenty passing mentions of
"follow up" never outweigh one discharge summary.

  1. Group pages into visits. When the continuity stage has run, a visit is
     one of its documents (``document`` on each page): the pages a document
     spans share one answer. Otherwise a visit is a run of consecutive pages
     sharing a usable date. A visit with no usable date on any page is left
     unresolved; the DOS default is not a date.
  2. Gather findings, each recorded once however often its words appear:
       tier 1  a matched page type that exists in one setting only
       tier 2  text that names the setting ("telehealth", "hospital course")
       tier 3  hints that also occur in other settings ("follow up")
     Negatives remove the tier 2/3 findings they name. Context phrases are
     logged and never score.
  3. Every page votes (2026-10-23): Not Applicable when its family is not
     one encounter type applies to (a lab report); else its own highest
     non-empty tier of tiers 1–2 (two settings there: more tier 3 hints, then
     priority); else its family's default (Progress Note -> Outpatient F2F,
     Discharge Summary -> Inpatient). The encounter's first page counts twice;
     Not Applicable wins only when every vote is Not Applicable; else the
     weighted majority of the settings wins (a tie: the first page's vote), confidence =
     its share of the weight. No vote at all: the canon default.
  4. Stamp every page of the visit with the answer.

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
    # When nothing is found: this setting at this (low) confidence; "" leaves it unresolved.
    default_setting: str = ""
    default_confidence: float = 0.0
    # Encounter type is valid only for these page families (L1); others are Not Applicable.
    applies_to: frozenset[str] = frozenset()
    not_applicable: str = "not_applicable"
    not_applicable_confidence: float = 1.0
    # The vote (2026-10-23): a page with no setting evidence votes its family's default
    # (Progress Note -> outpatient_f2f, Discharge Summary -> inpatient); the encounter's
    # first page counts first_page_weight times.
    family_defaults: dict[str, str] = field(default_factory=dict)
    first_page_weight: float = 2.0
    # The encounter model's vote (2026-10-25, model_vote): its class -> setting, and the
    # confidence each setting needs. A page with tier 1 / tier 2 evidence never asks it.
    model_settings: dict[str, str] = field(default_factory=dict)
    model_min: dict[str, float] = field(default_factory=dict)
    # Confirm words per setting (2026-10-25, user): with one on the page the model's bar
    # drops to model_confirm_min; they never block. setting -> (word, pattern) pairs.
    model_confirm: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = field(default_factory=dict)
    # With a confirming keyword, the model need only agree at this confidence.
    model_confirm_min: dict[str, float] = field(default_factory=dict)
    # A model answer at this confidence for one of these settings beats a tier 2 F2F phrase.
    model_override_min: float = 1.01
    model_override_settings: frozenset[str] = frozenset()


class CanonError(ValueError):
    """The canon breaks a loader rule; the message names every offence."""


def _page_type_ids() -> set[str]:
    """Every name a page can carry: the page taxonomy's L1 / L2 / L3
    (page_taxonomy.json) and the canon page types (page_canon.json)."""
    from stages.lib.page_classify import axes

    return set(axes.names())


def _validate(raw: dict[str, Any], page_type_ids: Optional[set[str]]) -> None:
    settings = set(raw.get("settings") or {})
    errors: list[str] = []

    for pt_id, setting in (raw.get("tier1_page_types") or {}).items():
        if setting not in settings:
            errors.append(f"tier1_page_types.{pt_id}: unknown setting {setting!r}")
        if page_type_ids is not None and pt_id not in page_type_ids:
            errors.append(f"tier1_page_types.{pt_id}: not an L1 / L2 / L3 of page_taxonomy.json or a canon page type in page_canon.json")

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

    for family, setting in ((raw.get("vote") or {}).get("family_defaults") or {}).items():
        if setting not in settings:
            errors.append(f"vote.family_defaults.{family}: unknown setting {setting!r}")

    model_vote = raw.get("model_vote") or {}
    for cls, setting in (model_vote.get("settings") or {}).items():
        if setting not in settings:
            errors.append(f"model_vote.settings.{cls}: unknown setting {setting!r}")
    for setting in model_vote.get("min_confidence") or {}:
        if setting not in settings:
            errors.append(f"model_vote.min_confidence.{setting}: unknown setting")
    for setting in model_vote.get("confirm") or {}:
        if setting not in settings:
            errors.append(f"model_vote.confirm.{setting}: unknown setting")
    for setting in (model_vote.get("override_f2f_phrase") or {}).get("settings") or ():
        if setting not in settings:
            errors.append(f"model_vote.override_f2f_phrase.settings: unknown setting {setting!r}")

    default = (raw.get("default") or {}).get("setting")
    if default and default not in settings:
        errors.append(f"default.setting: unknown setting {default!r}")

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
    na = (raw.get("applies_to") or {}).get("not_applicable") or {}
    na_setting = str(na.get("setting") or "not_applicable")
    return Canon(
        labels={
            **{k: str(v["label"]) for k, v in settings.items()},
            na_setting: str(na.get("label") or "Not Applicable"),
        },
        priority={k: int(v["priority"]) for k, v in settings.items()},
        confidence={k: float(v) for k, v in raw["confidence"].items()},
        tier1=dict(raw["tier1_page_types"]),
        phrases=tuple(phrases),
        negatives=negatives,
        default_setting=str((raw.get("default") or {}).get("setting") or ""),
        default_confidence=float((raw.get("default") or {}).get("confidence") or 0.0),
        applies_to=frozenset(
            str(f).strip().casefold() for f in (raw.get("applies_to") or {}).get("families") or []
        ),
        not_applicable=na_setting,
        not_applicable_confidence=float(na.get("confidence", 1.0)),
        family_defaults={
            str(f).strip().casefold(): str(v)
            for f, v in ((raw.get("vote") or {}).get("family_defaults") or {}).items()
        },
        first_page_weight=float((raw.get("vote") or {}).get("first_page_weight", 2.0)),
        model_settings=dict((raw.get("model_vote") or {}).get("settings") or {}),
        model_min={k: float(v) for k, v in ((raw.get("model_vote") or {}).get("min_confidence") or {}).items()},
        model_confirm_min={k: float(v) for k, v in ((raw.get("model_vote") or {}).get("confirm_min") or {}).items()},
        model_override_min=float(((raw.get("model_vote") or {}).get("override_f2f_phrase") or {}).get("min_confidence", 1.01)),
        model_override_settings=frozenset(((raw.get("model_vote") or {}).get("override_f2f_phrase") or {}).get("settings") or ()),
        model_confirm={
            setting: tuple((normalize_text(w), _phrase_re(normalize_text(w))) for w in words)
            for setting, words in ((raw.get("model_vote") or {}).get("confirm") or {}).items()
        },
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
    """Continuity documents when the pages carry one; else runs of
    consecutive pages sharing a date (a dateless page alone)."""
    if any(page.get("document") is not None for page in pages):
        return _document_visits(pages)
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


def _document_visits(pages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Consecutive pages of one continuity document; a page in none is alone."""
    visits: list[list[dict[str, Any]]] = []
    previous: Any = None
    for page in pages:
        document = page.get("document")
        if document is not None and document == previous and visits:
            visits[-1].append(page)
        else:
            visits.append([page])
        previous = document
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
    decided_by: str  # "tier1" | "tier2" | "default" | "not_applicable" | "unresolved"
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


def encounter_family(visit: list[dict[str, Any]]) -> str:
    """The encounter's page type: its first page's L1 family (the document's Final
    page type), else the first page that has one."""
    for page in visit:
        family = str(page.get("page_family") or "").strip()
        if family and family.casefold() != "unknown":
            return family
    return ""


@dataclass
class Vote:
    """One page's say in its encounter's setting."""

    setting: str  # "" = abstains (no evidence and no family default)
    weight: float
    decided_by: str  # tier1 | tier2 | model | family_default | not_applicable | none
    matched_keyword: str
    evidence: Evidence


F2F = "outpatient_f2f"


def page_vote(page: dict[str, Any], canon: Canon, *, weight: float = 1.0) -> Vote:
    """The page's own setting: Not Applicable for a family encounter type does not apply
    to (a lab report), else its strongest evidence (tier 1, then tier 2), else the
    encounter model's answer (``page["encounter_model"]``: class, confidence) when the
    canon's ``model_vote`` takes: a setting's probability at ``min_confidence``, or at
    ``confirm_min`` with one of its confirm words on the page (the keyword lowers the bar,
    never blocks), else its family's default
    (Progress Note -> Outpatient F2F, Discharge Summary -> Inpatient). A tier 2 F2F phrase
    gives way to the model at ``override_f2f_phrase`` confidence (0.95) for Evisit, Home
    Visit or Inpatient."""
    family = str(page.get("page_family") or "").strip()
    if canon.applies_to and family and family.casefold() != "unknown" and family.casefold() not in canon.applies_to:
        return Vote(canon.not_applicable, weight, "not_applicable", family, Evidence())
    evidence = gather([page], canon)
    own = decide(evidence, canon)
    model = page.get("encounter_model")
    if own.setting == F2F and own.decided_by == "tier2" and model:
        # A model at very high confidence beats a weak F2F phrase ("office visit" on a
        # house-call or telehealth template; 2026-10-25, user).
        setting = canon.model_settings.get(model[0], "")
        if setting in canon.model_override_settings and model[1] >= canon.model_override_min:
            return Vote(setting, weight, "model", f"model {model[0]} {model[1]:.2f} over phrase {own.matched_keyword!r}",
                        evidence)
    if own.setting:
        return Vote(own.setting, weight, own.decided_by, own.matched_keyword, evidence)
    if model:
        name, confidence = model[0], model[1]
        probs = model[2] if len(model) > 2 else {name: confidence}
        hay = normalize_text(str(page.get("text") or ""))
        # Each setting the model may vote: its probability (top answer or not) at
        # min_confidence, or at confirm_min with one of its confirm words on the page --
        # the keyword lowers the bar and never blocks (2026-10-25, user). The most
        # probable qualifying setting votes.
        for cls in sorted(canon.model_settings, key=lambda c: -float(probs.get(c, 0.0))):
            setting, p = canon.model_settings[cls], float(probs.get(cls, 0.0))
            if p >= canon.model_min.get(setting, 1.01):
                return Vote(setting, weight, "model", f"model: {cls} {p:.2f}", evidence)
            word = next((w for w, pattern in canon.model_confirm.get(setting, ())
                         if w in hay and pattern.search(hay)), None)
            if word and p >= canon.model_confirm_min.get(setting, 1.01):
                return Vote(setting, weight, "model", f"keyword {word!r} + model: {cls} {p:.2f}", evidence)
    fallback = canon.family_defaults.get(family.casefold(), "")
    return Vote(fallback, weight, "family_default" if fallback else "none", "", evidence)


# A vote from the page's own evidence, which lets the first page decide its encounter alone.
EVIDENCE = ("tier1", "tier2", "model")


def first_page_decision(votes: list[Vote], canon: Canon) -> Optional[Decision]:
    """The encounter's first page decides when it has evidence of its own (a tier 1 page
    type, a tier 2 phrase, or the encounter model; 2026-10-25, user). None when it does
    not (Not Applicable, its family's default, or nothing): then every page votes."""
    if not votes or votes[0].decided_by not in EVIDENCE or not votes[0].setting:
        return None
    first = votes[0]
    confidence = canon.confidence.get(first.decided_by, canon.confidence.get("tier2", 0.8))
    if first.decided_by == "model":
        confidence = canon.confidence.get("tier2", 0.8)
    others = {v.setting for v in votes[1:] if v.setting and v.setting != canon.not_applicable}
    return Decision(
        setting=first.setting,
        decided_by="first_page",
        confidence=confidence,
        conflict=bool(others - {first.setting}),
        matched_keyword=first.matched_keyword,
        reason=f"first page: {first.decided_by}",
        contenders=tuple(sorted(others | {first.setting})) if others - {first.setting} else (),
    )


def tally(votes: list[Vote], canon: Canon) -> Decision:
    """The weighted majority, the first page counting ``first_page_weight`` times. Only
    pages with evidence of their own (tier 1, tier 2, the model) vote when any has it;
    otherwise the family defaults vote (Progress Note -> F2F). Not Applicable wins only
    when every vote is Not Applicable; otherwise those votes are set
    aside. A tie goes to the first page's vote, then the setting priority. Confidence = the
    winner's share of the weight counted. No vote at all: the canon default (low confidence)."""
    if votes:
        votes[0].weight = canon.first_page_weight
    # Pages with evidence of their own vote; a family default (Progress Note -> F2F) is
    # only the fallback when no page has any (2026-10-25, user).
    counted = [v for v in votes if v.decided_by in EVIDENCE] or votes
    weights: dict[str, float] = {}
    for vote in counted:
        if vote.setting:
            weights[vote.setting] = weights.get(vote.setting, 0.0) + vote.weight
    # Not Applicable only when it is every vote (2026-10-23, user): one page voting a
    # setting decides among the settings; the lab pages' votes then do not count.
    if len(weights) > 1:
        weights.pop(canon.not_applicable, None)
    if not weights:
        return Decision("", "unresolved", 0.0, False, "", reason="no_setting_evidence")
    best = max(weights.values())
    tied = [setting for setting, weight in weights.items() if abs(weight - best) < 1e-9]
    opener = votes[0].setting
    winner = opener if opener in tied else min(tied, key=lambda s: canon.priority.get(s, 999))
    keyword = next((v.matched_keyword for v in votes if v.setting == winner and v.matched_keyword), "")
    return Decision(
        setting=winner,
        decided_by="vote",
        confidence=round(best / sum(weights.values()), 4),
        conflict=len(weights) > 1,
        matched_keyword=keyword,
        reason="; ".join(f"{s} {w:g}" for s, w in sorted(weights.items(), key=lambda kv: -kv[1])),
        contenders=tuple(weights) if len(weights) > 1 else (),
    )


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
        family = encounter_family(visit)
        votes = [page_vote(page, canon) for page in visit]
        decision = first_page_decision(votes, canon) or tally(votes, canon)
        if not decision.setting and canon.default_setting:
            # No page voted (no evidence, no family default): the default, low confidence.
            decision = Decision(
                canon.default_setting, "default", canon.default_confidence, False, "",
                reason=decision.reason,
            )
        evidence = Evidence()
        for vote in votes:
            evidence.findings.extend(vote.evidence.findings)
            evidence.cancelled.extend(vote.evidence.cancelled)
            evidence.negatives_fired.extend(n for n in vote.evidence.negatives_fired if n not in evidence.negatives_fired)
            evidence.context.extend(c for c in vote.evidence.context if c not in evidence.context)

        for page, vote in zip(visit, votes):
            own = vote.setting
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
                    "seq": len(visit_log) + 1,
                    "document": first.get("document"),
                    "page_family": family,
                    "first_page": first.get("page_name"),
                    "pages": [p.get("page_name") for p in visit],
                    "dos_from": first.get("dos_from") or "",
                    "dos_to": first.get("dos_to") or "",
                    "encounter_type": decision.setting,
                    "encounter_label": canon.labels.get(decision.setting, ""),
                    "matched_keyword": decision.matched_keyword,
                    "decided_by": decision.decided_by,
                    "confidence": decision.confidence,
                    "conflict": decision.conflict,
                    "contenders": list(decision.contenders),
                    # Each page's vote, in page order; the first page counts double.
                    "votes": [
                        {"page": p.get("page_name"), "setting": v.setting, "weight": v.weight, "by": v.decided_by,
                         "keyword": v.matched_keyword}
                        for p, v in zip(visit, votes)
                    ],
                    "reason": decision.reason,
                    "findings": [vars(f) for f in evidence.findings],
                    "cancelled": [vars(f) for f in evidence.cancelled],
                    "negatives_fired": evidence.negatives_fired,
                    "context": evidence.context,
                }
            )
    return rows
