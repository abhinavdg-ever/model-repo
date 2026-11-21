"""History (2026-11-19, user): chart confidence on the server (the confidenceLevel.ts rule) and
the admin Rerun's source / output folders."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review-ui" / "backend"))


def test_page_level_uses_the_seven_features():
    from app.services.chart_confidence import page_level

    high = {"member_name": 0.9, "member_dob": 0.95, "dos": 0.9, "page_type": 0.85}
    assert page_level(high, False)[0] == "high"
    # Member Verification = the lower of name and DOB: a Low DOB is a Low key feature.
    assert page_level({**high, "member_dob": 0.3}, False)[0] == "low"
    # Any Low feature: Low (2026-11-19, user: every feature matters); a Medium one: Medium.
    assert page_level({**high, "provider": 0.3}, False)[0] == "low"
    assert page_level({**high, "provider": 0.6}, False)[0] == "medium"
    assert page_level({**high, "provider": 0.3, "orientation": 0.2}, False)[0] == "low"
    # Member ID is not a feature.
    assert page_level({**high, "member_id": 0.1}, False)[0] == "high"
    # Blank / junk page: only Blank / Junk and Codability count.
    assert page_level({"dos": 0.1, "blank_junk": 0.9}, True)[0] == "high"
    assert page_level({}, False) is None
    assert page_level('{"dos": 0.9}', False)[0] == "high"


def test_chart_level():
    from app.services.chart_confidence import chart_level

    # 2026-11-21 (user, NPS-like): score (High - Low) / pages, value (1 + score) / 2, banded.
    assert chart_level([("high", 0.9)] * 3) == {"level": "high", "value": 1.0, "nps": 1.0,
                                                 "counts": {"high": 3, "medium": 0, "low": 0}}
    assert chart_level([("high", 0.9)] * 17 + [("medium", 0.6)] * 2 + [("low", 0.3)])["value"] == 0.9
    assert chart_level([("high", 0.9)] * 10 + [("medium", 0.6)] * 8 + [("low", 0.3)] * 2)["level"] == "medium"
    out = chart_level([("high", 0.9)] * 2 + [("medium", 0.6)] * 3 + [("low", 0.3)] * 5)
    assert (out["level"], out["value"], out["nps"]) == ("low", 0.35, -0.3)
    assert chart_level([("low", 0.3)] * 4)["value"] == 0.2  # the floor
    out = chart_level([None, ("medium", 0.6)])
    assert out["level"] == "medium" and out["counts"] == {"high": 0, "medium": 1, "low": 0}
    assert chart_level([])["level"] is None


def test_rerun_source_cuts_the_chart_folder():
    from app.services.playground import rerun_source

    assert rerun_source("c1", ("box", "Raw_Input/Run1/Batch1/DEID_PNGs", "Processed/Run1/Batch1/c1")) == (
        "box", "Raw_Input/Run1/Batch1/DEID_PNGs", "Processed/Run1/Batch1")
    assert rerun_source("c1", ("box", "Raw_Input/R/B/DEID_PNGs/c1/", None)) == ("box", "Raw_Input/R/B/DEID_PNGs", "")
    assert rerun_source("c1", (None, "Raw/x", None), "default") == ("default", "Raw/x", "")
    assert rerun_source("c1", ("box", None, None)) is None
    assert rerun_source("c1", None) is None


def test_the_percent_is_capped_by_the_level():
    """2026-11-19 (user): all fields 100% but one Low: the page reads Low, so its % is at most 49%;
    a Medium page or chart at most 79%."""
    from app.services.chart_confidence import chart_level, page_level

    page = {"member_name": 1.0, "member_dob": 1.0, "dos": 0.2, "page_type": 1.0, "codability": 1.0,
            "provider": 1.0, "orientation": 1.0}
    assert page_level(page, False) == ("low", 0.49)
    assert page_level({**page, "dos": 1.0, "provider": 0.6}, False) == ("medium", 0.79)
    # The chart reads its pages' levels, not their %: 7 High + 3 Medium -> score 0.7 -> 85%.
    assert chart_level([("high", 1.0)] * 7 + [("medium", 0.79)] * 3)["value"] == 0.85


def test_orientation_falls_back_to_the_measured_confidence():
    """2026-11-19 (user): Orientation's confidence is stage 1's measured one when the Final
    confidences lack it (as the frontend)."""
    from app.services.chart_confidence import page_level

    assert page_level({"dos": 0.9}, False, {"orientation": 0.3})[0] == "low"
    assert page_level({"dos": 0.9}, False, '{"orientation": 0.95}')[0] == "high"
