"""Map model probabilities → KEEP / BLANK / JUNK flags.

The winning class is the model's. A junk or blank call below the confidence
floor stays KEEP for review. Keyword lists do not rewrite the flag.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.preprocessing.taxonomy import Flag, to_flag


@dataclass
class DecisionConfig:
    min_flag_confidence: float = 0.70
    junk_blank_min_confidence: float = 0.85
    # junk_blank_min_confidence must be met by one label, not by several junk subtypes
    # together; a page the model cannot place stays KEEP for review.
    require_confident_subtype: bool = True


@dataclass
class PageDecision:
    flag: Flag
    confidence: float
    review_required: bool
    p_keep: float
    p_blank: float
    p_junk: float
    reason: str
    audit_tag: str | None = None
    protocol_evidence: tuple[str, ...] = ()

    @property
    def page_type(self) -> str:
        return self.flag

    @property
    def keep_delete(self) -> str:
        return self.flag


def _bucket_probs(labels: list[str], proba: np.ndarray) -> dict[str, float]:
    buckets = {"KEEP": 0.0, "BLANK": 0.0, "JUNK": 0.0}
    for lab, p in zip(labels, proba):
        try:
            flag = to_flag(lab)
        except KeyError:
            continue
        if flag is None:
            continue
        buckets[flag] += max(0.0, float(p))
    total = sum(buckets.values())
    if total > 1.0 + 1e-6:
        for k in buckets:
            buckets[k] /= total
    for k in buckets:
        buckets[k] = max(0.0, min(1.0, buckets[k]))
    return buckets


def _default_audit(flag: Flag) -> str:
    return {"KEEP": "KEEP", "BLANK": "BLANK", "JUNK": "JUNK"}[flag]


def decide_from_proba(
    proba: np.ndarray,
    labels: list[str],
    *,
    ocr_text: str = "",
    config: DecisionConfig | None = None,
) -> PageDecision:
    del ocr_text, config  # scoring is the class probabilities; floors are not applied
    buckets = _bucket_probs(labels, np.asarray(proba, dtype=float).ravel())
    p_keep, p_blank, p_junk = buckets["KEEP"], buckets["BLANK"], buckets["JUNK"]

    scores = {"KEEP": p_keep, "BLANK": p_blank, "JUNK": p_junk}
    best: Flag = max(scores, key=scores.get)  # type: ignore[assignment]
    confidence = max(0.0, min(1.0, float(scores[best])))
    return PageDecision(
        flag=best,
        confidence=confidence,
        review_required=False,
        p_keep=float(p_keep),
        p_blank=float(p_blank),
        p_junk=float(p_junk),
        reason="argmax",
        audit_tag=_default_audit(best),
        protocol_evidence=(),
    )
