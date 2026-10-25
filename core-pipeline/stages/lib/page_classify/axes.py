"""Page axes for a page — the **Extracted** page type: L1, L2, L3, specialty
and codeability, decided from the page alone (``page_arbitration.json`` →
``axes``), in the names of the page taxonomy (``keyword-canon/page_taxonomy.json``).

At every level -- L1; L2 under the L1; L3 under the L2 -- the page-axes model and the
keyword canon **vote** and the winner takes it (``keyword_vote``):

    vote(class) = (1 - k) * p_model(class) + k * keywords(class) / sum(keywords)

``keywords`` are the canon's terms on the page, each weighted per level by how much it
says about each class (rare, unique terms high; common ones low; learned from labelled
pages, ``keyword-canon/page_keyword_weights.json``). ``k`` is the keywords' share,
smaller when their evidence is thin. A winner below the level's minimum (0.2) is not
taken: L1 Others (``decided_by`` others), "L1 / Others", or L3 Others. Without a model
the keywords vote alone; with no keyword the model does. The section L1's L2 (Progress
Note) is the keywords' alone: the model never learned it.

``paths`` keeps the vote's best whole paths, so a Progress Note page that lists CT scans
keeps Progress Note > Lab and Imaging Results and Lab Report > Radiology Report > CT
Scan Report both, for the Final step. ``model_paths`` keeps the model's own.

Then ``page_rules`` -- rules that look at more than words: the lab guard, the
hospital-stay H&P / consult, the clinical summary title -- may re-place the page:
``sources`` says "rule" and ``rules`` names it. Specialty: the model's behind a strict
gate; else the specialty keywords (``specialty_keywords``); else the canon entry's; else
``specialty_fallback`` (General) -- never Unknown. Codeability here is the page's own,
from the canon entry behind the decided path; the client's is the Final type's
(``arbitration.decide_final``).

The axes are the page's classification: ``page_names`` turns them into
``page_type`` / ``page_subtype`` (L1 / Others, L1 / L2, or "L1 (L2)" / L3), and
``as_json`` is the whole answer for ``page_classification.axes`` (and
``staging/page_classification.json``).

The model is the folder ``train_axes.py`` writes (``encoder/``, ``heads.pt``,
``label_space.json``), in ``PAGE_AXES_MODEL_DIR`` (default
``models/page-family/page-axes/``).

* No folder, or no transformers / torch: the canon decides every level and
  ``route`` is None — a degraded run visible in the data.
* A folder trained on another taxonomy version (``label_space.json``
  ``taxonomy_version``) is refused at load, naming both; the canon decides.

``gate`` needs no torch: the model's raw answer (or None) and the page text in,
the decided values out.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Optional

from stages.lib import confidence as conf
from stages.lib.canon_store import CANON_DIR, CanonFile
from stages.lib.page_classify import keyword_vote, keywords, page_rules, specialty_keywords
from stages.lib.page_classify import taxonomy as tax

logger = logging.getLogger(__name__)

MODEL_FILES = ("label_space.json", "heads.pt", "encoder/config.json")
NEEDS_REVIEW = "needs_review"
# The L2 of a page whose L1 has no L2 for it (never the L1 repeated), and the
# L1 of a page neither the model nor the canon is sure of.
OTHERS = "Others"
MODEL_PATHS = 5  # the model's best paths kept per page (``model_paths``)
# Who decided a level.
MODEL, CANON, RULE, BY_OTHERS = "model", "canon", "rule", "others"

_ARBITRATION: CanonFile[dict[str, Any]] = CanonFile(CANON_DIR / "page_arbitration.json")

_lock = threading.Lock()
_loaded: Optional[dict[str, Any]] = None
_failed: Optional[str] = None


class ModelTaxonomyError(RuntimeError):
    """The model folder was trained on another taxonomy version."""


@dataclass(frozen=True)
class _Canon:
    keywords: Any  # keywords._Canon
    entries: dict[str, dict[str, Any]]  # canon page type -> its entry


def _build_canon(data: dict[str, Any]) -> _Canon:
    return _Canon(keywords.build(data), {e["page_type"]: e for e in data["page_types"]})


_CANON: CanonFile[_Canon] = CanonFile(CANON_DIR / "page_canon.json", _build_canon)


@dataclass(frozen=True)
class AxesResult:
    """The decided values (None when nothing cleared its gate), who decided
    each level, and the model's raw answer."""

    route: Optional[str] = None  # model | canon | review; None without a model
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
    # The canon page type behind the decided path (codeability, specialty,
    # and the fine-grained name encounter type reads).
    canon_page_type: Optional[str] = None
    # Its codeability rule: page_type_tag, or context / source (needs_review
    # until the document — imaging_final — or the ingest source decides).
    codeability_rule: Optional[str] = None
    # The L1 whose canon title term heads the page, if any.
    title_l1: Optional[str] = None
    # The page_rules that re-placed the page, if any.
    rules: tuple[str, ...] = ()
    # The model's best L1 > L2 > L3 paths (step 1); [] without a model.
    model_paths: tuple[dict[str, Any], ...] = field(default=(), compare=False)
    # The vote's best whole paths (model and keyword canon together), best first: every
    # candidate the page could be, kept for the Final step.
    paths: tuple[dict[str, Any], ...] = field(default=(), compare=False)
    # Under every other L1 that has L2s: the L2 (and L3) the page would take
    # there -- {L1: {l2, l2_confidence, l2_source, l3, l3_confidence}}. The Final
    # step reads it when the page's document has another L1.
    under_l1: dict[str, Any] = field(default_factory=dict, compare=False)
    prediction: dict[str, Any] = field(default_factory=dict, compare=False)

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
        }

    def csv_columns(self) -> dict[str, Any]:
        """The codeable CSV's axes columns (the CSV header is a frozen contract:
        the newer keys stay in the JSON only)."""
        flat = self.as_json()
        for key in ("prediction", "under_l1", "model_paths", "paths", "decided_by", "taxonomy_version", "rules",
                    "page_type_confidence"):
            flat.pop(key)
        return {
            ("axes_" + key if not key.startswith("specialty") else key): value
            for key, value in flat.items()
        }


def page_names(result: Optional[AxesResult]) -> tuple[Optional[str], Optional[str]]:
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


def names_of(kind: dict[str, Any]) -> tuple[str, ...]:
    """Every name of a classified page, most specific first: its canon page
    type, L3, L2, L1. Lists downstream (encounter tier 1, default-DOS types,
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


def canon_page_types() -> frozenset[str]:
    """Every canon page type (``page_canon.json``)."""
    return frozenset(_CANON.get().entries)


def names() -> frozenset[str]:
    """Every name a page can carry: the taxonomy's L1 / L2 / L3 and the canon
    page types."""
    return tax.names() | canon_page_types()


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
    """The L1 whose canon title heads the page, if any."""
    return stored(kind).get("title_l1") or None


def decided_by_of(kind: dict[str, Any]) -> Optional[str]:
    """model | canon | rule | others: who decided the page's L1."""
    return stored(kind).get("decided_by") or None


def settings() -> dict[str, Any]:
    return dict(_ARBITRATION.get()["axes"])


def model_dir() -> Path:
    from config import PAGE_AXES_MODEL_DIR

    return Path(PAGE_AXES_MODEL_DIR)


# --- The gate ---------------------------------------------------------------------------


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 4)


def route(top: list[tuple[str, float]], other_l1: str, s: dict[str, Any]) -> str:
    """'model' (the model decides L1), 'canon' (the keyword canon should) or 'review'."""
    if not top:
        return "review"
    name, best = top[0]
    second = top[1][1] if len(top) > 1 else 0.0
    if name != other_l1 and best >= s["route_model"] and best - second >= s["route_margin"]:
        return "model"
    if name == other_l1 or best >= s["route_canon"]:
        return "canon"
    return "review"


def model_paths(raw: dict[str, Any], space: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """The model's best L1 > L2 > L3 paths, best score first, as ``decode`` ranks
    them (``paths``: the 5 best over every allowed path, at most 3 under one L1),
    with each level's confidence and the score p(L1)·p(L2|L1)·p(L3|L2) (a level the
    parent does not have counts 1). An answer without ``paths`` gets one path per
    L1 of its ``L1_top``, from ``by_L1``, scored the same way. The folded OTHER
    class reads Others (``folded``)."""
    other = space.get("other_L1", "OTHER")
    paths = raw.get("paths")
    if not paths:
        by_l1 = dict(raw.get("by_L1") or {})
        if raw.get("L1") and raw.get("L2"):
            by_l1.setdefault(raw["L1"], {k: raw.get(k) for k in ("L2", "L2_confidence", "L3", "L3_confidence")})
        paths = []
        for l1, p1 in (raw.get("L1_top") or [])[:3]:
            best = by_l1.get(l1) or {}
            score = float(p1) * float(best.get("L2_confidence") or 1.0) * float(best.get("L3_confidence") or 1.0)
            paths.append({"L1": l1, "L2": best.get("L2"), "L3": best.get("L3"), "L1_confidence": p1,
                          "L2_confidence": best.get("L2_confidence"), "L3_confidence": best.get("L3_confidence"),
                          "score": score})
        paths.sort(key=lambda x: -x["score"])
    out = []
    for rank, found in enumerate(paths[:MODEL_PATHS], start=1):
        l1 = OTHERS if found.get("L1") == other else found.get("L1")
        l2, l3 = found.get("L2") or None, found.get("L3") or None
        out.append({
            "rank": rank,
            "path": " > ".join(x for x in (l1, l2, l3) if x),
            "l1": l1, "l1_confidence": _round(found.get("L1_confidence")),
            "l2": l2, "l2_confidence": _round(found.get("L2_confidence")),
            "l3": l3, "l3_confidence": _round(found.get("L3_confidence")),
            "score": _round(found.get("score")),
            **({"folded": True} if found.get("L1") == other else {}),
        })
    return tuple(out)


def canon_match(
    text: str, headings: tuple[str, ...], level: str, l1: Optional[str] = None, l2: Optional[str] = None
) -> tuple[Optional[str], Optional[float], Optional[str]]:
    """The canon's best ``level`` value (L1, L2 or L3) among entries under
    ``l1`` / ``l2``: (value, keyword confidence, canon page type)."""
    from stages.lib.page_classify.arbitration import keyword_confidence

    canon = _CANON.get()
    names_ = frozenset(
        name for name, e in canon.entries.items()
        if e["path"].get(level)
        and (l1 is None or e["path"]["L1"] == l1)
        and (l2 is None or e["path"].get("L2") == l2)
    )
    if not names_:
        return None, None, None
    group_of = {name: canon.entries[name]["path"][level] for name in names_}
    found = keywords.classify(text, headings, canon=canon.keywords, subtypes=names_, group_of=group_of)
    if found is None:
        return None, None, None
    return found.page_type, keyword_confidence(found), found.page_subtype


def canon_entry(
    text: str, headings: tuple[str, ...], l1: str, l2: Optional[str] = None, l3: Optional[str] = None
) -> tuple[Optional[str], Optional[float]]:
    """The canon page type behind the decided path: the best keyword match
    among the entries under L1 [> L2 [> L3]], with its keyword confidence.
    With no keyword hit, the entry for exactly that path — the one named after
    its deepest level, else any when they all carry the same codeability tag —
    with no confidence. (None, None) when the canon has nothing there."""
    from stages.lib.page_classify.arbitration import keyword_confidence

    canon = _CANON.get()
    entries = canon.entries
    under = {
        name for name, e in entries.items()
        if e["path"]["L1"] == l1
        and (l2 is None or e["path"].get("L2") == l2)
        and (l3 is None or e["path"].get("L3") == l3)
    }
    if not under:
        return None, None
    found = keywords.classify(text, headings, canon=canon.keywords,
                              subtypes=frozenset(under), group_of={n: n for n in under})
    if found is not None:
        return found.page_subtype, keyword_confidence(found)
    exact = sorted(n for n in under if (entries[n]["path"].get("L2") or None) == l2
                   and (entries[n]["path"].get("L3") or None) == l3)
    deepest = l3 or l2 or l1
    if deepest in exact:
        return deepest, None
    tags = {(entries[n]["codeability"], entries[n]["codeability_rule"]) for n in exact}
    return (exact[0], None) if len(tags) == 1 else (None, None)


def title_l1(text: str, headings: tuple[str, ...]) -> Optional[str]:
    """The L1 whose canon title term heads the page, if any (continuity's
    "own title" evidence, and the Final step's embedded rule)."""
    canon = _CANON.get()
    group_of = {name: e["path"]["L1"] for name, e in canon.entries.items()}
    found = keywords.classify(text, headings, canon=canon.keywords, group_of=group_of)
    return found.page_type if found is not None and found.title_hit else None


def _model_specialty(raw: dict[str, Any], space: dict[str, Any], s: dict[str, Any]) -> tuple[Optional[str], Optional[float]]:
    top = list(raw.get("specialty_top") or [])
    if not top and raw.get("specialty"):
        top = [(raw["specialty"], raw.get("specialty_confidence") or 0.0)]
    if not top:
        return None, None
    name, best = top[0]
    second = top[1][1] if len(top) > 1 else 0.0
    trained = ((space.get("train_pages") or {}).get("specialty") or {}).get(name, 0)
    if (
        name in s["specialty_not_asserted"]
        or best < s["specialty_min"]
        or best - second < s["specialty_lead"]
        or trained < s["specialty_min_train_pages"]
    ):
        return None, None
    return name, best


def l3_names(l1: Optional[str], l2: Optional[str]) -> tuple[str, ...]:
    """The named L3s under L1 > L2 (none for most L2s)."""
    if not (l1 and l2):
        return ()
    return tuple((tax.label_space().get("L3") or {}).get(f"{l1} > {l2}") or ())


def distributions(raw: dict[str, Any], space: dict[str, Any], s: dict[str, Any]) -> dict[str, Any]:
    """The model's answer per level for the vote: ``decode``'s ``distributions``, else
    rebuilt from ``L1_top`` / ``by_L1`` (an answer stored before 2026-10-25). The folded
    OTHER class, and the section L1's L2 (never trained: section tagging owned it), are
    left out."""
    other = space.get("other_L1", "OTHER")
    dist = raw.get("distributions")
    if not dist:
        by_l1 = {l1: {b["L2"]: b.get("L2_confidence") or 0.0} for l1, b in (raw.get("by_L1") or {}).items() if b.get("L2")}
        if raw.get("L1") and raw.get("L2"):
            by_l1[raw["L1"]] = {raw["L2"]: raw.get("L2_confidence") or 0.0}
        l3 = {f"{raw['L1']} > {raw['L2']}": {raw["L3"]: raw.get("L3_confidence") or 0.0}} if raw.get("L3") else {}
        l1s: dict[str, float] = {}
        for name, prob in raw.get("L1_top") or []:
            l1s.setdefault(name, float(prob))
        dist = {"L1": l1s, "L2": by_l1, "L3": l3}
    return {
        "L1": {c: p for c, p in (dist.get("L1") or {}).items() if c != other},
        "L2": {l1: v for l1, v in (dist.get("L2") or {}).items() if l1 != s["section_L1"]},
        "L3": dict(dist.get("L3") or {}),
    }


class _Vote:
    """The keyword canon and the model voting level by level on one page."""

    def __init__(self, raw: dict[str, Any], space: dict[str, Any], text: str, headings: tuple[str, ...],
                 s: dict[str, Any]) -> None:
        self.dist = distributions(raw, space, s) if raw else {"L1": {}, "L2": {}, "L3": {}}
        self.feats = keyword_vote.features(text, headings)
        self.vs = keyword_vote.settings()
        self.space = tax.label_space()

    def ranked(self, level: str, parent: Optional[str] = None) -> list[tuple[str, float, str]]:
        """(class, vote, who) best first at ``level`` under ``parent``."""
        if level == "L1":
            classes, model = self.space["L1"], self.dist["L1"]
        elif level == "L2":
            classes, model = self.space["L2"].get(parent or "") or (), self.dist["L2"].get(parent or "")
        else:
            classes, model = self.space["L3"].get(parent or "") or (), self.dist["L3"].get(parent or "")
        if not classes:
            return []
        model = {c: p for c, p in (model or {}).items() if c in classes} or None
        keyword = {c: w for c, w in keyword_vote.scores(self.feats, level, parent).items() if c in classes}
        return keyword_vote.vote(model, keyword, self.vs["share"][level], self.vs["full"][level], classes)

    def _sides(self, level: str, parent: Optional[str]) -> tuple[dict[str, float], dict[str, float]]:
        """The model's probabilities and the keyword scores at ``level`` over its classes."""
        classes = self.space["L1"] if level == "L1" else (self.space[level].get(parent or "") or ())
        model = self.dist[level] if level == "L1" else self.dist[level].get(parent or "")
        model = {c: p for c, p in (model or {}).items() if c in classes}
        keyword = {c: w for c, w in keyword_vote.scores(self.feats, level, parent).items() if c in classes and w > 0}
        return model, keyword

    def confidence(self, level: str, parent: Optional[str], cls: str, *, pinned: bool = False,
                   rule: bool = False) -> Optional[float]:
        """How sure the page is of ``cls`` at ``level`` (stages/lib/confidence.py): the
        model's probability for it, lifted when the keywords back it (a strong title pin
        or a page rule fully), times ``keywords_disagree`` when they point elsewhere (or
        the model's own favourite is another class behind a rule). Without a model: the
        keywords' share of the evidence times its strength (a pin or a rule: 1.0); the
        page type takes ``no_model`` once (page_type_confidence)."""
        model, keyword = self._sides(level, parent)
        total = sum(keyword.values())
        strength = min(1.0, total / max(1e-9, float(self.vs["full"][level])))
        top = max(keyword, key=keyword.get) if total else None
        reasons = []
        if not model:
            base = 1.0 if pinned or rule else (keyword.get(cls, 0.0) / total * strength if total else 0.0)
            return conf.score(base, module="page_type")
        base = model.get(cls, 0.0)
        if pinned or rule:
            base = conf.backed(base, 1.0)
            if rule and max(model, key=model.get) != cls:
                reasons.append("keywords_disagree")
        elif top == cls:
            base = conf.backed(base, keyword[cls] / total * strength)
        elif top is not None and strength >= 0.5:
            reasons.append("keywords_disagree")
        return conf.score(base, *reasons, module="page_type")

    def others_confidence(self, level: str, parent: Optional[str], raw: dict[str, Any], other: str) -> Optional[float]:
        """How sure the page is of Others at ``level`` (no class reached the minimum): the
        model's probability for its folded OTHER class at L1, else for none of the named
        classes (1 - the best), times ``fallback``. Without a model: 0.5 x ``fallback``."""
        model, _ = self._sides(level, parent)
        if level == "L1":
            folded = dict((str(n), float(p)) for n, p in raw.get("L1_top") or []).get(other)
            if folded is not None:
                return conf.score(folded, "fallback", module="page_type")
        if not model:
            return conf.score(0.5, "fallback", module="page_type")
        return conf.score(1.0 - max(model.values()), "fallback", module="page_type")

    def pick(self, level: str, parent: Optional[str] = None) -> tuple[Optional[str], Optional[float], str]:
        """The winner at ``level`` and its confidence: a class a strong canon term pins by
        title, else the vote's winner when it reaches the level's minimum."""
        classes = self.space["L1"] if level == "L1" else (self.space[level].get(parent or "") or ())
        pins = keyword_vote.pinned(self.feats, level, parent) & set(classes)
        if len(pins) == 1:
            cls = next(iter(pins))
            return cls, self.confidence(level, parent, cls, pinned=True), CANON
        ranked = self.ranked(level, parent)
        if ranked and ranked[0][1] >= self.vs["min"][level] and ranked[0][1] > 0:
            cls, _, who = ranked[0]
            return cls, self.confidence(level, parent, cls), who
        return None, None, "-"

    def paths(self, k: int = MODEL_PATHS, per_l1: int = 3) -> tuple[dict[str, Any], ...]:
        """The vote's best whole paths: vote(L1) x vote(L2 | L1) x vote(L3 | L2) over the
        three best L1s, their three best L2s and the best L3, at most ``per_l1`` under one
        L1. A Progress Note page listing CT scans keeps both Progress Note > Lab and
        Imaging Results and Lab Report > Radiology Report > CT Scan Report here."""
        found = []
        for l1, v1, _ in self.ranked("L1")[:3]:
            l2s = self.ranked("L2", l1)[:3] or [(None, 1.0, "-")]
            for l2, v2, _ in l2s:
                l3s = self.ranked("L3", f"{l1} > {l2}")[:1] if l2 else []
                l3, v3 = (l3s[0][0], l3s[0][1]) if l3s else (None, 1.0)
                found.append({"path": " > ".join(x for x in (l1, l2, l3) if x), "l1": l1, "l2": l2, "l3": l3,
                              "score": _round(v1 * v2 * v3)})
        kept, per = [], Counter()
        for path in sorted(found, key=lambda x: -(x["score"] or 0)):
            if per[path["l1"]] < per_l1:
                per[path["l1"]] += 1
                kept.append({"rank": len(kept) + 1, **path})
        return tuple(kept[:k])


def page_type_confidence(l1: Optional[str], l1_conf: Optional[float], l2: Optional[str], l2_conf: Optional[float],
                         l3: Optional[str], l3_conf: Optional[float], *, model: bool = True) -> Optional[float]:
    """The page type's confidence: the chance its named levels are right, the product of
    their confidences (L2 and L3 conditional on their parent), leaving out the deepest
    named level -- the subtype, shown beside the type with its own confidence
    (``subtype_confidence``), so a doubtful subtype does not flag the page (2026-10-25,
    user: fewer review flags). A level left Others adds no doubt of its own; L1 Others is
    the L1's own (fallback) confidence. Decided without the model (canon only): times
    ``no_model`` once."""
    if l1 is None or l1_conf is None:
        return None
    named = [float(c) for name, c in ((l2, l2_conf), (l3, l3_conf)) if name and name != OTHERS and c is not None]
    value = float(l1_conf)
    for level_conf in named[:-1]:
        value *= level_conf
    return conf.score(value, *(() if model else ("no_model",)), module="page_type")


def subtype_confidence(l2: Optional[str], l2_conf: Optional[float], l3: Optional[str],
                       l3_conf: Optional[float]) -> Optional[float]:
    """The subtype's own confidence (the deepest named level, conditional on its parent);
    None when the page has no named subtype."""
    named = [float(c) for name, c in ((l2, l2_conf), (l3, l3_conf)) if name and name != OTHERS and c is not None]
    return round(named[-1], 4) if named else None


def specialty_confidence(raw: dict[str, Any], specialty: Optional[str], keyword: Optional[tuple[str, float]], *,
                         fallback: bool, not_asserted: Any = ()) -> Optional[float]:
    """How sure the page is of its specialty (stages/lib/confidence.py): the model's
    probability for it, lifted when the specialty keywords (or the canon entry) back it,
    times ``keywords_disagree`` when they name another. When the model names no
    specialty (its best is Unknown) or there is no model, the keywords' own confidence
    (times ``no_model`` without a model). The fallback (General): the model's doubt
    about every named specialty (1 - its best named one) times ``fallback``."""
    if not specialty:
        return None
    probs = {str(n): float(p) for n, p in raw.get("specialty_top") or []}
    if not probs and raw.get("specialty"):
        probs = {str(raw["specialty"]): float(raw.get("specialty_confidence") or 0.0)}
    named = {n: p for n, p in probs.items() if n not in not_asserted}
    if fallback:
        base = probs.get(specialty, 1.0 - max(named.values(), default=0.0)) if probs else 0.5
        return conf.score(base, "fallback", module="specialty")
    if not probs or max(probs, key=probs.get) in not_asserted:
        own = keyword[1] if keyword and keyword[0] == specialty else 0.5
        return conf.score(own, *(() if probs else ("no_model",)), module="specialty")
    base, reasons = probs.get(specialty, 0.0), []
    if keyword and keyword[0] == specialty:
        base = conf.backed(base, keyword[1])
    elif keyword:
        reasons.append("keywords_disagree")
    return conf.score(base, *reasons, module="specialty")


def gate(
    raw: Optional[dict[str, Any]],
    space: dict[str, Any],
    text: str,
    headings: tuple[str, ...] = (),
    s: Optional[dict[str, Any]] = None,
) -> AxesResult:
    """The decided values for one page. ``raw`` is the model's answer
    (``decode`` output; None without a model), ``space`` its label space."""
    s = s or settings()
    raw = raw or {}
    top = [(str(n), float(p)) for n, p in raw.get("L1_top") or []]
    where = route(top, space.get("other_L1", "OTHER"), s) if raw else None
    sources = ["-"] * 5
    voting = _Vote(raw, space, text, headings, s)

    # Each level: the model and the keyword canon vote; the winner takes it when its
    # vote reaches the level's minimum (0.2), else Others.
    l1, l1_conf, sources[0] = voting.pick("L1")
    if not l1:
        l1, sources[0] = OTHERS, BY_OTHERS
        l1_conf = voting.others_confidence("L1", None, raw, space.get("other_L1", "OTHER"))
    l2 = l2_conf = None
    if l1 != OTHERS:
        l2, l2_conf, sources[1] = voting.pick("L2", l1)

    # Rules that look at more than words (lab guard, hospital stay, clinical summary).
    fired = page_rules.apply(text, headings, l1, _ARBITRATION.get().get("axes_rules") or {})
    if fired is not None:
        l1, l1_conf, sources[0] = fired.l1, voting.confidence("L1", None, fired.l1, rule=True), RULE
        l2, sources[1] = fired.l2, RULE
        l2_conf = voting.confidence("L2", l1, fired.l2, rule=True) if fired.l2 else None

    l3 = l3_conf = None
    if l3_names(l1, l2):
        l3, l3_conf, sources[2] = voting.pick("L3", f"{l1} > {l2}")
        if not l3:
            l3, sources[2] = OTHERS, BY_OTHERS
            l3_conf = voting.others_confidence("L3", f"{l1} > {l2}", raw, "")

    under = {}
    for other in tax.l1_names_with_l2():
        if other == l1:
            continue
        o2, o2_conf, who = voting.pick("L2", other)
        if o2:
            o3, o3_conf, _ = voting.pick("L3", f"{other} > {o2}") if l3_names(other, o2) else (None, None, "-")
            under[other] = {"l2": o2, "l2_confidence": _round(o2_conf), "l2_source": who,
                            "l3": o3, "l3_confidence": _round(o3_conf)}

    # The canon page type behind the decided path gives the Extracted codeability (the
    # client's is the Final type's: arbitration.decide_final).
    named_l3 = l3 if l3 != OTHERS else None
    entry_name, canon_conf = canon_entry(text, headings, l1, l2, named_l3) if l1 != OTHERS else (None, None)
    entry = _CANON.get().entries.get(entry_name or "") or {}

    # Specialty: the model's behind its gate; else the specialty keywords; else the canon
    # entry's; else the fallback (General). Never Unknown.
    specialty, _ = _model_specialty(raw, space, s) if raw else (None, None)
    found, score = specialty_keywords.specialty(text)
    keyword_specialty = (found, specialty_keywords.confidence(score)) if found else None
    if specialty:
        sources[3] = MODEL
    elif found:
        specialty, sources[3] = found, CANON
    elif entry.get("specialty") and entry["specialty"] not in s["specialty_not_asserted"]:
        specialty, sources[3] = entry["specialty"], CANON
        keyword_specialty = (specialty, canon_conf or 0.0)
    else:
        specialty, sources[3] = s["specialty_fallback"], BY_OTHERS
    specialty_conf = specialty_confidence(raw, specialty, keyword_specialty, fallback=sources[3] == BY_OTHERS,
                                          not_asserted=s["specialty_not_asserted"])

    # Codeability follows the page type, and the family (L1) settles it for nearly every
    # type (a Progress Note's subtypes are all codeable): it is as sure as the L1 is.
    path_conf = page_type_confidence(l1, l1_conf, l2, l2_conf, l3, l3_conf, model=bool(raw))
    family_conf = page_type_confidence(l1, l1_conf, None, None, None, None, model=bool(raw))
    rule = entry.get("codeability_rule")
    codeability, codeability_conf = NEEDS_REVIEW, None
    if rule == "page_type_tag" and entry.get("codeability", NEEDS_REVIEW) != NEEDS_REVIEW:
        codeability, codeability_conf, sources[4] = entry["codeability"], family_conf, CANON
    elif fired is not None and not entry and fired.codeability:
        codeability, codeability_conf, sources[4] = fired.codeability, family_conf, RULE

    return AxesResult(
        route=where,
        l1=l1, l1_confidence=_round(l1_conf),
        l2=l2, l2_confidence=_round(l2_conf),
        l3=l3, l3_confidence=_round(l3_conf),
        page_type_confidence=_round(path_conf),
        specialty=specialty, specialty_confidence=_round(specialty_conf),
        codeability=codeability, codeability_confidence=_round(codeability_conf),
        sources=tuple(sources),
        canon_page_type=entry_name,
        codeability_rule=rule,
        title_l1=title_l1(text, headings),
        rules=(fired.rule,) if fired is not None else (),
        model_paths=model_paths(raw, space) if raw else (),
        paths=voting.paths(),
        under_l1=under,
        prediction=_raw_json(raw),
    )


def _raw_json(raw: dict[str, Any]) -> dict[str, Any]:
    """The model's whole answer, rounded, for the raw prediction."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in ("by_L1", "paths", "distributions"):  # in under_l1 / model_paths / the vote
            continue
        if key in ("L1_top", "specialty_top"):
            out[key] = [[n, _round(p)] for n, p in value or []]
        elif key.endswith("_confidence"):
            out[key] = _round(value)
        else:
            out[key] = value
    return out


# --- The model --------------------------------------------------------------------------


# The weight files. A clone made while they were in git LFS, on a machine
# without git-lfs, has a ~130-byte text pointer in their place.
WEIGHT_FILES = ("heads.pt", "encoder/model.safetensors")
_LFS_POINTER = b"version https://git-lfs"


def _missing(folder: Path) -> list[str]:
    return [name for name in MODEL_FILES if not (folder / name).is_file()]


def _lfs_pointers(folder: Path) -> list[str]:
    """Weight files that are git LFS pointers, not weights."""
    out = []
    for name in WEIGHT_FILES:
        path = folder / name
        try:
            if path.is_file() and path.stat().st_size < 1024:
                with path.open("rb") as handle:
                    if handle.read(len(_LFS_POINTER)) == _LFS_POINTER:
                        out.append(name)
        except OSError:
            continue
    return out


def _unusable(folder: Path) -> Optional[str]:
    """Why the folder cannot be loaded, or None."""
    missing = _missing(folder)
    if missing:
        return f"no model at {folder} ({', '.join(missing)} missing)"
    pointers = _lfs_pointers(folder)
    if pointers:
        return (f"{', '.join(pointers)} in {folder} is a git LFS pointer, not the model; "
                "copy the real file there (docs/APPROACH.md §7.4)")
    return None


def check_taxonomy(space: dict[str, Any], folder: Path) -> None:
    """Refuses a model trained on another taxonomy version than the axes name pages in."""
    expected = settings().get("taxonomy_version")
    found = space.get("taxonomy_version")
    if expected and found != expected:
        raise ModelTaxonomyError(
            f"{folder} was trained on taxonomy {found or '(none)'}; page_arbitration.json "
            f"→ axes expects {expected}"
        )


def _load() -> Optional[dict[str, Any]]:
    global _loaded, _failed
    if _loaded is not None or _failed is not None:
        return _loaded
    with _lock:
        if _loaded is not None or _failed is not None:
            return _loaded
        folder = model_dir()
        unusable = _unusable(folder)
        if unusable:
            _failed = unusable
            logger.warning("Page axes: %s; canon only", _failed)
            return None
        if not (find_spec("transformers") and find_spec("torch")):
            _failed = "transformers / torch not installed"
            logger.warning("Page axes: %s; canon only", _failed)
            return None
        import torch

        from stages.lib.page_classify import axes_model

        try:
            model, tokenizer, space = axes_model.load_model(folder)
            check_taxonomy(space, folder)
        except Exception as exc:  # noqa: BLE001 — a bad model must not sink the chart
            _failed = f"model at {folder} failed to load: {str(exc).splitlines()[0][:200]}"
            logger.error("Page axes: %s; canon only", _failed)
            return None
        _loaded = {"model": model, "tokenizer": tokenizer, "space": space, "torch": torch}
        logger.info("Page axes: %d L1 classes, %d specialties from %s",
                    len(space["L1"]), len(space["specialty"]), folder)
        return _loaded


def predict(text: str, headings: tuple[str, ...] = ()) -> Optional[AxesResult]:
    """The decided axes for one page, or None when the page has no text.
    Without a model the canon decides every level."""
    if not (text or "").strip():
        return None
    loaded = _load()
    if loaded is None:
        return gate(None, {}, text, headings)
    from stages.lib.page_classify import axes_model

    torch, space = loaded["torch"], loaded["space"]
    ids = axes_model.encode_texts(
        loaded["tokenizer"], [text],
        int(space.get("max_length", axes_model.MAX_LENGTH)),
        float(space.get("head_fraction", axes_model.HEAD_FRACTION)),
    )[0]
    with _lock, torch.no_grad():
        logits = loaded["model"](torch.tensor([ids]), torch.ones((1, len(ids)), dtype=torch.long))
        raw = axes_model.decode(logits, loaded["model"], space)[0]
    return gate(raw, space, text, headings)


def _space_summary(space: dict[str, Any]) -> dict[str, Any]:
    return {
        "taxonomy_version": space.get("taxonomy_version"),
        "l1_classes": len(space.get("L1") or []),
        "specialties": len(space.get("specialty") or []),
    }


def describe() -> dict[str, Any]:
    """For /health: what the model folder holds, without loading any weights."""
    import json

    folder = model_dir()
    unusable = _unusable(folder)
    if unusable:
        return {"ready": False, "reason": f"{unusable}; canon only", "path": str(folder)}
    try:
        space = json.loads((folder / "label_space.json").read_text(encoding="utf-8"))
        check_taxonomy(space, folder)
    except (OSError, ValueError, ModelTaxonomyError) as exc:
        return {"ready": False, "reason": str(exc), "path": str(folder)}
    if not (find_spec("transformers") and find_spec("torch")):
        return {"ready": False, "reason": "transformers / torch not installed; canon only", "path": str(folder)}
    return {"ready": True, "path": str(folder), **_space_summary(space)}


def status() -> dict[str, Any]:
    """For the stage's result: whether the model ran."""
    try:
        loaded = _load()
    except ModelTaxonomyError as exc:
        return {"ready": False, "reason": str(exc), "path": str(model_dir())}
    if loaded is None:
        return {"ready": False, "reason": _failed, "path": str(model_dir())}
    return {"ready": True, "path": str(model_dir()), **_space_summary(loaded["space"])}
