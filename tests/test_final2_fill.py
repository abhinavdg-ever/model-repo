"""2026-11-06 (user): a skip_ocr re-run sends to Azure the pages an earlier run skipped
(high quality / pass-1 junk) that the rules no longer skip."""
from pathlib import Path

from modules.ocr.reuse import final2_pages_to_fill

ROOT = Path(__file__).resolve().parents[1]


def test_pages_without_azure_text_and_not_settled_are_owed_final2():
    stored = {1: '{"content": "text"}', 2: "", 3: None}
    assert final2_pages_to_fill(stored, [1, 2, 3, 4, 5], settled={5}) == [2, 3, 4]


def test_the_runner_reopens_final2_under_skip_ocr():
    source = (ROOT / "core-pipeline" / "apps" / "worker" / "runner.py").read_text(encoding="utf-8")
    assert "reopen_final2_missing(chart_id)" in source
    assert "refresh_final1 or refresh_final2" in source
