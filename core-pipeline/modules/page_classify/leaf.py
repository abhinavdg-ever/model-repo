"""The page classifier: the page's type as one **leaf** (a whole path L1 > L2 > L3), from the
page-leaf model and the keyword canon (``page_keywords.json``) together (2026-11-01, user; the
only classifier since 2026-11-22: the page-axes cascade, its model and keyword vote were removed).

1. **Model**: its best leaves -- the top ``top_k`` (5) above its minimum, at most
   ``per_l1`` (3) under one L1, ties at the cut kept, from the page-leaf model. Without its
   folder (or torch) the keywords decide alone (``model_source: none``, /health says why).
2. **Keywords** (``leaf_canon``): each leaf's score from its own and its ancestors' lists
   (a word in a section header x2, a title in the title zone x1.5); the best 5 the same way.
3. **Gates**: a leaf whose node has Must Have words and none on the page (a Deciding word
   of the node also counts), or with a Negative word on the page, is out.
4. **Deciding**: a node with a Deciding word on the page is sure: only leaves under it can
   win, unless the model is over ``model_sure_other_l1`` (0.90) sure of another L1.
5. **Rank points**: each side gives its ranked leaves 6 - rank points (ties share a rank);
   the leaf with the most points wins (then the model's points, the keyword score, the
   model's probability). An ``Others`` leaf wins only when no other leaf has points.
6. **Confidence** (2026-11-14, ``chosen_confidence``, CONFIDENCE_LOGIC.md section 12): the
   leaf model's probability for the chosen leaf; keywords only, Others, no model -> backup
   (0.40); a Deciding word against the model's top (either way) -> flipped (0.30).
   The older value below is kept as ``strength``, the weight later stages decide with.
   **Strength** (``leaf_confidence``, settings ``confidence`` in page_keywords.json):
   the model's probability for the winner lifted by the keywords' support for it (a
   Deciding word 1.0 in a header / 0.85 on the page, else 0.7 / 0.45 / 0.25 by the
   keywords' rank); the model alone -> its probability (x keywords_disagree, x close_call
   when its first two are within 0.10); the keywords alone -> 0.4 + 0.5 x support (x
   no_model / model_disagree); x conflict when a Deciding word was overruled; an Others
   leaf at most 0.6.

Codeability is the leaf's (``taxonomy.codeability_of``). Specialty: the model's specialty head
(>= ``specialty_min``, not Unknown / Others), else a ``specialty_titles`` phrase in the title
zone, else the specialty keywords (``specialty_keywords``), else General. ``title_l1``: the L1
whose page title heads the page. ``under_l1``: under every other L1 with L2s, the leaf the page
would take there (the best by the same points), for the Final step. The answer is a
``result.PageResult``, so continuity, Final and review-ui read it as before; the leaf's working
(both lists, gates, hits, points) is in ``PageResult.leaf``.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Iterable, Optional

from modules import confidence as conf
from modules.page_classify import agreement, leaf_canon, priority, specialty_keywords
from modules.page_classify import result as page_result
from modules.page_classify import taxonomy as tax

logger = logging.getLogger(__name__)

SEP = " > "
OTHERS = "Others"
MODEL_FILES = ("label_space.json", "heads.pt", "encoder/config.json")
LEAF_MODEL, NO_MODEL = "leaf_model", "none"

_lock = threading.Lock()
_loaded: Optional[dict[str, Any]] = None
_failed: Optional[str] = None


def model_dir() -> Path:
    from common.config import PAGE_LEAF_MODEL_DIR

    return Path(PAGE_LEAF_MODEL_DIR)


# --- The taxonomy's leaves --------------------------------------------------------------------------


def taxonomy_leaves() -> dict[str, tuple[str, Optional[str], Optional[str]]]:
    """Every leaf path -> (L1, L2, L3): an L1 without L2s, an L2 without L3s, every L3."""
    space = tax.label_space()
    out: dict[str, tuple[str, Optional[str], Optional[str]]] = {}
    for l1 in space["L1"]:
        l2s = space["L2"].get(l1) or []
        if not l2s:
            out[l1] = (l1, None, None)
        for l2 in l2s:
            parent = f"{l1}{SEP}{l2}"
            l3s = space["L3"].get(parent) or []
            if not l3s:
                out[parent] = (l1, l2, None)
            for l3 in l3s:
                out[f"{parent}{SEP}{l3}"] = (l1, l2, l3)
    return out


def is_others(path: str) -> bool:
    return path.split(SEP)[-1] == OTHERS


def under(path: str, node: str) -> bool:
    return path == node or path.startswith(node + SEP)


def renamed(path: str) -> str:
    """A path in an older taxonomy's names (page_taxonomy.json -> name_map) in today's."""
    from modules.page_classify.taxonomy import _TAXONOMY

    name_map = _TAXONOMY.get().get("name_map") or {}
    for level in ("L3", "L2", "L1"):
        for old, new in (name_map.get(level) or {}).items():
            if " > " in old and (path == old or path.startswith(old + SEP)):
                return new + path[len(old):]
            if " > " not in old and level == "L1" and path.split(SEP)[0] == old:
                return SEP.join([new, *path.split(SEP)[1:]])
    return path


# --- Ranking ------------------------------------------------------------------------------------------


def ranked(scores: dict[str, float], k: int, per_l1: int) -> dict[str, int]:
    """name -> rank (1 is best; ties share a rank) for the best ``k`` ranks, at most
    ``per_l1`` names under one L1; a name tied with the last one kept is kept."""
    out: dict[str, int] = {}
    per: dict[str, int] = {}
    rank, last = 0, None
    for position, (name, score) in enumerate(sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])), start=1):
        if score <= 0:
            break
        if last is None or round(score, 4) != round(last, 4):
            rank = position
            if rank > k:
                break
        l1 = name.split(SEP)[0]
        if per.get(l1, 0) >= per_l1:
            continue
        per[l1] = per.get(l1, 0) + 1
        out[name] = rank
        last = score
    return out


def points(rank: Optional[int], k: int) -> float:
    return float(k + 1 - rank) if rank else 0.0


# --- The model side -----------------------------------------------------------------------------------


def _load() -> Optional[dict[str, Any]]:
    global _loaded, _failed
    if _loaded is not None or _failed is not None:
        return _loaded
    with _lock:
        if _loaded is not None or _failed is not None:
            return _loaded
        folder = model_dir()
        missing = [f for f in MODEL_FILES if not (folder / f).is_file()]
        if missing:
            _failed = f"no page-leaf model at {folder} (missing {', '.join(missing)})"
            logger.info("Page leaf: %s; the keywords decide alone", _failed)
            return None
        if not (find_spec("transformers") and find_spec("torch")):
            _failed = "transformers / torch not installed"
            return None
        import torch

        from modules.page_classify import leaf_model

        try:
            model, tokenizer, space = leaf_model.load_model(folder)
        except Exception as exc:  # noqa: BLE001 - a bad model must not sink the chart
            _failed = f"model at {folder} failed to load: {str(exc).splitlines()[0][:200]}"
            logger.error("Page leaf: %s", _failed)
            return None
        if space.get("taxonomy_version") != tax.version():
            logger.warning("Page leaf: model taxonomy %s, pipeline %s", space.get("taxonomy_version"), tax.version())
        _loaded = {"model": model, "tokenizer": tokenizer, "space": space, "torch": torch,
                   "trained_as": trained_as(space, taxonomy_leaves())}
        logger.info("Page leaf: %d leaf classes from %s", len(space["leaf"]), folder)
        return _loaded


@dataclass
class ModelSide:
    source: str = NO_MODEL
    candidates: list[dict[str, Any]] = field(default_factory=list)  # {"leaf"|"node", "score"}
    probs: dict[str, float] = field(default_factory=dict)  # leaf or node -> probability
    specialty: Optional[tuple[str, float]] = None
    # A leaf the model has no class for (folded: too few pages, or never seen) -> the class
    # it trained as (its parent's Others, or the parent). That class's rank and probability
    # stand for the leaf when the keywords find it (2026-11-02, user).
    trained_as: dict[str, str] = field(default_factory=dict)


def trained_as(space: dict[str, Any], leaves: Iterable[str]) -> dict[str, str]:
    """Leaf -> the model class that stands for it, for every taxonomy leaf without a class
    of its own: ``label_space.json -> folded`` first, else the nearest ancestor's Others
    class, else the nearest ancestor that is a class."""
    classes = {renamed(c) for c in space.get("leaf") or []}
    out = {renamed(leaf): renamed(cls) for leaf, cls in (space.get("folded") or {}).items()}
    for leaf in leaves:
        if leaf in classes or leaf in out:
            continue
        for node in leaf_canon.ancestors(leaf)[1:]:
            others = f"{node}{SEP}{OTHERS}"
            if others in classes and others != leaf:
                out[leaf] = others
                break
            if node in classes:
                out[leaf] = node
                break
    return {leaf: cls for leaf, cls in out.items() if leaf != cls}


def model_side(text: str, k: int, per_l1: int) -> ModelSide:
    loaded = _load()
    if loaded is not None:
        from modules.page_classify import leaf_model

        torch, space = loaded["torch"], loaded["space"]
        ids = leaf_model.encode_texts(loaded["tokenizer"], [text], int(space.get("max_length", 512)),
                                      float(space.get("head_fraction", 0.85)))[0]
        with _lock, torch.no_grad():
            logits = loaded["model"](torch.tensor([ids]), torch.ones((1, len(ids)), dtype=torch.long))
            row = leaf_model.decode(logits, space, k, per_l1, float(space.get("min_prob", leaf_model.MIN_PROB)))[0]
        probs = {renamed(name): p for name, p in row["leaf_probs"].items()}
        cands = [{"path": renamed(leaf["leaf"]), "score": leaf["score"]} for leaf in row["leaves"]]
        spec = (row["specialty"], row["specialty_confidence"]) if row.get("specialty") else None
        return ModelSide(LEAF_MODEL, cands, probs, spec, trained_as=loaded["trained_as"])
    return ModelSide()


# --- The decision -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    path: str
    how: str  # agree | deciding | model | keywords | nothing
    confidence: Optional[float]
    model_rank: dict[str, int]
    keyword_rank: dict[str, int]
    totals: dict[str, float]
    deciding: Optional[str]
    deciding_overruled: Optional[str]
    excluded: dict[str, str]
    confidence_why: dict[str, Any] = field(default_factory=dict)
    # The pre-2026-11-14 confidence (leaf_confidence: model p backed by keywords, penalties):
    # kept as the weight continuity and the Final vote read, so no decision changes.
    strength: Optional[float] = None
    # Every eligible leaf with points (not only the Deciding pool): the page's best leaf
    # under each other L1 (under_l1).
    all_points: dict[str, float] = field(default_factory=dict)
    # The family priority step (priority.py): the rule that restricted the winner's family.
    priority_rule: Optional[dict[str, Any]] = None


def tie_order(leaves: dict[str, Any], settings: dict[str, Any]) -> dict[str, int]:
    """Leaf -> its place when scores tie exactly: leaves under a ``tie_priority`` L1 first (in
    that list's order), then every other leaf; within each, the taxonomy's order."""
    priority = [str(p) for p in settings.get("tie_priority") or []]
    rank = {l1: i for i, l1 in enumerate(priority)}
    taxonomy = list(leaves)
    ordered = sorted(taxonomy, key=lambda leaf: (rank.get(leaf.split(SEP)[0], len(rank)), taxonomy.index(leaf)))
    return {leaf: i for i, leaf in enumerate(ordered)}


def decide(leaves: dict[str, Any], model: ModelSide, kw: leaf_canon.LeafKeywords,
           settings: dict[str, Any], fired: Optional[priority.Fired] = None) -> Decision:
    k, per_l1 = int(settings["top_k"]), int(settings["per_l1"])
    eligible = {leaf for leaf in leaves if leaf not in kw.excluded}

    # Model ranks. A candidate that is a node (a path above the leaves) stands for the leaves under it the keywords scored; with none, for its
    # Others leaf, else for the node itself.
    model_rank_raw = ranked({c["path"]: c["score"] for c in model.candidates}, k, per_l1)
    model_rank: dict[str, int] = {}
    keyword_rank = ranked({leaf: s for leaf, s in kw.scores.items() if leaf in eligible}, k, per_l1)
    stood_for: dict[str, list[str]] = {}
    for leaf, cls in model.trained_as.items():
        stood_for.setdefault(cls, []).append(leaf)
    for path, rank in model_rank_raw.items():
        if path in leaves:
            # A class that stands for folded / unseen leaves ranks those the keywords ranked too.
            targets = [path, *(leaf for leaf in stood_for.get(path, ()) if leaf in keyword_rank)]
        else:
            targets = [leaf for leaf in leaves if under(leaf, path) and leaf in kw.scores]
            if not targets:
                others = f"{path}{SEP}{OTHERS}"
                targets = [others] if others in leaves else [path]
                if path not in leaves and others not in leaves:
                    eligible.add(path)  # an inner node the taxonomy has no Others for
        for leaf in targets:
            if leaf not in model_rank or rank < model_rank[leaf]:
                model_rank[leaf] = rank

    # The family priority step (2026-11-22, Brief 4 section 3): the Progress Note model guard or
    # the first priority rule that fired restricts the winner to its family.
    fired = fired or priority.Fired()
    family = fired.family
    in_family = {leaf for leaf in eligible if not family or under(leaf, family)}

    # Deciding: the best-scored node with a Deciding word that still has an eligible leaf (inside
    # the priority family when there is one).
    deciding = overruled = None
    for node, _score in sorted(kw.deciding.items(), key=lambda kv: (-kv[1], -kv[0].count(SEP))):
        if any(under(leaf, node) for leaf in in_family):
            deciding = node
            break
    top_model = max(model.candidates, key=lambda c: c["score"], default=None)
    if deciding and top_model and top_model["score"] > float(settings["model_sure_other_l1"]) \
            and top_model["path"].split(SEP)[0] != deciding.split(SEP)[0]:
        overruled = f"model {top_model['score']:.2f} sure of {top_model['path']}"
        deciding = None
    pool = {leaf for leaf in in_family if not deciding or under(leaf, deciding)}

    all_points = {leaf: points(model_rank.get(leaf), k) + points(keyword_rank.get(leaf), k) for leaf in eligible}
    totals = {leaf: all_points[leaf] for leaf in pool if leaf in all_points}
    scored = {leaf: t for leaf, t in totals.items() if t > 0}
    named = {leaf: t for leaf, t in scored.items() if not is_others(leaf)}
    choice_pool = named or scored

    order = tie_order(leaves, settings)

    def key(leaf: str) -> tuple:
        # The last term breaks an exact tie: the tie_priority L1s (the notes) first, then the
        # taxonomy's order, so the same page always gets the same leaf (2026-11-08, user).
        return (totals[leaf], points(model_rank.get(leaf), k), kw.scores.get(leaf, 0.0),
                _prob(model, leaf), -leaf.count(SEP) if is_others(leaf) else 0,
                -order.get(leaf, len(order)))

    if choice_pool:
        winner = max(choice_pool, key=key)
    elif deciding:
        others = f"{deciding}{SEP}{OTHERS}"
        winner = others if others in pool else deciding
    elif family:
        # Nothing scored inside the family: the model's likeliest leaf there, else its Others.
        mine = [leaf for leaf in in_family if _prob(model, leaf) > 0]
        others = f"{family}{SEP}{OTHERS}"
        winner = max(mine, key=lambda leaf: _prob(model, leaf)) if mine else others if others in leaves else family
    elif top_model:
        winner = top_model["path"]
    else:
        winner = OTHERS

    model_first = min(model_rank_raw, key=model_rank_raw.get) if model_rank_raw else None
    keyword_first = min(keyword_rank, key=keyword_rank.get) if keyword_rank else None
    model_has = any(stands_for(model, winner, path) for path in model_rank_raw)
    model_family = str(model_first).split(SEP)[0] if model_first else None
    if fired.rule and fired.rule != priority.GUARD and family and model_family != family:
        how = "priority"  # a priority rule chose another family than the model's
    elif deciding and under(winner, deciding):
        how = "deciding"
    elif model_first and keyword_first and stands_for(model, winner, model_first) and winner == keyword_first:
        how = "agree"
    elif model_first and stands_for(model, winner, model_first):
        how = "model"
    elif winner in kw.scores:
        how = "keywords"
    else:
        how = "nothing"
    support = keyword_support(winner, kw, keyword_rank, deciding, settings)
    strength, why = leaf_confidence(
        p=_prob(model, winner), support=support, model_ranked=model_has,
        model_ranked_other=bool(model_rank_raw) and not model_has,
        keywords_ranked_other=bool(keyword_rank) and support == 0.0,
        has_model=model.source != NO_MODEL, margin=_margin(model, winner, model_first),
        overruled=bool(overruled), others=is_others(winner), settings=settings,
    )
    # 2026-11-14 (user): a Deciding word and the model that differ only below the L1 are not a
    # flip: the model's own probability for that L1 (its best candidate under it) is used.
    winner_l1 = winner.split(SEP)[0]
    same_l1 = [float(c["score"]) for c in model.candidates if str(c["path"]).split(SEP)[0] == winner_l1]
    confidence, rule = chosen_confidence(
        model_l1_agrees=bool(model_first) and str(model_first).split(SEP)[0] == winner_l1,
        p_l1=max(same_l1) if same_l1 else None,
        how=how, p=_prob(model, winner), has_model=model.source != NO_MODEL and bool(model_first),
        model_agrees=bool(model_first) and stands_for(model, winner, model_first),
        overruled=bool(overruled), others=is_others(winner),
    )
    if how == "priority" and model_first:
        confidence, rule = conf.flipped(), f"priority rule {fired.rule} against the model's family: flipped"
    # 2026-11-24 (user): the agreement of the model and the keywords on the chosen type sets the
    # confidence (agreement.py); a Deciding word the model overruled and an Others leaf keep theirs.
    agree_cfg = agreement.settings(((settings.get("confidence") or {}).get("agreement")))
    has_model = model.source != NO_MODEL and bool(model_first)
    # 2026-11-25 (user): read on the family (the level the ground truth grades), not the leaf.
    family = winner.split(SEP)[0]
    mr = agreement.family_ranks(model_rank_raw).get(family)
    kr = agreement.family_ranks(keyword_rank).get(family)
    agreement_tier = agreement.tier(mr, kr, has_model)
    if agree_cfg["enabled"] and not overruled and not is_others(winner):
        confidence = agreement.page_type_confidence(
            agreement_tier, agreement.family_mass(dict(model.probs), family), agree_cfg, has_model)
        rule = f"agreement tier {agreement_tier} (family)"
    why = {**why, "strength": strength, "rule_2026_11_14": rule, "agreement_tier": agreement_tier,
           "agreement_basis": "family", "model_rank": mr, "keyword_rank": kr}
    return Decision(winner, how, confidence, model_rank_raw, keyword_rank,
                    {leaf: t for leaf, t in sorted(scored.items(), key=lambda kv: -kv[1])[: 2 * k]},
                    deciding, overruled, {leaf: why for leaf, why in kw.excluded.items()}, why, strength,
                    {leaf: t for leaf, t in all_points.items() if t > 0}, fired.record())


def chosen_confidence(*, how: str, p: float, has_model: bool, model_agrees: bool, overruled: bool,
                      others: bool, model_l1_agrees: bool = False,
                      p_l1: Optional[float] = None) -> tuple[Optional[float], str]:
    """The page type's confidence (2026-11-14, user; CONFIDENCE_LOGIC.md 12.2): the leaf
    model's probability for the chosen leaf when the model decided it (or a Deciding word
    chose what the model's top also says); ``confidence.flipped`` when a Deciding word
    overruled the model's top or the model overruled a Deciding word; ``confidence.backup``
    for keywords only, an Others leaf, or no model. No keyword lift, no penalties."""
    if others:
        return conf.backup(), "Others leaf: backup"
    if overruled:
        return conf.flipped(), "the model overruled a Deciding keyword: flipped"
    if how in ("agree", "model"):
        return conf.chosen(p, module="page_type"), "model probability"
    if how == "deciding":
        if not has_model:
            return conf.backup(), "Deciding keyword, no model: backup"
        if model_agrees:
            return conf.chosen(p, module="page_type"), "Deciding keyword and the model's top agree: model probability"
        if model_l1_agrees and p_l1 is not None:
            # 2026-11-14 (user): flipped only when the L1 differs.
            return conf.chosen(p_l1, module="page_type"), "Deciding keyword and the model agree on the L1: model probability"
        return conf.flipped(), "a Deciding keyword overruled the model's top L1: flipped"
    return conf.backup(), "keywords only: backup" if how == "keywords" else "neither: backup"


CONFIDENCE = leaf_canon.DEFAULT_SETTINGS["confidence"]


def keyword_support(winner: str, kw: leaf_canon.LeafKeywords, keyword_rank: dict[str, int],
                    deciding: Optional[str], settings: dict[str, Any]) -> float:
    """How much the keywords back the winning leaf, 0-1 (``confidence.support``)."""
    table = {**CONFIDENCE, **(settings.get("confidence") or {})}["support"]
    if deciding and under(winner, deciding):
        in_header = any(where == "header" for where in (kw.hits.get(deciding, {}).get("deciding") or {}).values())
        return float(table["deciding_header" if in_header else "deciding_page"])
    rank = keyword_rank.get(winner)
    if not rank:
        return 0.0
    return float(table["rank_1"] if rank == 1 else table["rank_2_3"] if rank <= 3 else table["rank_4_5"])


def _margin(model: ModelSide, winner: str, model_first: Optional[str]) -> Optional[float]:
    """The gap between the model's first and second leaf, when the winner is its first."""
    if not model_first or not stands_for(model, winner, model_first):
        return None
    scores = sorted((c["score"] for c in model.candidates), reverse=True)
    return scores[0] - scores[1] if len(scores) > 1 else None


def leaf_confidence(*, p: float, support: float, model_ranked: bool, model_ranked_other: bool,
                    keywords_ranked_other: bool, has_model: bool, margin: Optional[float], overruled: bool,
                    others: bool, settings: dict[str, Any]) -> tuple[Optional[float], dict[str, Any]]:
    """The page type's pre-2026-11-14 confidence, kept as ``Decision.strength`` (the weight
    continuity and the Final vote read; the stored confidence is ``chosen_confidence``) and
    how it was reached.

    * the model ranked the winner and the keywords back it: the model's probability
      lifted by the support (``confidence.backed``: support 1.0 closes half the gap to 1);
    * the model ranked it, the keywords did not: the model's probability, x
      keywords_disagree when the keywords ranked other leaves, x close_call when the
      model's first two leaves are within 0.10;
    * only the keywords: 0.4 + 0.5 x support, x no_model without a model, x model_disagree
      when the model ranked other leaves;
    * neither: the model's probability (0.3 without one), x no_model.
    Then x conflict when a Deciding word was overruled by the model, and an Others leaf
    is capped at 0.6. The page's own doubts (handwritten, thin text) are applied after,
    by the stage."""
    c = {**CONFIDENCE, **(settings.get("confidence") or {})}
    reasons: list[str] = []
    factors: list[str] = []
    if model_ranked and support > 0:
        rule, value = "model backed by keywords", conf.backed(p, support)
    elif model_ranked:
        rule, value = "model alone", p
        if keywords_ranked_other:
            reasons.append("keywords_disagree")
        if margin is not None and margin < float(c["close_call_margin"]):
            value *= float(c["close_call"])
            factors.append(f"close_call x{c['close_call']}")
    elif support > 0:
        rule = "keywords alone"
        value = float(c["keywords_only_base"]) + float(c["keywords_only_span"]) * support
        if not has_model:
            reasons.append("no_model")
        elif model_ranked_other:
            value *= float(c["model_disagree"])
            factors.append(f"model_disagree x{c['model_disagree']}")
    else:
        rule, value = "neither", p or 0.3
        if not has_model:
            reasons.append("no_model")
    if overruled:
        reasons.append("conflict")
    out = conf.score(value, *reasons, module="page_type")
    if others and out is not None and out > float(c["others_cap"]):
        out = float(c["others_cap"])
        factors.append(f"others_cap {c['others_cap']}")
    return out, {"rule": rule, "model_p": round(p, 4), "keyword_support": round(support, 4),
                 "penalties": reasons or None, "factors": factors or None}


def _prob(model: ModelSide, path: str) -> float:
    """The model's probability for ``path``: its own, else the class it trained as (a
    folded or unseen leaf), else the closest ancestor's."""
    if path not in model.probs and model.trained_as.get(path) in model.probs:
        return float(model.probs[model.trained_as[path]])
    for node in leaf_canon.ancestors(path):
        if node in model.probs:
            return float(model.probs[node])
    return 0.0


def stands_for(model: ModelSide, leaf: str, path: str) -> bool:
    """True when the model's ``path`` covers ``leaf``: the leaf itself, a node above it, or
    the class the leaf trained as."""
    return under(leaf, path) or model.trained_as.get(leaf) == path


# --- One page -----------------------------------------------------------------------------------------


def specialty_settings() -> dict[str, Any]:
    c = leaf_canon.canon().settings
    return {"min": float(c.get("specialty_min", 0.5)),
            "not_asserted": tuple(c.get("specialty_not_asserted") or ("Unknown", "Others")),
            "fallback": str(c.get("specialty_fallback") or "General")}


def page_specialty(model: ModelSide, text: str, headings: tuple[str, ...]) -> tuple[str, Optional[float], str]:
    """(specialty, confidence, who): the model's specialty head when it is sure enough; else a
    specialty title in the title zone; else the specialty keywords; else General. Only the
    model's carries its own probability; the others are a backup (CONFIDENCE_LOGIC.md 12.2)."""
    s = specialty_settings()
    if model.specialty and model.specialty[0] not in s["not_asserted"] and model.specialty[1] >= s["min"]:
        return model.specialty[0], round(float(model.specialty[1]), 4), page_result.MODEL
    found = leaf_canon.specialty_title(text, headings) or specialty_keywords.specialty(text)[0]
    if found:
        return found, conf.backup(), page_result.CANON
    return s["fallback"], conf.backup(), page_result.BY_OTHERS


def under_l1(leaves: dict[str, Any], decision: Decision, model: ModelSide, kw: leaf_canon.LeafKeywords,
             winner_l1: str) -> dict[str, Any]:
    """Under every other L1 that has L2s: the leaf the page would take there -- the best by the
    same points (then the model's probability, the keyword score) -- as
    {L1: {l2, l2_confidence, l2_source, l3, l3_confidence}}. The Final step reads it when the
    page's document has another L1."""
    out: dict[str, Any] = {}
    for other in tax.l1_names_with_l2():
        if other == winner_l1:
            continue
        mine = [leaf for leaf in decision.all_points if leaf.split(SEP)[0] == other]
        if not mine:
            continue
        best = max(mine, key=lambda leaf: (decision.all_points[leaf], _prob(model, leaf), kw.scores.get(leaf, 0.0)))
        _l1, l2, l3 = leaves.get(best) or _split(best)
        if not l2:
            continue
        p = round(_prob(model, best), 4) or None
        out[other] = {"l2": l2, "l2_confidence": p,
                      "l2_source": page_result.MODEL if p else page_result.CANON,
                      "l3": l3, "l3_confidence": p if l3 else None}
    return out


def predict(text: str, headings: tuple[str, ...] = ()) -> Optional[page_result.PageResult]:
    """The page's type as one leaf (a PageResult); None when the page has no text."""
    if not (text or "").strip():
        return None
    settings = leaf_canon.canon().settings
    leaves = taxonomy_leaves()
    model = model_side(text, int(settings["top_k"]), int(settings["per_l1"]))
    kw = leaf_canon.score_leaves(leaves, text, headings)
    top = max(model.candidates, key=lambda c: c["score"], default=None)
    fired = priority.decide(text, tuple(headings), model.probs if model.source != NO_MODEL else {},
                            (top["path"], float(top["score"])) if top else None,
                            float(settings["model_sure_other_l1"]))
    decision = decide(leaves, model, kw, settings, fired)

    l1, l2, l3 = leaves.get(decision.path) or _split(decision.path)
    if l1 == OTHERS:
        l2 = l3 = None
    path_conf = decision.confidence
    codeability = tax.codeability_of(l1, l2, l3) or page_result.NEEDS_REVIEW
    who = (page_result.BY_OTHERS if l1 == OTHERS or decision.how == "nothing"
           else page_result.MODEL if decision.how in ("agree", "model")
           else page_result.RULE if decision.how == "priority" else page_result.CANON)
    specialty, specialty_conf, specialty_by = page_specialty(model, text, headings)
    # 2026-11-24 (user): codability's own confidence -- the model's probability for every type with
    # this codability, checked against both approaches' firsts (agreement.py).
    agree_cfg = agreement.settings(((settings.get("confidence") or {}).get("agreement")))

    def _codeability(path: str) -> Optional[str]:
        a1, a2, a3 = leaves.get(path) or _split(path)
        return tax.codeability_of(a1, a2, a3)

    cod_conf, cod_why = (path_conf, "page type's") if not agree_cfg["enabled"] else agreement.codability_confidence(
        codeability if codeability != page_result.NEEDS_REVIEW else None, dict(model.probs),
        decision.model_rank, decision.keyword_rank, model.source != NO_MODEL and bool(decision.model_rank),
        _codeability, agree_cfg)
    sources = (who, who if l2 else "-", who if l3 else "-", specialty_by, who)
    return page_result.PageResult(
        l1=l1, l1_confidence=path_conf, l2=l2, l2_confidence=path_conf if l2 else None,
        l3=l3, l3_confidence=path_conf if l3 else None, page_type_confidence=path_conf,
        specialty=specialty, specialty_confidence=specialty_conf,
        codeability=codeability,
        codeability_confidence=cod_conf if codeability != page_result.NEEDS_REVIEW else None,
        sources=sources,
        title_l1=leaf_canon.title_l1(text, headings),
        paths=tuple({"rank": i, "path": leaf, "points": total,
                     "model_rank": decision.model_rank.get(leaf), "keyword_rank": decision.keyword_rank.get(leaf)}
                    for i, (leaf, total) in enumerate(decision.totals.items(), start=1)),
        under_l1=under_l1(leaves, decision, model, kw, l1),
        rules=(decision.priority_rule["rule"],) if decision.priority_rule and decision.priority_rule.get("rule") else (),
        leaf={
            "leaf": decision.path, "how": decision.how, "confidence": decision.confidence,
            "agreement": {"tier": decision.confidence_why.get("agreement_tier"),
                          "model_rank": decision.confidence_why.get("model_rank"),
                          "keyword_rank": decision.confidence_why.get("keyword_rank"),
                          "codability_confidence": cod_conf, "codability_why": cod_why},
            "strength": decision.strength,
            "confidence_why": decision.confidence_why,
            "model_source": model.source,
            "model_top": [{"path": p, "rank": r, "score": round(_prob(model, p), 4)}
                          for p, r in sorted(decision.model_rank.items(), key=lambda kv: kv[1])],
            "keyword_top": [{"path": p, "rank": r, "score": kw.scores.get(p)}
                            for p, r in sorted(decision.keyword_rank.items(), key=lambda kv: kv[1])],
            "deciding": decision.deciding, "deciding_overruled": decision.deciding_overruled,
            "priority_rule": decision.priority_rule,
            "excluded": dict(list(decision.excluded.items())[:20]) or None,
            "hits": {node: hits for node, hits in kw.hits.items()
                     if decision.path == node or decision.path.startswith(node + SEP)} or None,
        },
    )


def _split(path: str) -> tuple[str, Optional[str], Optional[str]]:
    parts = path.split(SEP) + [None, None]
    return parts[0], parts[1], parts[2]


def describe() -> dict[str, Any]:
    """For /health: what the leaf model folder holds, without loading any weights."""
    import json

    folder = model_dir()
    missing = [f for f in MODEL_FILES if not (folder / f).is_file()]
    if missing:
        return {"ready": False, "path": str(folder), "classifier": "leaf",
                "reason": f"missing {', '.join(missing)}; the keywords decide alone"}
    try:
        space = json.loads((folder / "label_space.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ready": False, "path": str(folder), "reason": str(exc)}
    if not (find_spec("transformers") and find_spec("torch")):
        return {"ready": False, "path": str(folder), "reason": "transformers / torch not installed; the keywords decide alone"}
    return {"ready": True, "path": str(folder), "classifier": "leaf",
            "taxonomy_version": space.get("taxonomy_version"), "leaf_classes": len(space.get("leaf") or [])}


def status() -> dict[str, Any]:
    """For the stage's result and /health."""
    loaded = _load()
    if loaded is None:
        return {"ready": False, "reason": _failed, "path": str(model_dir()),
                "stand_in": "keywords only"}
    return {"ready": True, "path": str(model_dir()), "leaf_classes": len(loaded["space"]["leaf"]),
            "taxonomy_version": loaded["space"].get("taxonomy_version")}
