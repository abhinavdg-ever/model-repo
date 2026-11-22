"""The page's classification answer and how later stages read it back.

``PageResult`` is what the page classifier (``leaf.predict``) returns for one page: L1 / L2 /
L3, specialty, codeability, who decided, the confidences and the classifier's working
(``leaf``). ``as_json`` is the whole answer, stored in ``page_classification.axes`` and
``staging/page_classification.json``; the column and the JSON keys keep their names so the
tables, CSVs and review-ui read it as before. ``stored`` / ``family_of`` / ``names_of`` /
``title_of`` / ``under_l1_of`` ... read a stored row (continuity, DOS, encounter type, Final).

``page_names`` turns it into ``page_type`` / ``page_subtype`` (L1 / Others, L1 / L2, or
"L1 (L2)" / L3).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from modules.page_classify import taxonomy as tax

NEEDS_REVIEW = "needs_review"
# The L2 of a page whose L1 has no L2 for it (never the L1 repeated), and the L1 of a page
# nothing names.
OTHERS = "Others"
# Who decided a level (stored values, unchanged): the model, the keyword canon
# (page_keywords.json), a rule, or nobody (others).
MODEL, CANON, RULE, BY_OTHERS = "model", "canon", "rule", "others"




@dataclass(frozen=True)
class PageResult:
    """The decided values (None when nothing cleared its gate), who decided
    each level, and the model's raw answer."""

    route: Optional[str] = None  # kept for the stored shape; the leaf classifier leaves it None
    l1: Optional[str] = None
    l1_confidence: Optional[float] = None
    l2: Optional[str] = None
    l2_confidence: Optional[float] = None
    l3: Optional[str] = None
    l3_confidence: Optional[float] = None
    # The whole path's (page_type_confidence): what page_classification.confidence stores.
    page_type_confidence: Optional[float] = None
    specialty: Optional[str] = None
    specialty_confidence: Optional[float] = None
    codeability: str = NEEDS_REVIEW
    codeability_confidence: Optional[float] = None
    # Who decided L1 / L2 / L3 / specialty / codeability: model, canon, rule, others or -.
    sources: tuple[str, ...] = ("-", "-", "-", "-", "-")
    # Retired 2026-11-22 with the page type canon: always None (the key stays in the JSON
    # and the CSV column stays, empty).
    canon_page_type: Optional[str] = None
    # Its codeability rule: page_type_tag, or context / source (needs_review
    # until the document — imaging_final — or the ingest source decides).
    codeability_rule: Optional[str] = None
    # The L1 whose page title (a Deciding term of page_keywords.json) heads the page, if any.
    title_l1: Optional[str] = None
    # Rules that re-placed the page, if any (the family priority rules, step 2).
    rules: tuple[str, ...] = ()
    # The model's best L1 > L2 > L3 paths (step 1); [] without a model.
    model_paths: tuple[dict[str, Any], ...] = field(default=(), compare=False)
    # The leaf decision's best whole paths (model and keyword canon together), best first: every
    # candidate the page could be, kept for the Final step.
    paths: tuple[dict[str, Any], ...] = field(default=(), compare=False)
    # Under every other L1 that has L2s: the L2 (and L3) the page would take
    # there -- {L1: {l2, l2_confidence, l2_source, l3, l3_confidence}}. The Final
    # step reads it when the page's document has another L1.
    under_l1: dict[str, Any] = field(default_factory=dict, compare=False)
    prediction: dict[str, Any] = field(default_factory=dict, compare=False)
    # The page-leaf decision (modules/page_classify/leaf.py): both top-5 lists, the
    # keyword gates and hits, the points and why the leaf won.
    leaf: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def decided_by(self) -> str:
        """model | canon | rule | others: who decided the L1."""
        return self.sources[0] if self.sources[0] != "-" else BY_OTHERS

    def as_json(self) -> dict[str, Any]:
        """The whole answer, for ``page_classification.axes``."""
        return {
            "route": self.route,
            "decided_by": self.decided_by,
            "l1": self.l1, "l1_confidence": self.l1_confidence,
            "l2": self.l2, "l2_confidence": self.l2_confidence,
            "l3": self.l3, "l3_confidence": self.l3_confidence,
            "page_type_confidence": self.page_type_confidence,
            "specialty": self.specialty, "specialty_confidence": self.specialty_confidence,
            "codeability": self.codeability, "codeability_confidence": self.codeability_confidence,
            "sources": "/".join(self.sources),
            "canon_page_type": self.canon_page_type,
            "codeability_rule": self.codeability_rule,
            "title_l1": self.title_l1,
            "taxonomy_version": tax.version(),
            "rules": list(self.rules) or None,
            "model_paths": list(self.model_paths) or None,
            "paths": list(self.paths) or None,
            "under_l1": self.under_l1 or None,
            "prediction": self.prediction or None,
            "leaf": self.leaf or None,
        }

    def csv_columns(self) -> dict[str, Any]:
        """The codeable CSV's axes columns (the CSV header is a frozen contract:
        the newer keys stay in the JSON only)."""
        flat = self.as_json()
        for key in ("prediction", "under_l1", "model_paths", "paths", "decided_by", "taxonomy_version", "rules",
                    "page_type_confidence", "leaf"):
            flat.pop(key)
        return {
            ("axes_" + key if not key.startswith("specialty") else key): value
            for key, value in flat.items()
        }


def page_names(result: Optional[PageResult]) -> tuple[Optional[str], Optional[str]]:
    """``page_type`` / ``page_subtype`` from the decided levels: L1 / Others with
    L1 only (an L1 is never its own sub-type), L1 / L2 with two levels,
    "L1 (L2)" / L3 with three; (None, None) when no L1 was decided."""
    if result is None or not result.l1:
        return None, None
    if result.l2 and result.l3:
        return f"{result.l1} ({result.l2})", result.l3
    if result.l2:
        return result.l1, result.l2
    return result.l1, OTHERS


# --- Reading a stored row (downstream stages) -------------------------------------------


def stored(kind: dict[str, Any]) -> dict[str, Any]:
    """The ``axes`` JSON of a page_classification row (a dict, or the JSON
    text a driver may hand back); {} when the row has none."""
    value = (kind or {}).get("axes")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def decision_confidence(kind: dict[str, Any]) -> Optional[float]:
    """The page type's weight for later decisions (continuity's page-type change, the Final
    vote): ``axes.decision_confidence``, the pre-2026-11-14 confidence (keyword lift and the
    page's doubts), so the new stored confidence (CONFIDENCE_LOGIC.md section 12) changes no
    decision; rows written before it: their ``confidence``. None without either."""
    value = stored(kind).get("decision_confidence")
    if value is None:
        value = (kind or {}).get("confidence")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def names_of(kind: dict[str, Any]) -> tuple[str, ...]:
    """Every name of a classified page, most specific first: L3, L2, L1 (and the canon page
    type of rows written before 2026-11-22). Lists downstream (encounter tier 1, default-DOS types,
    inside-note rules) match against these. A row without axes gives its
    page_subtype and page_type."""
    a = stored(kind)
    found = [a.get("canon_page_type"), a.get("l3"), a.get("l2"), a.get("l1")] if a else \
        [(kind or {}).get("page_subtype"), (kind or {}).get("page_type")]
    out: list[str] = []
    for name in found:
        if name and name not in out:
            out.append(str(name))
    return tuple(out)


def family_of(kind: dict[str, Any]) -> str:
    """The page's L1 (its family; documents agree on it), else its page type."""
    return str(stored(kind).get("l1") or (kind or {}).get("page_type") or "")


def names() -> frozenset[str]:
    """Every name a page can carry: the taxonomy's L1 / L2 / L3."""
    return tax.names()


def l2_names(l1: Optional[str]) -> frozenset[str]:
    """Every L2 the taxonomy places under ``l1`` (none for an L1 without L2s)."""
    return tax.l2_names(l1)


def under_l1_of(kind: dict[str, Any]) -> dict[str, Any]:
    """The L2 / L3 the page would take under each other L1 (``AxesResult.under_l1``)."""
    value = stored(kind).get("under_l1")
    return value if isinstance(value, dict) else {}


def l2_of(kind: dict[str, Any]) -> Optional[str]:
    """The page's own L2, if its axes decided one."""
    return stored(kind).get("l2") or None


def title_of(kind: dict[str, Any]) -> Optional[str]:
    """The L1 whose page title heads the page, if any."""
    return stored(kind).get("title_l1") or None


def decided_by_of(kind: dict[str, Any]) -> Optional[str]:
    """model | canon | rule | others: who decided the page's L1."""
    return stored(kind).get("decided_by") or None


def subtype_confidence(l2: Optional[str], l2_conf: Optional[float], l3: Optional[str],
                       l3_conf: Optional[float]) -> Optional[float]:
    """The subtype's own confidence (the deepest named level, conditional on its parent);
    None when the page has no named subtype."""
    named = [float(c) for name, c in ((l2, l2_conf), (l3, l3_conf)) if name and name != OTHERS and c is not None]
    return round(named[-1], 4) if named else None
