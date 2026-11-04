"""2026-11-04 (user): the Progress Note unjunk rule (2026-11-03) is undone; pages it made
Main go back to junk with their subtype."""
from types import SimpleNamespace

import pytest

from stages.lib.continuity import stage


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(stage, "connect", lambda: __import__("contextlib").nullcontext(None))
    monkeypatch.setattr(stage, "revise_blank_junk_final", lambda conn, pid, **kw: seen.append((pid, kw)))
    monkeypatch.setattr(stage, "_rewrite_junk_csv", lambda *a: None)
    monkeypatch.setattr(stage, "assign", lambda inputs, rules: "reassigned")
    return seen


def _page(pid, kind="main", subtype=""):
    return SimpleNamespace(page_id=pid, kind=kind, junk_subtype=subtype)


def test_an_unjunked_page_is_junk_again(calls):
    inputs = [_page(1), _page(2), _page(3, "junk", "Invoice")]
    bj = {2: {"reason": stage.UNJUNK_MARK + "Letter/Fax (junk inside a Progress Note, not a short page -> Main)"},
          3: {"reason": "R5 service JUNK"}}
    assert stage._restore_unjunked(1, "c", inputs, [], bj, None) == "reassigned"
    assert [pid for pid, _ in calls] == [2]
    assert calls[0][1]["blank_junk_flag"] == "junk" and calls[0][1]["junk_subtype"] == "Letter/Fax"
    assert inputs[1].kind == "junk"


def test_junk_inside_a_progress_note_stays_junk(calls):
    rows = [{"segment_kind": "main", "document_seq": 1}]
    inputs = [_page(1), _page(2, "junk", "Letter/Fax"), _page(3)]
    assert stage._restore_unjunked(1, "c", inputs, rows, {2: {"reason": "R5 service JUNK"}}, None) is rows
    assert calls == []
