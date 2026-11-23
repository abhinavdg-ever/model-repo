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
    # 2026-11-22 (user): like an NPS over the features -- (High - Low) / features, % (1 + score) / 2.
    assert page_level({**high, "member_dob": 0.3}, False)[:2] == ("medium", 0.6667)  # 2 High, 1 Low
    assert page_level({**high, "provider": 0.3}, False)[:2] == ("medium", 0.75)  # 3 High, 1 Low
    assert page_level({**high, "provider": 0.6}, False)[0] == "high"  # 3 High, 1 Medium: 87.5%
    assert page_level({**high, "provider": 0.3, "orientation": 0.2}, False)[:2] == ("medium", 0.6)
    assert page_level({"dos": 0.3, "page_type": 0.2, "provider": 0.9}, False)[0] == "low"  # more Low than High
    # Member ID is not a feature.
    assert page_level({**high, "member_id": 0.1}, False)[0] == "high"
    # Blank / junk page: only Blank / Junk and Codability count.
    assert page_level({"dos": 0.1, "blank_junk": 0.9}, True)[0] == "high"
    assert page_level({}, False) is None
    assert page_level('{"dos": 0.9}', False)[0] == "high"


def test_chart_level():
    """2026-11-23 (user): weighted page levels (High 1.0, Medium 0.6, Low 0; critical pages x2), then
    guardrails: Low under 50%, over 15% Low pages or a critical page Low; Medium under 80%, any Low
    page or over 30% Medium; else High. The spec's three 100-page examples."""
    from app.services.chart_confidence import chart_level

    H, M, L = ("high", 0.9, False), ("medium", 0.6, False), ("low", 0.3, False)
    out = chart_level([H] * 75 + [M] * 25)
    assert (out["level"], out["value"]) == ("high", 0.9)
    out = chart_level([H] * 82 + [M] * 12 + [L] * 6)
    assert (out["level"], out["value"]) == ("medium", 0.892) and "Low" in out["reason"]
    out = chart_level([H] * 40 + [M] * 15 + [L] * 45)
    assert (out["level"], out["value"]) == ("low", 0.49)
    # More than 30% Medium caps at Medium even with a high score; one critical Low page is Low.
    assert chart_level([H] * 65 + [M] * 35)["level"] == "medium"
    out = chart_level([H] * 99 + [("low", 0.3, True)])
    assert out["level"] == "low" and "critical" in out["reason"]
    # A critical page counts twice in the score.
    assert chart_level([("high", 0.9, True), M])["value"] == round((2 * 1.0 + 0.6) / 3, 4)
    assert chart_level([L] * 4)["value"] == 0.2  # the floor
    out = chart_level([None, M])
    assert out["level"] == "medium" and out["counts"] == {"high": 0, "medium": 1, "low": 0}
    assert chart_level([])["level"] is None


def test_critical_page_types():
    from app.services.chart_confidence import is_critical, page_level

    assert is_critical("Discharge Summary") and is_critical("Operative Note (Surgery Report)")
    assert not is_critical("Progress Note") and not is_critical(None)
    assert page_level({"dos": 0.9}, False, None, "Discharge Summary")[2] is True


def test_rerun_source_cuts_the_chart_folder():
    from app.services.playground import rerun_source

    assert rerun_source("c1", ("box", "Raw_Input/Run1/Batch1/DEID_PNGs", "Processed/Run1/Batch1/c1")) == (
        "box", "Raw_Input/Run1/Batch1/DEID_PNGs", "Processed/Run1/Batch1")
    assert rerun_source("c1", ("box", "Raw_Input/R/B/DEID_PNGs/c1/", None)) == ("box", "Raw_Input/R/B/DEID_PNGs", "")
    assert rerun_source("c1", (None, "Raw/x", None), "default") == ("default", "Raw/x", "")
    assert rerun_source("c1", ("box", None, None)) is None
    assert rerun_source("c1", None) is None


def test_the_page_percent_is_the_nps_score():
    """2026-11-22 (user): a page's % is (1 + (High - Low) / features) / 2, as the chart's over its
    pages; its level is that % banded."""
    from app.services.chart_confidence import chart_level, page_level

    page = {"member_name": 1.0, "member_dob": 1.0, "dos": 0.2, "page_type": 1.0, "codability": 1.0,
            "provider": 1.0, "orientation": 1.0}
    # Member Name + DOB are one feature (Member Verification): six features.
    assert page_level(page, False)[:2] == ("high", 0.8333)  # 5 High, 1 Low
    assert page_level({**page, "dos": 1.0, "provider": 0.6}, False)[:2] == ("high", 0.9167)  # 5 High, 1 Medium
    # The chart reads its pages' levels, not their %: 7 High + 3 Medium -> score 0.7 -> 85%.
    # The chart reads its pages' levels: 7 High + 3 Medium -> (7 + 1.8) / 10 = 88%, Medium (over 30% is not
    # reached: 30% exactly) -> High.
    assert chart_level([("high", 1.0, False)] * 7 + [("medium", 0.79, False)] * 3)["value"] == 0.88


def test_orientation_falls_back_to_the_measured_confidence():
    """2026-11-19 (user): Orientation's confidence is stage 1's measured one when the Final
    confidences lack it (as the frontend)."""
    from app.services.chart_confidence import page_level

    assert page_level({"dos": 0.9}, False, {"orientation": 0.3})[:2] == ("medium", 0.5)  # 1 High, 1 Low
    assert page_level({"dos": 0.9}, False, '{"orientation": 0.95}')[0] == "high"
