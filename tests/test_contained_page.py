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
    # 2026-11-05 (user): a short page's content ends in the top 20%.
    for i in texts:
        inputs[i].content_bottom = 0.9 if len(texts[i]) > 400 else 0.12
    rows = stage._classify(pages, texts, set(texts), inputs=inputs, signed_ids=set(signed), find_duplicates=True)
    return {row["page_id"]: row for row in rows}


def test_a_page_of_only_the_header_and_footer_is_junk(monkeypatch):
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    rows = _run({1: HEADER + BODY + FOOTER + "1 2", 2: HEADER + FOOTER + "2"})
    assert rows[2]["flag"] == "junk" and rows[2]["subtype"] == "Others"
    assert rows[2]["reason"].startswith(stage.CONTAINED_MARK + "1")
    assert rows[1]["flag"] == "not_blank_junk"


def test_a_signed_page_or_one_with_its_own_words_stays(monkeypatch):
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    rows = _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER}, signed={2})
    assert rows[2]["flag"] == "not_blank_junk"
    rows = _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER + " addendum newword"})
    assert rows[2]["flag"] == "not_blank_junk"


def test_pass_1_marks_an_exact_copy_within_3_pages_the_later_page(monkeypatch):
    """2026-11-03 (user): pass 1 tags the second of two pages with the same distinct words."""
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.5, "p_junk": 0.1}})
    page = HEADER + BODY + FOOTER
    texts = {1: page, 2: page, 3: HEADER + " other " + BODY,
             **{n: HEADER + f" filler{n} " + BODY for n in (4, 5, 6)}, 7: page}
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    inputs = {i: method.PageInputs(text="", pass_no=1) for i in texts}
    rows = {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs,
                                                       find_duplicates=False, exact_near=True)}
    assert rows[2]["flag"] == "duplicate" and rows[2]["duplicate_of"] == 1
    assert rows[1]["flag"] != "duplicate" and rows[3]["flag"] != "duplicate"
    assert rows[7]["flag"] != "duplicate"  # 6 places from page 1, 5 from page 2: not within 3


def test_a_page_adding_only_its_section_headers_is_junk(monkeypatch):
    """2026-11-05 (user): no geometry; the words not on a longer page within 3 are its section
    headers only -> Junk / Others; any other new word keeps it."""
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    texts = {1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER + " Addendum Summary"}
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
    inputs[1].content_bottom, inputs[2].content_bottom = 0.9, 0.12
    inputs[2].headings = ("Addendum Summary",)
    rows = {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True)}
    assert rows[2]["flag"] == "junk" and "section headers only" in rows[2]["reason"]
    inputs[2].headings = ("Addendum",)
    rows = {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True)}
    assert rows[2]["flag"] == "not_blank_junk"


def test_a_contained_page_whose_content_goes_past_20_percent_stays_main(monkeypatch):
    """2026-11-05 (user): the top-20% condition is back; past it, or not measured, stays Main."""
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    texts = {1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER}
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    for bottom, flag in ((0.12, "junk"), (0.35, "not_blank_junk"), (None, "not_blank_junk")):
        inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
        inputs[1].content_bottom, inputs[2].content_bottom = 0.9, bottom
        rows = {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True)}
        assert rows[2]["flag"] == flag, bottom


def test_contained_decides_before_the_classifier(monkeypatch):
    """2026-11-05 (user): the contained page comes before the classifier, so a page the classifier
    junked becomes Junk / Others (contained), and one it kept at p(keep) >= 0.99 is junked too."""
    def scores(text, docling=None):
        flag = "JUNK" if len(text) < 400 else "KEEP"
        return {"bert": {"p_keep": 0.995 if flag == "KEEP" else 0.1, "p_junk": 0.005 if flag == "KEEP" else 0.9,
                         "service": {"flag": flag, "subclass": "JUNK_INSTRUCTIONS" if flag == "JUNK" else "",
                                     "confidence": 0.9, "decision_reason": "argmax"}}}
    monkeypatch.setattr(stage, "model_scores", scores)
    rows = _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER})
    assert rows[2]["subtype"] == "Others" and rows[2]["reason"].startswith(stage.CONTAINED_MARK + "1")
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.995, "p_junk": 0.005, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.99, "decision_reason": "argmax"}}})
    assert _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER})[2]["flag"] == "junk"


def test_a_page_an_earlier_rule_settled_is_not_a_candidate(monkeypatch):
    """Handwritten > 30% (and visual, signed, card, fax) decide before the contained page."""
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    texts = {1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER}
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
    inputs[1].content_bottom, inputs[2].content_bottom = 0.9, 0.12
    inputs[2].hw_area_pct = 45
    rows = {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True)}
    assert rows[2]["flag"] == "not_blank_junk" and rows[2]["reason"].startswith("HW")
