"""The short page in blank / junk pass 2 (2026-11-19, user): no text in the middle of the page
(25%-75%); S1-S6 decide it before the other rules, else the classifier (stage.short_verdict)."""
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





def test_a_contents_page_is_junk_others():
    """2026-11-08 (user): Index / Contents as a title and page numbers -> Junk / Others."""
    text = "TABLE OF CONTENTS\nProgress Note ........ 3\nLab Results ......... 12\nDischarge Summary 20\n"
    inputs = method.PageInputs(text=text, pass_no=2, bert={"p_keep": 0.9, "p_junk": 0.1})
    verdict = method.decide(inputs)
    assert verdict.rule == "INDEX" and verdict.code == stage.CODE_OTHERS
    assert method.index_page("Body Mass Index 23\nweight 80\nheight 170\npulse 70") is None
    assert method.index_page("Index\nPages 1-4 History\nPages 5-9 Labs") is None   # 2 lines only




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


def _short(texts, headers):
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
    for i in texts:
        inputs[i].content_bottom = 0.9 if len(texts[i]) > 400 else 0.12
    return {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True,
                                                       headers_by_id=headers)}






def test_a_contents_pages_next_page_carrying_its_list_is_junk(monkeypatch):
    """2026-11-15 (user): page 2 carries on page 1's CONTENTS list of encounters."""
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax"}}})
    contents = ("CONTENTS\n" + "\n".join(f"Encounter: {m}/14/2025 9:15:00 AM\nTRI_Full Exam" for m in range(1, 9)))
    more = "TRI_Full Exam\nEncounter: 2/11/2025 10:00:00 AM\nTRI_Laser\nEncounter: 2/4/2025 9:00:00 AM\nTRI_OCT"
    texts = {1: contents, 2: more, 3: HEADER + BODY + FOOTER}
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
    for i in texts:
        inputs[i].content_bottom = 0.9
    rows = {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True)}
    assert rows[1]["reason"].startswith("INDEX") and rows[1]["flag"] == "junk"
    assert rows[2]["flag"] == "junk" and "INDEX continued" in rows[2]["reason"]
    assert rows[3]["flag"] == "not_blank_junk"


def test_the_similarity_matrix_is_containment_one_way(tmp_path, monkeypatch):
    """2026-11-15 (user): staging duplicate_similarity_pass<N>.csv, page x page, with word counts;
    page 1 inside page 2 is 100% for row 1 / column 2, not for row 2 / column 1."""
    import csv
    monkeypatch.setattr(stage, "staging_dir", lambda chart: tmp_path)
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in (1, 2, 3)]
    texts = {1: "alpha beta gamma delta", 2: "alpha beta gamma delta epsilon zeta eta theta", 3: "other words"}
    path = stage._write_similarity_matrix("c", 2, pages, texts)
    rows = {r["page"]: r for r in csv.DictReader(path.open(encoding="utf-8"))}
    assert rows["1"]["p2"] == "100.0" and rows["2"]["p1"] == "50.0" and rows["1"]["p1"] == "100.0"
    assert rows["2"]["words"] == "8" and rows["3"]["p1"] == "0.0"


KEEP = {"bert": {"p_keep": 0.9, "p_junk": 0.1, "service": {"flag": "KEEP", "subclass": "", "confidence": 0.9,
                                                           "decision_reason": "argmax"}}}


def _keep(monkeypatch):
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: KEEP)


def test_s5_a_short_page_repeating_a_neighbour_in_order_is_junk(monkeypatch):
    """2026-11-19 (user): 98% of its words in order on the previous / next page -> Junk / Others."""
    _keep(monkeypatch)
    rows = _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER})
    assert rows[2]["flag"] == "junk" and rows[2]["reason"].startswith(stage.SHORT_MARK + "S5")
    assert rows[1]["flag"] == "not_blank_junk"


def test_s6_few_new_words_headers_junk_else_main(monkeypatch):
    """2026-11-19 (user): <= 5 new words: all header words -> Junk / Others; else Main."""
    _keep(monkeypatch)
    texts = {1: HEADER + BODY + FOOTER, 2: FOOTER + " Insurance Guarantor " + HEADER}
    rows = _short(texts, {2: ("Insurance Guarantor",)})
    assert rows[2]["flag"] == "junk" and "S6" in rows[2]["reason"]
    rows = _run({1: HEADER + BODY + FOOTER, 2: FOOTER + " addendum newword " + HEADER})
    assert rows[2]["flag"] == "not_blank_junk" and "S6" in rows[2]["reason"]


def test_s1_the_previous_page_ending_in_a_note_header_keeps_the_short_page(monkeypatch):
    """2026-11-19 (user): the previous page's Progress Note header in its bottom third (one of its
    last 3) -> the short page carries that section on: Main."""
    _keep(monkeypatch)
    before = "\n".join([HEADER] + [f"line {i} {BODY[:40]}" for i in range(20)] + ["Plan:", FOOTER])
    rows = _run({1: before, 2: FOOTER + " " + HEADER})
    assert rows[2]["flag"] == "not_blank_junk" and "S1" in rows[2]["reason"]
    early = "\n".join([HEADER, "Plan:"] + [f"line {i} {BODY[:40]}" for i in range(20)] + [FOOTER])
    assert "S1" not in _run({1: early, 2: FOOTER + " " + HEADER})[2]["reason"]


def test_s2_s3_s4_keep_a_short_page(monkeypatch):
    """S2 a Progress Note header + 3 other words; S3 a signature not repeated nearby; S4
    measurements / doses / ICD codes -> Main."""
    _keep(monkeypatch)
    base = {1: HEADER + BODY + FOOTER}
    assert "S2" in _run({**base, 2: "Orders:\nCBC lipid panel chest xray"})[2]["reason"]
    stamp = {"region": "footer", "sentence": "Electronically signed by Nathan A Monaco MD", "provider_name": "Nathan A Monaco"}
    rows = _run({**base, 2: HEADER + FOOTER}, signed={2}, signatures={2: stamp})
    assert rows[2]["flag"] == "not_blank_junk" and "S3" in rows[2]["reason"]
    # the same signature in the same spot on the neighbour: no keep, judged on its words.
    rows = _run({**base, 2: HEADER + FOOTER}, signed={1, 2}, signatures={1: stamp, 2: stamp})
    assert rows[2]["flag"] == "junk"
    assert "S4" in _run({**base, 2: "Glucose 112 mg/dL fasting, Vitamin D 31 ng/mL"})[2]["reason"]


def test_a_page_not_short_is_not_judged_by_the_short_rules(monkeypatch):
    """Content past 25% of the page: the short rules do not apply (repeated words included)."""
    _keep(monkeypatch)
    texts = {1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER}
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    for bottom, flag in ((0.12, "junk"), (0.35, "not_blank_junk"), (None, "not_blank_junk")):
        inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
        inputs[1].content_bottom, inputs[2].content_bottom = 0.9, bottom
        rows = {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True)}
        assert rows[2]["flag"] == flag, bottom


def test_a_short_page_no_rule_decides_goes_to_the_classifier(monkeypatch):
    """2026-11-19 (user): more than 5 new words and no short rule -> the classifier."""
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {
        "p_keep": 0.1, "p_junk": 0.9, "service": {"flag": "JUNK", "subclass": "JUNK_INSTRUCTIONS",
                                                  "confidence": 0.9, "decision_reason": "argmax"}}})
    rows = _run({1: HEADER + BODY + FOOTER, 2: HEADER + " alpha beta gamma delta epsilon zeta eta"})
    assert rows[2]["reason"].startswith("R5") and rows[2]["flag"] == "junk"
