"""Blank / junk confidence from evidence (2026-11-21, user: "basis the comfort and uniqueness" --
no one number for every rule).

A rule's verdict is as sure as three measurements make it:

  1. margin     how far past the rule's own cut the page was (0 = on the cut, 1 = far past it):
                0 words of its own is a clearer Junk than 1; 100% of the words in order on a
                neighbour is clearer than 98%. Each rule measures its own (stage.short_verdict,
                method.decide) and passes it here.
  2. support    how sure the measurement itself is: the uniqueness of the page against its
                neighbours (both compared or one; the share of its lines that are theirs), and
                whether the texts compared came from the same OCR engine. The mean of what was
                measured; 1.0 when nothing was.
     ocr        the page's OCR match rate (routing): words "missing" from a badly read page may
                be misread, so a low rate pulls the rule's side towards 50 / 50.
  3. model      DistilBERT's probability for the verdict's class: agreeing lifts the confidence,
                disagreeing lowers it -- in proportion, instead of a flat backup.

They meet in log-odds:

  rule      = 0.5 + rule_span x margin x support   (on its cut a coin flip; rule_span: the rule's own
                                                  measured accuracy - 0.5 once it has enough
                                                  ground-truth pages, else 0.45: at most 95%)
  logit(c)  = rule_weight x ocr_factor x logit(rule) + model_weight x logit(p_model)
  ocr_factor = ocr_min_factor + (1 - ocr_min_factor) x match rate   (1 when not measured)

then the module calibration (confidence_canon "calibration".blank_junk, fitted on labelled pages,
identity until then) and the floor. The weights live in confidence_canon
modules.blank_junk.evidence. Every part is recorded with the verdict (votes.confidence_parts).
"""
from __future__ import annotations

import math
from typing import Any, Optional

from modules import confidence as conf

DEFAULTS = {"rule_span": 0.45, "rule_weight": 1.0, "model_weight": 1.0, "ocr_min_factor": 0.5, "unmeasured_margin": 0.5,
            "min_measured_pages": 30}
MODULE = "blank_junk"
_EPS = 1e-4


def settings() -> dict[str, Any]:
    return {**DEFAULTS, **(conf.settings_for(MODULE, {}).get("evidence") or {})}


def clip(value: Optional[float]) -> Optional[float]:
    return None if value is None else max(0.0, min(1.0, float(value)))


def _logit(p: float) -> float:
    p = min(1.0 - _EPS, max(_EPS, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def past_cut(value: float, cut: float, far: float) -> float:
    """How far ``value`` is past ``cut`` towards ``far`` (0 on the cut, 1 at or past ``far``);
    either direction (far below cut for a "fewer is surer" measure)."""
    if far == cut:
        return 1.0
    return max(0.0, min(1.0, (value - cut) / (far - cut)))


def model_p(code_class: str, bert: Optional[dict[str, Any]]) -> Optional[float]:
    """DistilBERT's probability for the verdict's class: keep / junk / blank."""
    if not bert or bert.get("p_keep") is None or bert.get("p_junk") is None:
        return None
    probs = {"keep": float(bert["p_keep"]), "junk": float(bert["p_junk"]), "blank": float(bert.get("p_blank") or 0.0)}
    return probs.get(code_class)


def rule_span(rule: Optional[str], cfg: dict[str, Any]) -> tuple[float, str]:
    """The rule's ceiling above a coin flip (2026-11-22, user: not one size for every rule): its
    measured accuracy on the ground truth minus 0.5 (``rule_reliability``, written by
    ``python -m modules.blank_junk.rule_reliability --write``) once it has min_measured_pages
    pages; else the shared rule_span. A rule right half the time or less adds nothing."""
    found = (cfg.get("rule_reliability") or {}).get(rule or "") or {}
    if found.get("accuracy") is not None and int(found.get("pages") or 0) >= int(cfg["min_measured_pages"]):
        span = max(0.0, min(0.49, float(found["accuracy"]) - 0.5))
        return span, f"measured {rule}: {float(found['accuracy']):.0%} right on {int(found['pages'])} pages"
    return float(cfg["rule_span"]), "shared default"


def combine(*, margin: Optional[float], support: Optional[dict[str, Optional[float]]] = None,
            p_model: Optional[float] = None, ocr_rate: Optional[float] = None,
            rule: Optional[str] = None) -> tuple[float, dict[str, Any]]:
    """The confidence and its parts. ``margin`` None = the rule measured none (unmeasured_margin);
    ``support``: named measures in [0, 1], None ones left out; ``rule``: its name, for its own
    measured ceiling (rule_span)."""
    cfg = settings()
    m = clip(margin) if margin is not None else float(cfg["unmeasured_margin"])
    measured = {k: round(float(v), 4) for k, v in (support or {}).items() if v is not None}
    s = sum(measured.values()) / len(measured) if measured else 1.0
    span, span_from = rule_span(rule, cfg)
    rule_value = 0.5 + span * m * s
    rate = clip(ocr_rate)
    lo = float(cfg["ocr_min_factor"])
    ocr_factor = 1.0 if rate is None else lo + (1.0 - lo) * rate
    x = float(cfg["rule_weight"]) * ocr_factor * _logit(rule_value)
    if p_model is not None:
        x += float(cfg["model_weight"]) * _logit(float(p_model))
    raw = _sigmoid(x)
    value = conf.at_least_floor(conf.calibrate(MODULE, raw))
    parts = {
        "margin": round(m, 4) if margin is not None else f"not measured ({m})",
        "support": measured or "not measured",
        "rule": round(rule_value, 4),
        "rule_span": f"{span:.2f} ({span_from})",
        "ocr_match_rate": round(rate, 4) if rate is not None else "not measured",
        "model_p": round(float(p_model), 4) if p_model is not None else "no model",
        "raw": round(raw, 4),
        "confidence": value,
    }
    return float(value), parts
