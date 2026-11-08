"""The short page in blank / junk pass 2 (2026-11-08, user; was 2026-11-03 "contained in a page
within 3"): content in the top 20%; signed alone -> Main; a signature carried on from the page
before -> Main; < 5 distinct words not on the page before or after -> Junk / Others."""
import modules.blank_junk.stage as stage
from modules.blank_junk import method

HEADER = "Radiology Ltd Green Valley Patient Watson DOB MRN ACC Requesting Provider CC Provider "
FOOTER = "The information contained in this report is CONFIDENTIAL and LEGALLY PRIVILEGED Page of "
BODY = " ".join(f"finding{i} meniscus ligament effusion impression" for i in range(80)) + " "


def _run(texts, signed=(), signatures=None):
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
    # 2026-11-05 (user): a short page's content ends in the top 20%.
    for i in texts:
        inputs[i].content_bottom = 0.9 if len(texts[i]) > 400 else 0.12
        inputs[i].signed = i in signed
    rows = stage._classify(pages, texts, set(texts), inputs=inputs, signed_ids=set(signed),
                           signatures=signatures or {}, find_duplicates=True)
    return {row["page_id"]: row for row in rows}


def test_a_page_of_only_the_header_and_footer_is_junk(monkeypatch):
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    rows = _run({1: HEADER + BODY + FOOTER + "1 2", 2: HEADER + FOOTER + "2"})
    assert rows[2]["flag"] == "junk" and rows[2]["subtype"] == "Others"
    assert rows[2]["reason"].startswith(stage.SHORT_MARK + "repeated")
    assert rows[1]["flag"] == "not_blank_junk"


def test_a_signed_page_or_one_with_its_own_words_stays(monkeypatch):
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    rows = _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER}, signed={2})
    assert rows[2]["flag"] == "not_blank_junk"
    rows = _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER + " addendum newword reviewed amended today"})
    assert rows[2]["flag"] == "not_blank_junk"            # 5 words of its own
    rows = _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER + " addendum newword"})
    assert rows[2]["flag"] == "junk"                      # 2 of its own: < 5


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


def test_only_the_pages_before_and_after_count(monkeypatch):
    """2026-11-08 (user): the words left are counted against the page before and the page after
    only, not every page within 3."""
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    other = " ".join(f"other{i} words here" for i in range(60))
    # page 3 repeats page 1 (two pages away): not looked at.
    rows = _run({1: HEADER + BODY + FOOTER, 2: other, 3: HEADER + FOOTER})
    assert rows[3]["flag"] == "not_blank_junk"
    rows = _run({1: other, 2: HEADER + BODY + FOOTER, 3: HEADER + FOOTER, 4: other})
    assert rows[3]["flag"] == "junk" and rows[3]["subtype"] == "Others"


def test_a_signature_keeps_a_page_only_when_the_pages_around_it_are_not_signed(monkeypatch):
    """2026-11-08 (user), every page: Electronic Signature here, none on the previous / next
    non-blank page, raw p(junk) < 0.95 -> Main (R4). Blank pages in between are skipped over."""
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    texts = {1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER, 3: HEADER + BODY + FOOTER + " more"}
    rows = _run(texts, signed={2})
    assert rows[2]["flag"] == "not_blank_junk" and rows[2]["reason"].startswith("R4")
    # the page after is signed too: no signature keep, the short page is judged on its words.
    rows = _run(texts, signed={2, 3})
    assert rows[2]["flag"] == "junk" and "r4_not_alone" in rows[2]["votes"]
    # a blank page between them does not hide the signed neighbour.
    rows = _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER, 3: "", 4: HEADER + BODY + FOOTER + " more"},
                signed={2, 4})
    assert rows[2]["flag"] == "junk"
    # p(junk) >= 0.95: no signature keep either.
    assert not method.signature_keeps(True, 0.98) and method.signature_keeps(True, 0.5)
    assert not method.signature_keeps(True, 0.5, neighbour_signed=True)
    junky = {"p_keep": 0.02, "p_junk": 0.98, "service": {"flag": "JUNK", "subclass": "JUNK_OTHER",
                                                         "confidence": 0.98, "decision_reason": "argmax"}}
    assert method.decide(method.PageInputs(text=HEADER + BODY, pass_no=2, signed=True, bert=junky)).rule != "R4"


def test_a_signature_carried_on_from_the_page_before_keeps_the_page(monkeypatch):
    """2026-11-08 (user): the page before ends in its Electronic Signature (kv_extract: footer
    band, or its signature sentence in the last paragraph), and this short page carries the
    name / credentials or a date -> Main. Signature wording alone never counts."""
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    before = HEADER + BODY + FOOTER + "\n\nElectronically signed by Jane Watson"
    footer = {1: {"region": "footer", "sentence": "", "provider_name": "Jane Watson"}}
    rows = _run({1: before, 2: HEADER + FOOTER + " MD"}, signed={1}, signatures=footer)
    assert rows[2]["flag"] == "not_blank_junk" and "signature_continued" in rows[2]["votes"]["short_page"]
    # mid page, but its sentence is the last paragraph: still the end of the page.
    mid = {1: {"region": "mid", "sentence": "Electronically signed by Jane Watson", "provider_name": "Jane Watson"}}
    assert _run({1: before, 2: HEADER + FOOTER + " 01/02/2024"}, signed={1}, signatures=mid)[2]["flag"] == "not_blank_junk"
    # the signature sits mid page, not at its end: judged on its words.
    early = {1: {"region": "header", "sentence": "finding1 meniscus", "provider_name": "Jane Watson"}}
    assert _run({1: before, 2: HEADER + FOOTER + " MD"}, signed={1}, signatures=early)[2]["flag"] == "junk"
    # no Electronic Signature on the page before (the wording alone): judged on its words.
    assert _run({1: before, 2: HEADER + FOOTER + " MD"}, signatures=footer)[2]["flag"] == "junk"
    # nothing of the signature here (no name, credentials or date): judged on its words.
    assert _run({1: before, 2: HEADER + FOOTER}, signed={1}, signatures=footer)[2]["flag"] == "junk"


def test_a_contents_page_is_junk_others():
    """2026-11-08 (user): Index / Contents as a title and page numbers -> Junk / Others."""
    text = "TABLE OF CONTENTS\nProgress Note ........ 3\nLab Results ......... 12\nDischarge Summary 20\n"
    inputs = method.PageInputs(text=text, pass_no=2, bert={"p_keep": 0.9, "p_junk": 0.1})
    verdict = method.decide(inputs)
    assert verdict.rule == "INDEX" and verdict.code == stage.CODE_OTHERS
    assert method.index_page("Body Mass Index 23\nweight 80\nheight 170\npulse 70") is None
    assert method.index_page("Index\nPages 1-4 History\nPages 5-9 Labs") is None   # 2 lines only


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
    assert rows[2]["subtype"] == "Others" and rows[2]["reason"].startswith(stage.SHORT_MARK + "repeated")
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


def test_repeated_content_is_off():
    """2026-11-08 (user): a pair is a Duplicate, a Partial, or nothing."""
    assert stage.REPEATED_CONTENT is False


def test_a_shorter_page_mostly_in_another_is_repeated_content(monkeypatch):
    """2026-11-06 (user): shorter than the other, at least 80% of its length, and at most 3 of
    its distinct words not on it -> Repeated Content (Main, pointing at the earlier page), never
    a Duplicate by this rule. (Off since 2026-11-08; checked with it switched on.)"""
    monkeypatch.setattr(stage, "REPEATED_CONTENT", True)
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    words = [f"finding{i}" for i in range(100)]
    longer = " ".join(words)
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in (1, 2)]

    def run(second):
        texts = {1: longer, 2: second}
        inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
        for i in texts:
            inputs[i].content_bottom = 0.9
        return {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True)}

    # 85 of its words on page 1 plus 3 of its own: about 85% of page 1's length.
    row = run(" ".join(words[:85] + [f"extra{i}" for i in range(3)]))[2]
    assert row["flag"] == "not_blank_junk" and stage.SAME_CONTENT_MARK + "1" in row["reason"]
    # The shorter page is the one marked, even when it comes first.
    texts = {1: " ".join(words[:85] + [f"extra{i}" for i in range(3)]), 2: longer}
    inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
    for i in texts:
        inputs[i].content_bottom = 0.9
    first = {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True)}
    assert stage.SAME_CONTENT_MARK + "2" in first[1]["reason"]
    assert stage.SAME_CONTENT_MARK not in (first[2]["reason"] or "")
    # 4 words of its own: not by this rule.
    assert stage.SAME_CONTENT_MARK not in (run(" ".join(words[:85] + [f"extra{i}" for i in range(4)]))[2]["reason"] or "")
    # 70% of the length: too short.
    assert stage.SAME_CONTENT_MARK not in (run(" ".join(words[:70] + ["extra1"]))[2]["reason"] or "")


def test_a_short_page_adding_only_diagnosis_stays(monkeypatch):
    """2026-11-08 (user): when the only words a short page adds to its neighbour are "Diagnosis"
    or "Visit Diagnosis", it is not junked; any other header-only addition still is."""
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    for added, kept in (("Visit Diagnosis", True), ("Diagnosis", True), ("Addendum Summary", False)):
        texts = {1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER + " " + added}
        pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
        inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
        inputs[1].content_bottom, inputs[2].content_bottom = 0.9, 0.12
        inputs[2].headings = (added,)
        rows = {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True)}
        assert (rows[2]["flag"] == "not_blank_junk") is kept, added
