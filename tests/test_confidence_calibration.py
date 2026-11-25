"""Page Type / Codability confidence calibration (2026-11-24, user: a higher band must be more
accurate; fewer High is fine, High must be right). Made-up values."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review-ui" / "backend"))


def test_knots_are_checked():
    from app.services.confidence_calibration import valid_knots

    assert valid_knots([[0.5, 0.4], [0.9, 0.85]]) == [[0.5, 0.4], [0.9, 0.85]]
    assert valid_knots([[0.5, 0.9], [0.9, 0.6]]) is None      # accuracy falls
    assert valid_knots([[0.9, 0.4], [0.5, 0.6]]) is None      # raw not increasing
    assert valid_knots([[0.5, 1.2], [0.9, 0.9]]) is None      # out of 0-1
    assert valid_knots([[0.5, 0.5]]) is None                   # one knot


def test_apply():
    from app.services.confidence_calibration import apply

    knots = [[0.6, 0.5], [0.9, 0.7], [0.99, 0.92]]
    assert apply(knots, 0.3) == 0.5 and apply(knots, 1.0) == 0.92
    assert apply(knots, 0.75) == 0.6
    assert apply(None, 0.75) == 0.75 and apply(knots, None) is None


def test_history_twin_applies_the_curve():
    from app.services.chart_confidence import page_level

    # Raw page type 0.95 reads High; its curve says pages like it were right 60% of the time.
    conf = {"page_type": 0.95, "dos": 0.9, "member_name": 0.9, "member_dob": 0.9}
    assert page_level(conf, False)[0] == "high"
    calibrated = page_level(conf, False, calibration={"page_type": [[0.5, 0.3], [0.99, 0.6]]})
    assert calibrated[1] < page_level(conf, False)[1]


def test_a_discharge_summary_codability_is_low_at_most():
    """2026-11-25 (user): codability on a Discharge Summary page reads Low, whatever the model."""
    from app.services import chart_confidence as cc

    conf = {"codability": 0.97, "page_type": 0.97}
    ds = cc.page_level(conf, False, None, "Discharge Summary (Inpatient)")
    pn = cc.page_level(conf, False, None, "Progress Note")
    assert cc.is_discharge_summary("Discharge Summary (Inpatient)")
    assert ds[1] < pn[1]  # one Low field now: the level score drops
