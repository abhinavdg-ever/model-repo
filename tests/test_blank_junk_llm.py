"""The blank / junk LLM judge for short pages (2026-11-23, user: BLANK_JUNK_LLM). No network: the
call is replaced. Made-up text only."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import modules.blank_junk.stage as stage  # noqa: E402
from modules.blank_junk import llm_judge, method  # noqa: E402

KEEP = {"bert": {"p_keep": 0.9, "p_junk": 0.1, "p_blank": 0.0, "top_junk_label": "",
                 "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}}
NOTE = ("Progress Note\n## Chief Complaint\nmade up cough\n## History of Present Illness\n"
        + "\n".join(f"made up history line {i}" for i in range(30))
        + "\n## Assessment and Plan\nmade up plan line one\nmade up plan line two\n")
SHORT = "made up knee effusion follow up two weeks"


def _run(monkeypatch, texts, answer):
    calls = []
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: KEEP)
    monkeypatch.setattr(llm_judge, "ready", lambda: True)

    def fake(chart_id, messages):
        calls.append((chart_id, messages))
        return answer

    monkeypatch.setattr(llm_judge, "judge", fake)
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
    for i in texts:
        inputs[i].content_bottom = 0.9 if len(texts[i]) > 400 else 0.12
    rows = stage._classify(pages, texts, set(texts), inputs=inputs, signed_ids=set(), signatures={},
                           find_duplicates=True, chart_id=7)
    return {r["page_id"]: r for r in rows}, calls


def test_the_judge_decides_a_short_page_after_a_full_note(monkeypatch):
    rows, calls = _run(monkeypatch, {1: NOTE, 2: SHORT},
                       {"verdict": "junk", "confidence": 0.97, "reason": "only a repeated footer"})
    assert len(calls) == 1 and calls[0][0] == 7
    assert "CURRENT PAGE" in calls[0][1][1]["content"] and "PREVIOUS PAGE" in calls[0][1][1]["content"]
    assert rows[2]["flag"] == "junk" and rows[2]["reason"].startswith("LLM junk")
    assert rows[2]["confidence"] == 0.97


def test_not_asked_without_note_headers_on_the_previous_page(monkeypatch):
    plain = "\n".join(f"made up line {i}" for i in range(60))
    _, calls = _run(monkeypatch, {1: plain, 2: SHORT}, {"verdict": "junk", "confidence": 0.9, "reason": "x"})
    assert calls == []


def test_a_failed_call_keeps_the_rules(monkeypatch):
    rows, calls = _run(monkeypatch, {1: NOTE, 2: SHORT}, {"error": "HTTP 500"})
    assert calls and not rows[2]["reason"].startswith("LLM")


def test_usage_is_recorded_and_the_answer_parsed(monkeypatch):
    from apps.worker import usage

    monkeypatch.setattr(llm_judge, "settings", lambda: {"enabled": True, "endpoint": "https://made-up", "key": "k",
                                                       "deployment": "gpt-made-up", "api_version": "2024-10-21"})
    monkeypatch.setattr(llm_judge, "_post", lambda url, key, body: {
        "choices": [{"message": {"content": '{"verdict": "main", "confidence": 0.8, "reason": "plan continued"}'}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 100}}})
    usage.take_llm(99)
    out = llm_judge.judge(99, llm_judge.prompt(NOTE, None, SHORT, [method.Line(SHORT, 0.05, 0.07)]))
    assert out == {"verdict": "main", "confidence": 0.8, "reason": "plan continued"}
    assert usage.take_llm(99) == {"gpt-made-up": {"calls": 1, "prompt_tokens": 900, "completion_tokens": 30,
                                                  "cached_tokens": 100}}


def test_off_or_unconfigured_is_reported_without_the_key(monkeypatch):
    monkeypatch.setattr(llm_judge, "settings", lambda: {"enabled": True, "endpoint": "", "key": "secret",
                                                       "deployment": "", "api_version": "x"})
    s = llm_judge.status()
    assert s["ready"] is False and "AZURE_OPENAI_ENDPOINT" in s["reason"] and "secret" not in str(s)
    assert llm_judge.parse("not json") is None and llm_judge.parse('{"verdict": "maybe"}') is None


def test_a_gpt5_deployment_gets_the_reasoning_parameters():
    """2026-11-23 (user): gpt-5-mini on Azure OpenAI -- no temperature, max_completion_tokens with
    room for the reasoning, a reasoning effort; older models keep temperature 0 / max_tokens."""
    body = llm_judge.request_body([], "gpt-5-mini", "minimal")
    assert "temperature" not in body and "max_tokens" not in body
    assert body["max_completion_tokens"] >= 1000 and body["reasoning_effort"] == "minimal"
    old = llm_judge.request_body([], "gpt-4o-mini")
    assert old["temperature"] == 0 and old["max_tokens"] == 200 and "reasoning_effort" not in old
