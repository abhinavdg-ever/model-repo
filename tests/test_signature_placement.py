"""A provider signature counts only on an encounter's first / last page or in the page's top /
bottom band (2026-11-24, user). Made-up values."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

pytest.importorskip("psycopg", reason="imaging_final imports the db layer")
from modules.imaging_final.stage import placed_signatures  # noqa: E402


def _sig(name):
    return {"signature_present": True, "provider_name": name, "provider_credentials": "MD", "signers": None,
            "provider_version": 1, "provider_confidence": 0.9}


def test_middle_page_mid_signature_dropped_edges_kept():
    sigs = {1: _sig("A"), 2: _sig("B"), 3: _sig("C"), 4: _sig("D"), 5: _sig("E"), 6: _sig("F")}
    rows = {1: {"region": ""}, 2: {"region": ""}, 3: {"region": "footer"}, 4: {"region": "role_header"},
            5: {"region": ""}, 6: {"region": ""}}
    document_of = {pid: [1, 2, 3, 4, 5] for pid in (1, 2, 3, 4, 5)} | {6: [6]}
    out = placed_signatures(sigs, rows, document_of)
    assert out[1]["provider_name"] == "A" and out[5]["provider_name"] == "E"   # first / last page
    assert out[2]["provider_name"] is None and out[2]["signature_present"] is False  # middle, mid-page
    assert out[3]["provider_name"] == "C" and out[4]["provider_name"] == "D"   # bottom / top band
    assert out[6]["provider_name"] == "F"                                     # a one-page encounter
    assert sigs[2]["provider_name"] == "B"                                    # input untouched
