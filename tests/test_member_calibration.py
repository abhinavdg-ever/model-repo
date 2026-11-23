"""Member Name / DOB confidence calibration (2026-11-23): sides as the Accuracy View reads them, the
fit per source, applying it, and the raw values kept in the Final confidences. Made-up values."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.member import calibration as cal  # noqa: E402

MANIFEST = {"member_name": "Made Upname", "first_name": "Made", "last_name": "Upname", "middle_name": None,
            "member_dob": "1950-02-03"}


def _row(**kw):
    base = {"gt_name": "Yes", "gt_dob": "Yes", "final_name": "Made Upname", "final_dob": "1950-02-03",
            "confidences": {"member_name": 0.9, "member_dob": 0.9}, "key_source_name": "Patient Name",
            "key_source_dob": "DOB", "blank_junk_flag": None, "duplicate_of_page_id": None,
            **{f"manifest_{k}": v for k, v in MANIFEST.items()}}
    return {**base, **kw}


class TestSides:
    def test_truth_side(self):
        assert [cal.truth_side(v) for v in ("Yes", "no", "", None, "Skipped", "Not Found")] == \
            ["yes", "no", "not_found", "not_found", "not_found", "not_found"]

    def test_pipeline_side(self):
        assert cal.pipeline_side("dob", "02/03/1950", MANIFEST) == "yes"
        assert cal.pipeline_side("dob", "1951-02-03", MANIFEST) == "no"
        assert cal.pipeline_side("dob", "", MANIFEST) == "not_found"
        assert cal.pipeline_side("name", "Upname, Made", MANIFEST) == "yes"
        assert cal.pipeline_side("name", "Other Person", MANIFEST) == "no"


class TestSamples:
    def test_sources_inferred_for_older_rows(self):
        assert cal.raw_and_source("name", _row()) == (0.9, "kv_row")
        assert cal.raw_and_source("name", _row(key_source_name=""))[1] == "whole_page"
        assert cal.raw_and_source("dob", _row(final_dob=None, key_source_dob="dropped: far"))[1] == "dropped"
        assert cal.raw_and_source("dob", _row(final_dob=None, key_source_dob=""))[1] == "not_found"

    def test_stored_raw_wins(self):
        row = _row(confidences={"member_name": 0.99, "member_raw": {"member_name": {"raw": 0.6, "source": "whole_page"}}})
        assert cal.raw_and_source("name", row) == (0.6, "whole_page")

    def test_a_missed_dob_the_truth_has_is_wrong(self):
        out = cal.samples([_row(final_dob=None, key_source_dob="", confidences={"member_name": 0.9})])
        assert ("dob", "not_found", None, False) in out and ("name", "kv_row", 0.9, True) in out

    def test_blank_junk_and_duplicates_are_not_scored(self):
        assert cal.samples([_row(blank_junk_flag="junk"), _row(gt_is_duplicate="Yes"), _row(gt_blank_page="Yes")]) == []


class TestFit:
    def test_constant_sources_and_minimum_pages(self):
        data = [("name", "whole_page", 0.6, i < 48) for i in range(50)] + [("dob", "dropped", 0.6, True)] * 5
        calibration, table = cal.fit(data)
        assert calibration["name"]["whole_page"] == round(49 / 52, 4)
        assert "dob" not in calibration and table["dob/dropped"]["pages"] == 5

    def test_isotonic_knots_rise(self):
        # High raw scores wrong more often than mid ones: pooled, never decreasing.
        pts = [(0.5 + 0.005 * i, i % 2 == 0) for i in range(50)] + [(0.97 + 0.0005 * i, i % 3 == 0) for i in range(50)]
        knots = cal.isotonic_knots(pts)
        ys = [y for _, y in knots]
        assert ys == sorted(ys) and len(knots) >= 1

    def test_apply(self, monkeypatch):
        monkeypatch.setattr(cal, "settings", lambda: {"name": {"whole_page": 0.95, "kv_row": {"knots": [[0.5, 0.6], [0.9, 0.7]]}},
                                                     "dob": {"not_found": 0.4}})
        assert cal.apply("name", "whole_page", 0.6) == 0.95
        assert cal.apply("name", "kv_row", 0.7) == 0.65
        assert cal.apply("name", "kv_row", 0.99) == 0.7
        assert cal.apply("dob", "not_found", None) == 0.4
        assert cal.apply("dob", "kv_row", 0.8) == 0.8  # nothing fitted: unchanged
        assert cal.apply("name", "not_found", None) is None


class TestFinal:
    def test_raw_kept_and_not_a_review_field(self):
        pytest.importorskip("psycopg", reason="imaging_final imports the db layer")
        from modules.imaging_final.stage import final_confidences, review_fields

        out = final_confidences(
            1, page_type=None, decider=1, type_pages=[1], specialty_page=None, dos_page=None, encounter_page=1,
            classification={}, dos={}, encounters={}, members={}, provider_confidence=None, blank_junk={}, quality={},
            member_fields={"name": 0.95, "dob": 0.3, "member_id": None},
            member_raw={"name": (0.6, "whole_page"), "dob": (None, "not_found"), "member_id": (None, "not_found")})
        assert out["member_raw"] == {"member_name": {"raw": 0.6, "source": "whole_page"},
                                     "member_dob": {"raw": None, "source": "not_found"}}
        assert review_fields(out) == ["member_dob"]
