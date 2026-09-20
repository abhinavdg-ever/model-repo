"""Standalone section_headers stage: re-derive from Final1/Final2 JSON."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def test_refresh_from_candidates_picks_up_canon_change(tmp_path, monkeypatch):
    from stages.lib.imaging import section_header_match as shm
    from stages.lib.imaging.section_headers_io import refresh_page_headers

    canon = tmp_path / "section_header_canon.json"
    canon.write_text(json.dumps(["Chief Complaint"]), encoding="utf-8")
    monkeypatch.setattr(shm, "_CANON_PATH", canon)
    shm._catalog_mtime = None
    shm._canonical_headers = None
    shm._canonical_norms = None
    shm._catalog_embeddings = None

    page = {
        "fileName": "1.jpg",
        "content": "…",
        "section_header_candidates": [
            {"text": "Chief Complaint", "level": 2},
            {"text": "Soap Bubbles", "level": 2},
            {"text": "SOAP Note", "level": 2},
        ],
        "section_headers": [],
    }
    out = refresh_page_headers(page, kind="final1", use_minilm=False)
    texts = {h["text"] for h in out["section_headers"]}
    assert texts == {"Chief Complaint"}

    canon.write_text(json.dumps(["Chief Complaint", "SOAP Note"]), encoding="utf-8")
    # Force mtime bump for catalog reload.
    import os
    import time

    os.utime(canon, (time.time() + 2, time.time() + 2))
    out2 = refresh_page_headers(out, kind="final1", use_minilm=False)
    texts2 = {h["text"] for h in out2["section_headers"]}
    assert texts2 == {"Chief Complaint", "SOAP Note"}


def test_final2_rebuilds_candidates_from_pages_meta():
    from stages.lib.imaging.section_headers_io import candidates_from_page

    page = {
        "fileName": "2.jpg",
        "content": "Chief Complaint\nnoise",
        "pagesMeta": [
            {
                "width": 1000.0,
                "height": 2000.0,
                "unit": "pixel",
                "lines": [
                    {
                        "content": "Chief Complaint",
                        "polygon": [10, 20, 210, 20, 210, 50, 10, 50],
                    },
                    {
                        "content": "random body text here",
                        "polygon": [10, 100, 400, 100, 400, 130, 10, 130],
                    },
                ],
            }
        ],
    }
    cands = candidates_from_page(page, kind="final2")
    assert len(cands) == 2
    assert cands[0]["text"] == "Chief Complaint"


def test_refresh_ocr_json_doc_rewrites_headers():
    from stages.lib.imaging.section_headers_io import refresh_ocr_json_doc

    doc = {
        "recordId": "chart",
        "model": "prebuilt-read",
        "pages": [
            {
                "fileName": "1.jpg",
                "section_header_candidates": [
                    {"text": "History and Physical", "level": 1},
                    {"text": "lorem ipsum dolor", "level": 2},
                ],
            }
        ],
    }
    new_doc, n_pages, n_headers = refresh_ocr_json_doc(
        doc, kind="final2", use_minilm=False
    )
    assert n_pages == 1
    assert n_headers >= 1
    kept = new_doc["pages"][0]["section_headers"]
    assert any(h["text"] == "History and Physical" for h in kept)
    assert all("lorem" not in h["text"].casefold() for h in kept)


def test_stage_in_chain():
    from orchestrator.runner import STAGE_CHAIN, resolve_stage

    assert resolve_stage("section_headers") >= 0
    names = [n for n, _p, _ in STAGE_CHAIN]
    assert names.index("ocr_final2") < names.index("section_headers")
    assert names.index("section_headers") < names.index("blank_junk") or (
        # blank_junk appears twice; pass 2 is after section_headers
        STAGE_CHAIN[resolve_stage("blank_junk:2")][0] == "blank_junk"
    )
    assert resolve_stage("section_headers") < resolve_stage("blank_junk:2")
