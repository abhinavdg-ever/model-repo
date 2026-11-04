"""2026-11-04 (user): blank / junk pages get their member name, ID and DOB read and shown,
never verified (page_status not_verified, no other names, no matched member)."""
import contextlib
from types import SimpleNamespace

from stages.lib.member import stage


def test_blank_and_junk_pages_are_read_but_not_verified(monkeypatch):
    written, completed = [], []
    monkeypatch.setattr(stage, "connect", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(stage, "upsert_member_extraction", lambda conn, **kw: written.append(kw))
    monkeypatch.setattr(stage, "mark_completed", lambda conn, ctx, pid: completed.append(pid))
    page = SimpleNamespace(
        page_no=3, page_name="3.jpg", detected_name="Test Person", detected_dob="01/01/1950",
        detected_member_id="ABC123", detection_source_name="kv", detection_source_dob="kv",
        detection_source_member_id="kv", ner_key_source_name="", ner_key_source_dob="",
        ner_key_source_member_id="", other_names=["Someone Else"], page_verified=True,
        db_page_status="verified",
    )
    monkeypatch.setattr(stage, "verify_record", lambda **kw: SimpleNamespace(pages=[page], ner_enabled=True))
    monkeypatch.setattr(stage, "_page_confidence", lambda *a, **k: 0.8)
    staged = SimpleNamespace(page=lambda name: None)
    ctx = SimpleNamespace(pages=[{"id": 30, "page_name": "3.jpg", "page_number": 3}])
    rows = stage._extract_only_pages(
        1, "chart", ctx, ctx.pages, {30: "text"}, staged, None, "full", None,
        {"member_name": "Test Person", "external_member_id": "ABC123"}, "1950-01-01", {30: ctx.pages[0]},
    )
    assert completed == [30] and len(rows) == 1
    kw = written[0]
    assert kw["extracted_name"] == "Test Person" and kw["extracted_member_id"] == "ABC123"
    assert kw["page_status"] == "not_verified" and kw["page_verified"] is None
    assert kw["other_names"] is None and kw["matched_member_list_id"] is None


def test_only_blank_and_junk_are_read_duplicates_are_not():
    assert stage.EXTRACT_ONLY_FLAGS == {"blank", "junk"}
