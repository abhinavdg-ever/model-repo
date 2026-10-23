"""Page axes for a page: L1, L2, L3, specialty and codeability, each decided
by the page-axes model when it clears its gate and otherwise by the v24
keyword canon (``page_arbitration.json`` → ``axes``).

* **L1, L2:** the model's, at ``route_model`` / ``l2_min`` (0.40); below that
  the canon's best match, at ``canon_min`` (0.50).
* **L3:** always the canon's best match under the decided L1 > L2, at
  ``l3_canon_min`` (0.50). The model's L3 is kept in the raw prediction only.
* **Specialty:** the model's, behind a strict gate; else the canon entry's.
* **Codeability:** always the canon's — the tag of the canon page type behind
  the decided path; needs_review when its rule is context / source (the
  Final step resolves context from the page's document).

The axes are the page's classification: ``page_names`` turns them into
``page_type`` / ``page_subtype`` (L1 / L1, L1 / L2, or "L1 (L2)" / L3), and
``as_json`` is the whole answer for ``page_classification.axes``.

The model is the folder ``train_axes.py`` writes (``encoder/``, ``heads.pt``,
``label_space.json``), in ``PAGE_AXES_MODEL_DIR`` (default
``models/page-family/page-axes/``).

* No folder, or no transformers / torch: the canon decides every level and
  ``route`` is None — a degraded run visible in the data.
* A folder trained on another taxonomy version raises at load, naming both.

``gate`` needs no torch: the model's raw answer (or None) and the page text in,
the decided values out.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Optional

from stages.lib.canon_store import CANON_DIR, CanonFile
from stages.lib.page_classify import keywords

logger = logging.getLogger(__name__)

MODEL_FILES = ("label_space.json", "heads.pt", "encoder/config.json")
NEEDS_REVIEW = "needs_review"
# The L2 of a page whose L1 has no L2 for it (never the L1 repeated).
OTHERS = "Others"
MODEL, CANON = "model", "canon"

_ARBITRATION: CanonFile[dict[str, Any]] = CanonFile(CANON_DIR / "page_arbitration.json")

_lock = threading.Lock()
_loaded: Optional[dict[str, Any]] = None
_failed: Optional[str] = None


class ModelTaxonomyError(RuntimeError):
    """The model folder was trained on another taxonomy version."""


@dataclass(frozen=True)
class _V24:
    keywords: Any  # keywords._Canon
    entries: dict[str, dict[str, Any]]  # canon page type -> its entry


def _build_v24(data: dict[str, Any]) -> _V24:
    return _V24(keywords.build_v24(data), {e["page_type"]: e for e in data["page_types"]})


_CANON_V24: CanonFile[_V24] = CanonFile(CANON_DIR / "page_canon_v24.json", _build_v24)


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
    specialty: Optional[str] = None
    specialty_confidence: Optional[float] = None
    codeability: str = NEEDS_REVIEW
    codeability_confidence: Optional[float] = None
    # Who decided L1 / L2 / L3 / specialty / codeability: model, canon or -.
    sources: tuple[str, ...] = ("-", "-", "-", "-", "-")
    # The canon page type behind the decided path (codeability, specialty,
    # and the fine-grained name encounter type reads).
    canon_page_type: Optional[str] = None
    # Its codeability rule: page_type_tag, or context / source (needs_review
    # until the document — imaging_final — or the ingest source decides).
    codeability_rule: Optional[str] = None
    # The L1 whose canon title term heads the page, if any.
    title_l1: Optional[str] = None
    # Under every other L1 that has L2s: the L2 (and L3) the page would take
    # there -- {L1: {l2, l2_confidence, l2_source, l3, l3_confidence}}. The Final
    # step reads it when the page's document has another L1.
    under_l1: dict[str, Any] = field(default_factory=dict, compare=False)
    prediction: dict[str, Any] = field(default_factory=dict, compare=False)

    def as_json(self) -> dict[str, Any]:
        """The whole answer, for ``page_classification.axes``."""
        return {
            "route": self.route,
            "l1": self.l1, "l1_confidence": self.l1_confidence,
            "l2": self.l2, "l2_confidence": self.l2_confidence,
            "l3": self.l3, "l3_confidence": self.l3_confidence,
            "specialty": self.specialty, "specialty_confidence": self.specialty_confidence,
            "codeability": self.codeability, "codeability_confidence": self.codeability_confidence,
            "sources": "/".join(self.sources),
            "canon_page_type": self.canon_page_type,
            "codeability_rule": self.codeability_rule,
            "title_l1": self.title_l1,
            "under_l1": self.under_l1 or None,
            "prediction": self.prediction or None,
        }

    def csv_columns(self) -> dict[str, Any]:
        """The codeable CSV's axes columns."""
        flat = self.as_json()
        flat.pop("prediction")
        flat.pop("under_l1")
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
    """Every v24 name of a classified page, most specific first: its canon page
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


def v24_names() -> frozenset[str]:
    """Every name a v24 page can carry: canon page types and L1 / L2 / L3."""
    entries = _CANON_V24.get().entries
    levels = {e["path"].get(k) for e in entries.values() for k in ("L1", "L2", "L3")}
    return frozenset(entries) | frozenset(n for n in levels if n)


def l2_names(l1: Optional[str]) -> frozenset[str]:
    """Every L2 the v24 hierarchy places under ``l1`` (none for an L1 without L2s)."""
    entries = _CANON_V24.get().entries.values()
    return frozenset(e["path"]["L2"] for e in entries if e["path"].get("L1") == l1 and e["path"].get("L2"))


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


def canon_match(
    text: str, headings: tuple[str, ...], level: str, l1: Optional[str] = None, l2: Optional[str] = None
) -> tuple[Optional[str], Optional[float], Optional[str]]:
    """The canon's best ``level`` value (L1, L2 or L3) among entries under
    ``l1`` / ``l2``: (value, keyword confidence, canon page type)."""
    from stages.lib.page_classify.arbitration import keyword_confidence

    v24 = _CANON_V24.get()
    names = frozenset(
        name for name, e in v24.entries.items()
        if e["path"].get(level)
        and (l1 is None or e["path"]["L1"] == l1)
        and (l2 is None or e["path"].get("L2") == l2)
    )
    if not names:
        return None, None, None
    group_of = {name: v24.entries[name]["path"][level] for name in names}
    found = keywords.classify(text, headings, canon=v24.keywords, subtypes=names, group_of=group_of)
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

    entries = _CANON_V24.get().entries
    under = {
        name for name, e in entries.items()
        if e["path"]["L1"] == l1
        and (l2 is None or e["path"].get("L2") == l2)
        and (l3 is None or e["path"].get("L3") == l3)
    }
    if not under:
        return None, None
    found = keywords.classify(text, headings, canon=_CANON_V24.get().keywords,
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
    v24 = _CANON_V24.get()
    group_of = {name: e["path"]["L1"] for name, e in v24.entries.items()}
    found = keywords.classify(text, headings, canon=v24.keywords, group_of=group_of)
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

    def by_canon(level: str, minimum: float, l1: Optional[str] = None, l2: Optional[str] = None):
        value, conf, _ = canon_match(text, headings, level, l1, l2)
        if value is None or conf is None or conf < minimum:
            return None, None
        return value, conf

    if where == MODEL:
        l1, l1_conf, sources[0] = raw["L1"], raw["L1_confidence"], MODEL
    else:
        l1, l1_conf = by_canon("L1", s["canon_min"])
        sources[0] = CANON if l1 else "-"

    l2 = l2_conf = None
    if l1 and l1 != s["section_L1"]:
        if sources[0] == MODEL and raw.get("L2") and (raw.get("L2_confidence") or 0.0) >= s["l2_min"]:
            l2, l2_conf, sources[1] = raw["L2"], raw["L2_confidence"], MODEL
        else:
            l2, l2_conf = by_canon("L2", s["canon_min"], l1)
            sources[1] = CANON if l2 else "-"

    l3 = l3_conf = None
    if l2:
        l3, l3_conf = by_canon("L3", s["l3_canon_min"], l1, l2)
        sources[2] = CANON if l3 else "-"

    under = _under_every_l1(raw, text, headings, l1, s, by_canon)

    # The canon page type behind the decided path gives the codeability (always
    # the canon's; the model's is kept in the raw prediction only) and, when
    # the model asserts none, the specialty.
    entry_name, canon_conf = canon_entry(text, headings, l1, l2, l3) if l1 else (None, None)
    entry = _CANON_V24.get().entries.get(entry_name or "") or {}
    specialty, specialty_conf = _model_specialty(raw, space, s) if raw else (None, None)
    if specialty:
        sources[3] = MODEL
    elif entry.get("specialty"):
        specialty, specialty_conf, sources[3] = entry["specialty"], canon_conf, CANON

    rule = entry.get("codeability_rule")
    codeability, codeability_conf = NEEDS_REVIEW, None
    if rule == "page_type_tag" and entry.get("codeability", NEEDS_REVIEW) != NEEDS_REVIEW:
        codeability, codeability_conf, sources[4] = entry["codeability"], canon_conf, CANON

    return AxesResult(
        route=where,
        l1=l1, l1_confidence=_round(l1_conf),
        l2=l2, l2_confidence=_round(l2_conf),
        l3=l3, l3_confidence=_round(l3_conf),
        specialty=specialty, specialty_confidence=_round(specialty_conf),
        codeability=codeability, codeability_confidence=_round(codeability_conf),
        sources=tuple(sources),
        canon_page_type=entry_name,
        codeability_rule=rule,
        title_l1=title_l1(text, headings),
        under_l1=under,
        prediction=_raw_json(raw),
    )


def _under_every_l1(raw: dict[str, Any], text: str, headings: tuple[str, ...], own_l1: Optional[str],
                    s: dict[str, Any], by_canon) -> dict[str, Any]:
    """The L2 / L3 the page would take under each L1 other than its own, by the
    same gates as its own: the model's L2 under that L1 at ``l2_min``, else the
    canon's at ``canon_min``; L3 always the canon's at ``l3_canon_min``. An L1
    whose L2 is the content section (section_L1) is left out."""
    model_by_l1 = raw.get("by_L1") or {}
    l1s = sorted({e["path"]["L1"] for e in _CANON_V24.get().entries.values() if e["path"].get("L2")})
    out: dict[str, Any] = {}
    for l1 in l1s:
        if l1 in (own_l1, s["section_L1"]):
            continue
        model = model_by_l1.get(l1) or {}
        if model.get("L2") and (model.get("L2_confidence") or 0.0) >= s["l2_min"]:
            l2, l2_conf, source = model["L2"], model["L2_confidence"], MODEL
        else:
            (l2, l2_conf), source = by_canon("L2", s["canon_min"], l1), CANON
        if not l2:
            continue
        l3, l3_conf = by_canon("L3", s["l3_canon_min"], l1, l2)
        out[l1] = {"l2": l2, "l2_confidence": _round(l2_conf), "l2_source": source,
                   "l3": l3, "l3_confidence": _round(l3_conf)}
    return out


def _raw_json(raw: dict[str, Any]) -> dict[str, Any]:
    """The model's whole answer, rounded, for the raw prediction."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "by_L1":  # gated into under_l1
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
                "copy the real file there (docs/PAGE_AXES_MODEL.md)")
    return None


def check_taxonomy(space: dict[str, Any], folder: Path) -> None:
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
