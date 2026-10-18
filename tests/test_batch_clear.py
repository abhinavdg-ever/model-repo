"""A batch does not clear every chart up front: each chart's previous results stay until
its own run starts (run_pipeline_for_chart clears them then)."""
from __future__ import annotations

import inspect


def test_a_batch_does_not_clear_its_charts_before_they_run():
    from jobs import batch_intake
    from orchestrator import runner

    assert not hasattr(runner, "clear_before_batch")
    assert "_clear_for_rerun" not in inspect.getsource(batch_intake)


def test_quality_kept_by_memory_store():
    from db.memory_store import MemoryStore

    store = MemoryStore()
    store.quality[1] = {"chart_id": 7, "page_id": 1}
    store.ocr[1] = {"chart_id": 7, "page_id": 1}
    store.clear_chart_outputs(7, keep_ocr=True, keep_quality=True)
    assert 1 in store.quality and 1 in store.ocr
    store.clear_chart_outputs(7, keep_ocr=True)
    assert 1 not in store.quality and 1 in store.ocr
