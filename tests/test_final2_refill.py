"""2026-11-28 (user): on a skip_ocr re-run, a page Doc Intelligence never really read is sent again."""
from __future__ import annotations

import json
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.ocr.reuse import final2_done, final2_pages_to_fill  # noqa: E402


def test_empty_wrapper_is_owed_doc_intel():
    empty = json.dumps({"pageNumber": 1, "content": "", "pagesMeta": []})          # Azure off / no image
    read = json.dumps({"pageNumber": 2, "content": "Progress note ...", "sent": True})
    blank = json.dumps({"pageNumber": 3, "content": "", "sent": True})              # Azure found it empty
    old_text = json.dumps({"pageNumber": 4, "content": "Assessment and plan"})       # older row, no "sent"
    stored = {1: empty, 2: read, 3: blank, 4: old_text, 5: None}
    assert not final2_done(empty) and final2_done(read) and final2_done(blank) and final2_done(old_text)
    assert final2_pages_to_fill(stored, [1, 2, 3, 4, 5, 6], settled={6}) == [1, 5]
