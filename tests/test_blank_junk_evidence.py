"""Blank / junk confidence from evidence (2026-11-21, user: "basis the comfort and uniqueness",
no one number for every rule): margin, support, OCR match rate and the model's p."""
from modules.blank_junk import evidence


def test_past_cut_both_directions():
    assert evidence.past_cut(0, 1.5, 0) == 1.0          # 0 words of its own: far past the Junk cut
    assert round(evidence.past_cut(1, 1.5, 0), 2) == 0.33
    assert evidence.past_cut(0.98, 0.98, 1.0) == 0.0     # S5 on its cut
    assert evidence.past_cut(7, 1.5, 5) == 1.0


def test_a_clear_rule_the_model_agrees_with_is_high():
    value, parts = evidence.combine(margin=1.0, support={"neighbours_compared": 1.0, "lines_theirs": 1.0},
                                    p_model=0.9, ocr_rate=0.95)
    assert value >= 0.95 and parts["model_p"] == 0.9


def test_a_borderline_rule_against_the_model_is_low():
    value, _ = evidence.combine(margin=0.33, support={"neighbours_compared": 0.5}, p_model=0.2)
    assert 0.2 <= value < 0.55


def test_more_margin_more_support_more_agreement_each_raise_it():
    base = dict(margin=0.5, support={"x": 0.5}, p_model=0.5, ocr_rate=0.5)
    value = evidence.combine(**base)[0]
    assert evidence.combine(**{**base, "margin": 0.9})[0] > value
    assert evidence.combine(**{**base, "support": {"x": 1.0}})[0] > value
    assert evidence.combine(**{**base, "p_model": 0.9})[0] > value
    assert evidence.combine(**{**base, "ocr_rate": 1.0})[0] > value


def test_a_rule_alone_never_outweighs_a_sure_model():
    value, _ = evidence.combine(margin=1.0, support={"x": 1.0}, p_model=0.01)
    assert value < 0.6


def test_the_floor_and_unmeasured_parts():
    value, parts = evidence.combine(margin=None, support=None, p_model=None)
    assert 0.2 <= value <= 1.0
    assert parts["support"] == "not measured" and parts["model_p"] == "no model"
    assert str(parts["margin"]).startswith("not measured")


def test_each_rule_has_its_own_measured_ceiling(monkeypatch):
    """2026-11-22 (user): a rule's ceiling is its measured accuracy on the ground truth once it has
    enough pages; else the shared rule_span."""
    cfg = {**evidence.settings(), "rule_reliability": {"S6": {"pages": 200, "accuracy": 0.97},
                                                        "S2": {"pages": 120, "accuracy": 0.70},
                                                        "SC": {"pages": 5, "accuracy": 1.0}}}
    monkeypatch.setattr(evidence, "settings", lambda: cfg)
    sure = evidence.combine(rule="S6", margin=1.0, support={"x": 1.0})[0]
    weak = evidence.combine(rule="S2", margin=1.0, support={"x": 1.0})[0]
    few = evidence.combine(rule="SC", margin=1.0, support={"x": 1.0})
    assert sure > weak
    assert abs(sure - 0.97) < 0.01 and abs(weak - 0.70) < 0.01
    assert "shared default" in few[1]["rule_span"]


def test_rule_reliability_counts_per_rule(monkeypatch):
    from modules.blank_junk import rule_reliability as rr

    monkeypatch.setattr(rr, "staged_rules", lambda chart: {"p1": "S6", "p2": "S6"})
    rows = [("c", "p1", "junk", "S6 ...", "No", "Yes"), ("c", "p2", "junk", "S6 ...", "No", "No"),
            ("c", "p3", "not_blank_junk", "R5 service keep", "No", "No"), ("c", "p4", "duplicate", "", "No", "No")]
    found = rr.measure(rows)
    assert found["S6"] == {"pages": 2, "right": 1, "accuracy": 0.5}
    assert found["R5"]["accuracy"] == 1.0 and "duplicate" not in found
