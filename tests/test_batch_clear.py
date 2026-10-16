"""A batch clears every chart's previous results when it starts
(orchestrator.runner.clear_before_batch), with the same rules as one chart."""
from __future__ import annotations

import pytest

from orchestrator import runner


@pytest.fixture
def calls(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(runner, "_clear_for_rerun", lambda cid, name, **kw: seen.append({"id": cid, **kw}) or kw)
    return seen


def test_full_forced_run_clears_everything(calls):
    runner.clear_before_batch(1, "c1", force=True, only=None, through=None, skip_ocr=False)
    assert calls == [{"id": 1, "keep_ocr": False}]


def test_skip_ocr_keeps_ocr_and_quality(calls):
    runner.clear_before_batch(1, "c1", force=False, only=None, through=None, skip_ocr=True)
    assert calls == [{"id": 1, "keep_ocr": True, "keep_quality": True}]


@pytest.mark.parametrize("only, through", [(["dos_extract"], None), (None, "ocr_final2")])
def test_partial_run_clears_nothing(calls, only, through):
    assert runner.clear_before_batch(1, "c1", force=True, only=only, through=through, skip_ocr=False) is None
    assert calls == []


def test_quality_kept_by_memory_store():
    from db.memory_store import MemoryStore

    store = MemoryStore()
    store.quality[1] = {"chart_id": 7, "page_id": 1}
    store.ocr[1] = {"chart_id": 7, "page_id": 1}
    store.clear_chart_outputs(7, keep_ocr=True, keep_quality=True)
    assert 1 in store.quality and 1 in store.ocr
    store.clear_chart_outputs(7, keep_ocr=True)
    assert 1 not in store.quality and 1 in store.ocr
