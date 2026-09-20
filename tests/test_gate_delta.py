"""Unit tests for gate-delta (quality-aware skip_ocr reopen rules)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from stages.gate_delta import (
    GateSignature,
    PageOcrPresence,
    STAGE_BJ1,
    STAGE_BJ2,
    STAGE_FINAL1,
    STAGE_FINAL2,
    STAGE_HEADERS,
    STAGE_PRELIM,
    compute_gate_delta,
    gate_signature_from_row,
    orientation_bucket,
    plan_page_delta,
)


def _sig(**kwargs) -> GateSignature:
    defaults = dict(
        hw_class="printed",
        quality_tag="high",
        rotation_applied=False,
        orientation_bucket=0,
    )
    defaults.update(kwargs)
    return GateSignature(**defaults)


def _stages(actions) -> set:
    out = set()
    for stage_list, _reason in actions:
        out.update(stage_list)
    return out


def test_orientation_bucket_nearest_90():
    assert orientation_bucket(0) == 0
    assert orientation_bucket(44) == 0
    assert orientation_bucket(45) == 90
    assert orientation_bucket(180) == 180
    assert orientation_bucket(270) == 270
    assert orientation_bucket(None) == 0


def test_signature_from_row():
    sig = gate_signature_from_row(
        {
            "printed_or_handwritten": "Handwritten",
            "quality_tag": "LOW",
            "rotation_applied": True,
            "orientation_angle": 90,
        }
    )
    assert sig is not None
    assert sig.hw_class == "non_printed"
    assert sig.quality_tag == "low"
    assert sig.rotation_applied is True
    assert sig.orientation_bucket == 90
    assert sig.skips_bj_pass1 is True
    assert sig.needs_final2 is True


def test_no_change_no_actions_when_artifacts_ok():
    old = _sig()
    new = _sig()
    presence = PageOcrPresence(has_prelim=True, has_final1=True, has_final2=False)
    # high printed → final2 not required
    assert plan_page_delta(old, new, presence) == []


def test_unchanged_but_needs_final2_when_missing():
    old = _sig(quality_tag="medium")
    new = _sig(quality_tag="medium")
    presence = PageOcrPresence(has_prelim=True, has_final1=True, has_final2=False)
    stages = _stages(plan_page_delta(old, new, presence))
    assert STAGE_FINAL2 in stages
    assert STAGE_BJ2 in stages


def test_high_to_medium_forces_final2_when_missing():
    old = _sig(quality_tag="high")
    new = _sig(quality_tag="medium")
    presence = PageOcrPresence(has_prelim=True, has_final1=True, has_final2=False)
    stages = _stages(plan_page_delta(old, new, presence))
    assert STAGE_FINAL2 in stages
    assert STAGE_BJ1 in stages
    assert STAGE_BJ2 in stages


def test_high_to_medium_with_final2_present_no_rebill():
    old = _sig(quality_tag="high")
    new = _sig(quality_tag="medium")
    presence = PageOcrPresence(has_prelim=True, has_final1=True, has_final2=True)
    stages = _stages(plan_page_delta(old, new, presence))
    assert STAGE_FINAL2 not in stages
    assert STAGE_BJ2 in stages


def test_medium_to_high_no_final2_call():
    old = _sig(quality_tag="medium")
    new = _sig(quality_tag="high")
    presence = PageOcrPresence(has_prelim=True, has_final1=True, has_final2=True)
    stages = _stages(plan_page_delta(old, new, presence))
    assert STAGE_FINAL2 not in stages
    assert STAGE_BJ2 in stages


def test_printed_to_handwritten():
    old = _sig(hw_class="printed", quality_tag="high")
    new = _sig(hw_class="non_printed", quality_tag="low")
    presence = PageOcrPresence(has_prelim=True, has_final1=True, has_final2=False)
    stages = _stages(plan_page_delta(old, new, presence))
    assert STAGE_BJ1 in stages
    assert STAGE_FINAL2 in stages


def test_rotation_change_reopens_ocr():
    old = _sig(orientation_bucket=0, rotation_applied=False)
    new = _sig(orientation_bucket=90, rotation_applied=True)
    presence = PageOcrPresence(has_prelim=True, has_final1=True, has_final2=False)
    stages = _stages(plan_page_delta(old, new, presence))
    assert STAGE_PRELIM in stages
    assert STAGE_FINAL1 in stages
    assert STAGE_BJ1 in stages


def test_first_quality_only_fills_missing_artifacts():
    new = _sig(quality_tag="medium")
    presence = PageOcrPresence(has_prelim=True, has_final1=True, has_final2=False)
    stages = _stages(plan_page_delta(None, new, presence))
    assert STAGE_FINAL2 in stages
    assert STAGE_PRELIM not in stages


def test_compute_gate_delta_aggregates():
    old = {1: _sig(quality_tag="high"), 2: _sig(quality_tag="high")}
    new = {1: _sig(quality_tag="medium"), 2: _sig(quality_tag="high")}
    presence = {
        1: PageOcrPresence(True, True, False),
        2: PageOcrPresence(True, True, False),
    }
    plan = compute_gate_delta(old, new, presence, [1, 2])
    assert 1 in plan.force_final2
    assert 2 not in plan.force_final2
    assert STAGE_FINAL2 in plan.invalidate
    assert 1 in plan.invalidate[STAGE_FINAL2]
