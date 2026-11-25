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


def test_tiers_on_the_family():
    assert ag.tier(1, 1, True) == "A"
    assert ag.tier(1, 3, True) == "B"
    assert ag.tier(1, None, True) == "C"
    assert ag.tier(2, 1, True) == "D"
    assert ag.tier(None, 1, True) == "E"     # the model does not rank the family
    assert ag.tier(4, 1, True) == "E"
    assert ag.tier(None, 1, False) == "D"     # no model, keywords' #1 family
    assert ag.tier(None, 2, False) == "E"


def test_family_ranks_and_mass():
    ranks = {"PN > A": 1, "PN > B": 2, "Lab > X": 3, "Rad > Y": 4}
    assert ag.family_ranks(ranks) == {"PN": 1, "Lab": 2, "Rad": 3}
    # 2026-11-25: a leaf's probability is split between siblings; the family's is not.
    assert ag.family_mass({"PN > A": 0.45, "PN > B": 0.40, "Lab > X": 0.15}, "PN") == 0.85


def test_page_type_confidence_orders_the_tiers():
    p = 0.97
    values = [ag.page_type_confidence(t, p, S) for t in ag.TIERS]
    assert values == sorted(values, reverse=True)
    assert values[0] == 0.97 and values[2] <= 0.79 and values[3] <= 0.69 and values[-1] <= 0.49
    # 2026-11-25 (user: no pile on): D and E follow the family probability, not one value each.
    low = [ag.page_type_confidence(t, 0.2, S) for t in ("D", "E")]
    assert low == [0.5, 0.3] and values[3] > low[0] and values[4] > low[1]
    assert ag.page_type_confidence("D", 0.2, S, has_model=False) == 0.55


def test_codability_confidence():
    cod = {"PN > A": "codeable", "PN > B": "codeable", "Lab > X": "non_codeable"}.get
    probs = {"PN > A": 0.5, "PN > B": 0.3, "Lab > X": 0.2}
    # Both firsts codable: the model's mass on codable types.
    assert ag.codability_confidence("codeable", probs, {"PN > A": 1}, {"PN > B": 1}, True, cod, S)[0] == 0.8
    # Keywords' first is a lab page: one first differs, capped Medium.
    value, _ = ag.codability_confidence("codeable", probs, {"PN > A": 1}, {"Lab > X": 1}, True, cod, S)
    assert value == round(min(0.8 * 0.75, 0.79), 4)
    # Neither first is codable.
    assert ag.codability_confidence("codeable", probs, {"Lab > X": 1}, {"Lab > X": 1}, True, cod, S)[0] == 0.49
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
    assert tier(stored([("PN > Other", 1)], [("PN > Third", 1)])) == "A"      # same family: A
    assert tier(stored([("PN", 1)], [("X > Y", 1), (leaf, 2)])) == "B"
    assert tier(stored([(leaf, 1)], [])) == "C"
    assert tier(stored([("Lab > CBC", 1), (leaf, 2)], [(leaf, 1)])) == "D"
    assert tier(stored([("Lab > CBC", 1)], [(leaf, 1)])) == "E"
    assert tier(stored([], [(leaf, 1)], source="none")) == "D"
    assert tier(stored([], [], t="B")) == "B"                                  # no rankings: stored
    assert tier(None) is None and tier({"leaf": "Others"}) is None
