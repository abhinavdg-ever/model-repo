"""2026-11-03 (user): blank / junk pass 2 junks a page whose distinct words are all on a longer
Main page within 3 pages, at most half its length (a report's page 2 that only repeats the
header block and footer)."""
import stages.lib.blank_junk.stage as stage
from stages.lib.blank_junk import method

HEADER = "Radiology Ltd Green Valley Patient Watson DOB MRN ACC Requesting Provider CC Provider "
FOOTER = "The information contained in this report is CONFIDENTIAL and LEGALLY PRIVILEGED Page of "
BODY = " ".join(f"finding{i} meniscus ligament effusion impression" for i in range(80)) + " "


def _run(texts, signed=()):
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
    rows = stage._classify(pages, texts, set(texts), inputs=inputs, signed_ids=set(signed), find_duplicates=True)
    return {row["page_id"]: row for row in rows}


def test_a_page_of_only_the_header_and_footer_is_junk(monkeypatch):
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1}})
    rows = _run({1: HEADER + BODY + FOOTER + "1 2", 2: HEADER + FOOTER + "2"})
    assert rows[2]["flag"] == "junk" and rows[2]["subtype"] == "Others"
    assert rows[2]["reason"].startswith(stage.CONTAINED_MARK + "1")
    assert rows[1]["flag"] == "not_blank_junk"


def test_a_signed_page_or_one_with_its_own_words_stays(monkeypatch):
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1}})
    rows = _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER}, signed={2})
    assert rows[2]["flag"] == "not_blank_junk"
    rows = _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER + " addendum newword"})
    assert rows[2]["flag"] == "not_blank_junk"
