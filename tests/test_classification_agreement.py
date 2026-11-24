"""Page Type / Codability confidence from the model and keywords' agreement (2026-11-24, user).
Made-up values."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.page_classify import agreement as ag  # noqa: E402

S = ag.settings()


def test_tiers():
    assert ag.tier(1, 1, True, True) == "A"
    assert ag.tier(1, 3, True, True) == "B"
    assert ag.tier(1, None, True, True) == "C"
    assert ag.tier(2, 1, False, True) == "D"
    assert ag.tier(None, 1, True, True) == "D"      # model's #1 in the same L1
    assert ag.tier(None, 1, False, True) == "E"     # another family
    assert ag.tier(None, 1, False, False) == "D"     # no model, keywords' #1
    assert ag.tier(None, 2, False, False) == "E"


def test_page_type_confidence_orders_the_tiers():
    p = 0.97
    values = [ag.page_type_confidence(t, p, S) for t in ag.TIERS]
    assert values == sorted(values, reverse=True)
    assert values[0] == 0.97 and values[2] <= 0.79 and values[-1] == 0.3


def test_codability_confidence():
    cod = {"PN > A": "codeable", "PN > B": "codeable", "Lab > X": "non_codeable"}.get
    probs = {"PN > A": 0.5, "PN > B": 0.3, "Lab > X": 0.2}
    # Both firsts codable: the model's mass on codable types.
    assert ag.codability_confidence("codeable", probs, {"PN > A": 1}, {"PN > B": 1}, True, cod, S)[0] == 0.8
    # Keywords' first is a lab page: one first differs, capped Medium.
    value, _ = ag.codability_confidence("codeable", probs, {"PN > A": 1}, {"Lab > X": 1}, True, cod, S)
    assert value == round(min(0.8 * 0.75, 0.79), 4)
    # Neither first is codable.
    assert ag.codability_confidence("codeable", probs, {"Lab > X": 1}, {"Lab > X": 1}, True, cod, S)[0] == 0.3
    assert ag.codability_confidence(None, probs, {}, {}, True, cod, S)[0] is None


def test_review_ui_reads_the_same_tier_from_stored_rankings():
    sys.path.insert(0, str(REPO / "review-ui" / "backend"))
    from app.services.classification_agreement import tier

    leaf = "PN > Office Visit"
    def stored(model_top, keyword_top, source="leaf_model", t=None):
        return {"tier": t, "leaf": leaf, "model_source": source,
                "model_top": [{"path": p, "rank": r} for p, r in model_top],
                "keyword_top": [{"path": p, "rank": r} for p, r in keyword_top]}
    assert tier(stored([(leaf, 1)], [(leaf, 1)])) == "A"
    assert tier(stored([("PN", 1)], [("X > Y", 1), (leaf, 2)])) == "B"      # a node above the leaf
    assert tier(stored([(leaf, 1)], [])) == "C"
    assert tier(stored([("PN > Other", 1), (leaf, 2)], [(leaf, 1)])) == "D"
    assert tier(stored([("Lab > CBC", 1)], [(leaf, 1)])) == "E"
    assert tier(stored([], [(leaf, 1)], source="none")) == "D"
    assert tier(stored([("Lab > CBC", 1)], [], t="B")) == "B"               # the stored tier wins
    assert tier(None) is None and tier({"leaf": "Others"}) is None
