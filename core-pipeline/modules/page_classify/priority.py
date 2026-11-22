"""The family priority order (Brief 4 section 3, 2026-11-22, user): which family a page may be,
checked after the keyword gates and before the rank points (``leaf.decide``). Settings live in
``page_keywords.json`` -> ``priority``, not here.

0. **Progress Note model guard.** When the page-leaf model's Progress Note probability (the sum
   over its Progress Note leaves) is at least ``min_model_prob`` (0.50), only Progress Note
   leaves can win: another family's titles and words (an embedded lab table, a medication
   list) cannot move the page. It yields only to rules 1-3 firing on a **title** in the title
   zone (not on headers alone).
1. ``discharge_summary`` / 2. ``operative_note`` / 3. ``procedure_note``: a title in the title
   zone, or at least ``min_headers`` of the rule's headers (procedure: and fewer than
   ``without_groups`` note-section groups).
4. ``progress_note_headers``: a visit header, one strong header, or headers from
   ``min_groups`` note groups -- never on a letter (opens "Dear", closes "Sincerely").

The first rule that fires restricts the winner to its family, unless the model is more than
``model_sure_other_l1`` (0.90) sure of a family the rule ``yields_to``.

A **header** is a line that starts with the phrase followed by ':' or nothing else, of at most
``max_header_words`` words (``arbitration.heading_regex``, the one heading matcher), or a heading
the OCR detected. A **title** counts only in the title zone (``leaf_canon.title_zone``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Optional

from modules.page_classify import leaf_canon
from modules.page_classify.arbitration import heading_regex

SEP = " > "
PROGRESS_NOTE = "Progress Note"
GUARD = "progress_note_model_guard"


@dataclass(frozen=True)
class Fired:
    """What the priority step decided: the family the winner must come from (None: no
    restriction), the rule, why, and the rules that yielded to the model."""

    family: Optional[str] = None
    rule: Optional[str] = None
    why: dict[str, Any] = field(default_factory=dict)
    yielded: tuple[str, ...] = ()

    def record(self) -> Optional[dict[str, Any]]:
        if not self.rule and not self.yielded:
            return None
        return {"rule": self.rule, "family": self.family, **self.why,
                **({"yielded": list(self.yielded)} if self.yielded else {})}


def settings() -> dict[str, Any]:
    return leaf_canon.canon().priority or {}


@lru_cache(maxsize=256)
def _rx(phrases: tuple[str, ...]) -> Optional[re.Pattern[str]]:
    return heading_regex(phrases) if phrases else None


def _norm(text: str) -> str:
    return leaf_canon._norm(text)


def short_lines(text: str, max_words: int) -> str:
    """The page's lines of at most ``max_words`` words (headers are short)."""
    return "\n".join(line for line in (text or "").splitlines() if 0 < len(line.split()) <= max_words)


def headers_found(phrases: Iterable[str], text: str, headings: Iterable[str], max_words: int) -> list[str]:
    """The ``phrases`` the page heads: as a header line, or as an OCR-detected heading."""
    lines = short_lines(text, max_words)
    detected = {_norm(h) for h in headings if _norm(h)}
    out = []
    for phrase in phrases:
        rx = _rx((phrase,))
        if rx is not None and (rx.search(lines) or _norm(phrase) in detected):
            out.append(phrase)
    return out


def titles_found(titles: Iterable[str], text: str, headings: Iterable[str], max_words: int) -> list[str]:
    """The ``titles`` heading the page: a line of the title zone (or a detected heading) that
    starts with the title, at most ``max_words`` words longer than it."""
    zone = leaf_canon.title_zone(text, headings).split("\n")
    out = []
    for title in titles:
        t = _norm(title)
        n = len(t.split())
        if t and any(line == t or (line.startswith(t + " ") and len(line.split()) <= n + max_words) for line in zone):
            out.append(title)
    return out


def note_groups(text: str, headings: Iterable[str], cfg: dict[str, Any]) -> dict[str, list[str]]:
    """The note-section groups (subjective / objective / assessment / plan) the page heads."""
    max_words = int(settings().get("max_header_words", 6))
    out = {}
    for group, phrases in (cfg.get("groups") or {}).items():
        found = headers_found(phrases, text, headings, max_words)
        if found:
            out[group] = found
    return out


def is_letter(text: str, not_when: dict[str, Any]) -> bool:
    """A letter: a line opening with ``letter_open`` (Dear) near the top and a line opening with
    ``letter_close`` (Sincerely, Regards) anywhere (rule 4's ``not_when``)."""
    lines = [_norm(line) for line in (text or "").splitlines() if _norm(line)]
    opens = [_norm(w) for w in not_when.get("letter_open") or ()]
    closes = [_norm(w) for w in not_when.get("letter_close") or ()]
    return bool(opens and closes) and any(line.split()[0] in opens for line in lines[:15]) \
        and any(line.split()[0] in closes for line in lines)


def rule_fires(rule: dict[str, Any], text: str, headings: tuple[str, ...]) -> Optional[dict[str, Any]]:
    """Why ``rule`` fires on the page ({"title": ...} / {"headers": [...]} ...), else None."""
    max_words = int(settings().get("max_header_words", 6))
    if rule.get("name") == "progress_note_headers":
        if is_letter(text, rule.get("not_when") or {}):
            return None
        zone = leaf_canon.title_zone(text, headings)
        for pattern in rule.get("visit_header_patterns") or ():
            if re.search(pattern, zone, re.I):
                return {"visit_header": True}
        strong = headers_found(rule.get("strong_headers") or (), text, headings, max_words)
        if strong:
            return {"strong_header": strong[0]}
        groups = note_groups(text, headings, rule)
        need = set(rule.get("groups_need_one_of") or ())  # Exam + Impression alone is a report
        if len(groups) >= int(rule.get("min_groups", 2)) and (not need or need & set(groups)):
            return {"note_groups": sorted(groups)}
        return None
    titles = titles_found(rule.get("titles") or (), text, headings, max_words)
    if titles:
        return {"title": titles[0]}
    headers = headers_found(rule.get("headers") or (), text, headings, max_words)
    if len(headers) < int(rule.get("min_headers", 2)):
        return None
    if rule.get("without_groups") is not None:
        pn = next((r for r in settings().get("rules") or () if r.get("name") == "progress_note_headers"), {})
        if len(note_groups(text, headings, pn)) >= int(rule["without_groups"]):
            return None
    return {"headers": headers}


def family_probability(probs: dict[str, float], family: str) -> float:
    """The model's probability for a family: the sum over its classes."""
    return round(sum(p for path, p in probs.items() if path == family or path.startswith(family + SEP)), 4)


def decide(text: str, headings: tuple[str, ...], probs: dict[str, float],
           top_model: Optional[tuple[str, float]], sure_other: float) -> Fired:
    """The priority step for one page. ``probs``: the model's probability per class ({} without a
    model); ``top_model``: its best (path, probability)."""
    cfg = settings()
    rules = list(cfg.get("rules") or ())
    guard = cfg.get(GUARD) or {}
    yielded: list[str] = []

    # 0. The Progress Note model guard, yielding only to a title rule among rules 1-3.
    p_note = family_probability(probs, PROGRESS_NOTE) if probs else 0.0
    if probs and guard and p_note >= float(guard.get("min_model_prob", 0.5)):
        for rule in rules:
            if rule.get("family") == PROGRESS_NOTE:
                break
            why = rule_fires(rule, text, headings)
            if why and "title" in why:
                return Fired(rule["family"], rule["name"], {**why, "over_guard": p_note})
        return Fired(PROGRESS_NOTE, GUARD, {"model_prob": p_note})

    # 1-4 in order: the first that fires, unless the model is sure of a family it yields to.
    for rule in rules:
        why = rule_fires(rule, text, headings)
        if not why:
            continue
        if top_model and top_model[1] > sure_other and top_model[0].split(SEP)[0] in (rule.get("yields_to") or ()):
            yielded.append(f"{rule['name']} yielded to model {top_model[1]:.2f} {top_model[0].split(SEP)[0]}")
            continue
        return Fired(rule["family"], rule["name"], why, tuple(yielded))
    return Fired(yielded=tuple(yielded))
