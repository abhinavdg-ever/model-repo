"""2026-11-28 (user): the pipeline's Discharge against a ground-truth Codable is correct."""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "review-ui" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.excel_compare.normalizer import normalize_codable  # noqa: E402


def test_discharge_reads_as_codable():
    assert normalize_codable("Discharge") == normalize_codable("Codable") == "codable"
    assert normalize_codable("Discharge") != normalize_codable("Non-Codable")


def test_frontend_rule_is_in_place():
    src = (Path(__file__).resolve().parents[1] / "review-ui" / "frontend" / "src" / "accuracy.ts").read_text(encoding="utf-8")
    assert "pipelineDischarge\n    ? (gtSaysCodable ? \"yes\" : null)" in src
