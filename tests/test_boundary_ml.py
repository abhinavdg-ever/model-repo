"""Briefs 1 and 2 (2026-11-02): the clue code the encounter-end and page-order models train on
lives in the pipeline (continuity/ml, sequencing/ml) and computes the documented clues."""
from stages.lib.continuity.ml.features import page_feats, pair_feats
from stages.lib.sequencing.ml.features import opage, order_feats
from stages.lib.sequencing.ml.train import best_path


def _rec(text, l1="Progress Note"):
    return {"text": text, "L1": l1, "L2": "", "specialty": ""}


def test_encounter_clues():
    a = _rec("Office Visit 3/4/2025 Provider: X\nHPI: cough\nPage 1 of 2")
    b = _rec("Page 2 of 2\nPlan: rest\nElectronically signed by X MD")
    f = pair_feats(a, b, page_feats(a), page_feats(b))
    assert f["pageof_seq"] == 1 and f["same_L1"] == 1
    c = _rec("Office Visit 4/9/2025 Provider: Y\nHPI: rash")
    g = pair_feats(b, c, page_feats(b), page_feats(c))
    assert g["sig_tail_a"] == 1 and g["enc_head_b"] == 1


def test_order_clues_and_decoder():
    a, b = _rec("Page 3 of 7\nthe patient denies chest"), _rec("Page 4 of 7\npain or shortness of breath.")
    f = order_feats(a, b, opage(a), opage(b))
    assert f["pg_next"] == 1 and f["pg_delta"] == 1
    S = [[0, -0.1, -5], [-5, 0, -0.1], [-0.1, -5, 0]]
    assert best_path(3, S) in ([0, 1, 2], [1, 2, 0], [2, 0, 1])
