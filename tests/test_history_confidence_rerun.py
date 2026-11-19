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
    # One Low non-key feature: Medium; two: Low.
    assert page_level({**high, "provider": 0.3}, False)[0] == "medium"
    assert page_level({**high, "provider": 0.3, "orientation": 0.2}, False)[0] == "low"
    # Member ID is not a feature.
    assert page_level({**high, "member_id": 0.1}, False)[0] == "high"
    # Blank / junk page: only Blank / Junk and Codability count.
    assert page_level({"dos": 0.1, "blank_junk": 0.9}, True)[0] == "high"
    assert page_level({}, False) is None
    assert page_level('{"dos": 0.9}', False)[0] == "high"


def test_chart_level():
    from app.services.chart_confidence import chart_level

    assert chart_level([("high", 0.9)] * 3)["level"] == "high"
    assert chart_level([("high", 0.9)] * 9 + [("low", 0.3)])["level"] == "low"
    assert chart_level([("high", 0.9)] * 19 + [("low", 0.3)])["level"] == "medium"
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
