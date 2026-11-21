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
