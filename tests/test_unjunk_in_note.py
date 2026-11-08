"""2026-11-03 / 2026-11-06 (user): a junk page inside a Progress Note that is not a short page is Main
again (undone 2026-11-04, restored 2026-11-06)."""
from types import SimpleNamespace

import pytest

import modules.blank_junk.stage  # noqa: F401  (puts the blank/junk helpers on sys.path)
from modules.continuity import stage


def _inputs():
    def page(pid, kind="main", family="Progress Note", subtype=""):
        return SimpleNamespace(page_id=pid, kind=kind, page_type=family, junk_subtype=subtype, text="t", lines=[1])
    return [page(1), page(2, "junk", "", "Letter/Fax"), page(3), page(4, "junk", "", "Invoice"), page(5, family="Lab Report")]


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(stage, "connect", lambda: __import__("contextlib").nullcontext(None))
    monkeypatch.setattr(stage, "revise_blank_junk_final", lambda conn, pid, **kw: seen.append((pid, kw)))
    monkeypatch.setattr(stage, "_rewrite_junk_csv", lambda *a: None)
    monkeypatch.setattr(stage, "assign", lambda inputs, rules: "reassigned")
    return seen


def test_unjunking_inside_a_note_is_off(calls, monkeypatch):
    """2026-11-08 (user): off for now; nothing is unjunked."""
    from modules.continuity import stage as cont
    assert cont.UNJUNK_IN_NOTES is False


def test_junk_between_two_pages_of_a_progress_note_is_main(calls, monkeypatch):
    from modules.continuity import stage as cont
    monkeypatch.setattr(cont, "UNJUNK_IN_NOTES", True)
    rows = [{"segment_kind": "main", "document_seq": 1}, {"segment_kind": "junk", "document_seq": 9},
            {"segment_kind": "main", "document_seq": 1}, {"segment_kind": "junk", "document_seq": 9},
            {"segment_kind": "main", "document_seq": 2}]
    bj = {2: {"reason": "R5 DistilBERT p(junk) 0.900 >= 0.7 -> Junk / Letter/Fax"}, 4: {"reason": "R5 ..."}}
    inputs = _inputs()
    assert stage._unjunk_inside_notes(1, "c", inputs, rows, bj, None) == "reassigned"
    assert [pid for pid, _ in calls] == [2]  # page 4 sits between two documents
    assert calls[0][1]["blank_junk_flag"] == "not_blank_junk"
    assert calls[0][1]["reason"].startswith(stage.UNJUNK_MARK + "Letter/Fax")
    assert inputs[1].kind == "main"


def test_a_short_page_or_card_stays_junk(calls):
    rows = [{"segment_kind": "main", "document_seq": 1}, {"segment_kind": "junk", "document_seq": 9},
            {"segment_kind": "main", "document_seq": 1}]
    for reason in ("R1 tiny page: < 15 characters -> Junk / Others", "short_page_contained_in_page_id:1 (...)"):
        inputs = _inputs()[:3]
        assert stage._unjunk_inside_notes(1, "c", inputs, rows, {2: {"reason": reason}}, None) is rows
    inputs = _inputs()[:3]
    inputs[1].junk_subtype = "Cards"
    assert stage._unjunk_inside_notes(1, "c", inputs, rows, {2: {"reason": "R3 card"}}, None) is rows
    assert not calls


def test_a_page_no_longer_inside_a_note_is_junk_again(calls):
    rows = [{"segment_kind": "main", "document_seq": 1}, {"segment_kind": "main", "document_seq": 2},
            {"segment_kind": "main", "document_seq": 3}]
    inputs = _inputs()[:3]
    inputs[1].kind, inputs[1].junk_subtype = "main", ""
    bj = {2: {"reason": stage.UNJUNK_MARK + "Letter/Fax (junk inside a Progress Note, not a short page -> Main)"}}
    assert stage._unjunk_inside_notes(1, "c", inputs, rows, bj, None) == "reassigned"
    assert calls[0][1]["blank_junk_flag"] == "junk" and calls[0][1]["junk_subtype"] == "Letter/Fax"
