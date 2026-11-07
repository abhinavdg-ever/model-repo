"""2026-11-07 (user): monitoring: time, Doc Intel cost and LLM tokens per run (run_usage)."""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from orchestrator import usage  # noqa: E402

RUN = {"chart_id": 7, "chart_name": "c7", "trigger_id": "T1", "trigger": "batch-run", "status": "completed",
       "started": "2026-11-07T10:00:00+05:30", "ended": "2026-11-07T10:05:00+05:30", "seconds": 300.0,
       "pages": 120, "ocr": True, "docintel_pages": 80, "partial": ""}


def test_doc_intel_cost_comes_from_the_price_in_env(monkeypatch):
    monkeypatch.setenv("PRICE_DOCINTEL_PER_1000_PAGES", "1.50")
    row = usage.build(RUN, {"stage_seconds": {"ocr_final2:1": 61.04}}, {})
    assert row["docintel_cost"] == Decimal("0.1200") and row["docintel_price_per_1000"] == Decimal("1.50")
    assert row["stage_seconds"] == {"ocr_final2:1": 61.0}
    assert row["llm_cost"] == 0 and row["currency"] == "USD"


def test_llm_tokens_are_counted_per_chart_and_priced_per_deployment(monkeypatch):
    monkeypatch.setenv("PRICE_LLM_GPT_4O_MINI_IN_PER_1000", "0.15")
    monkeypatch.setenv("PRICE_LLM_GPT_4O_MINI_OUT_PER_1000", "0.60")
    usage.add_llm_tokens(7, "gpt-4o-mini", 1000, 500)
    usage.add_llm_tokens(7, "gpt-4o-mini", 1000, 500)
    usage.add_llm_tokens(8, "gpt-4o-mini", 99, 99)  # another chart: not this run's
    row = usage.build(RUN, {}, usage.take_llm(7))
    assert (row["llm_prompt_tokens"], row["llm_completion_tokens"]) == (2000, 1000)
    assert row["llm_cost"] == Decimal("0.9000")
    assert row["llm_usage"]["gpt-4o-mini"]["calls"] == 2
    assert usage.take_llm(7) == {}  # reset for the next run
    usage.take_llm(8)


def test_a_deployment_without_a_price_records_tokens_at_cost_zero():
    row = usage.build(RUN, {}, {"unknown-model": {"calls": 1, "prompt_tokens": 10, "completion_tokens": 5,
                                                  "cached_tokens": 0}})
    assert row["llm_prompt_tokens"] == 10 and row["llm_cost"] == 0


def test_the_row_matches_the_table():
    schema = (Path(__file__).resolve().parents[1] / "schema" / "schema.sql").read_text(encoding="utf-8")
    table = schema[schema.index("CREATE TABLE run_usage"):]
    table = table[: table.index(");")]
    for column in usage.COLUMNS:
        assert f"\n    {column} " in table, column
