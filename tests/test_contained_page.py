"""The short page in blank / junk pass 2 (2026-11-19, user): no text in the middle of the page
(25%-75%); S1-S6 decide it before the other rules, else the classifier (stage.short_verdict)."""
import types

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
    # 2026-11-20 (user): the title in the top third, followed by 3+ page-number entries.
    assert method.index_page("Index\nPages 1-4 History\nPages 5-9 Labs") is None   # 2 entries only
    assert method.index_page("Table of Contents\nHistory\nLabs\nImaging") is None  # no page numbers
    # Docling's table form, and page numbers on lines of their own, are entries.
    table = "## Table of Contents\n| Section | Page |\n|---|---|\n| Progress Note | 3 |\n| Labs | 12 |\n| Imaging | 20 |"
    assert method.index_page(table) is not None
    assert method.index_page("CONTENTS\nProgress Note\n3\nLabs\n12\nImaging\n20") is not None
    # A title lower than the top third of the page does not count.
    low = "History\nLabs\nImaging\nNotes\nContents\nProgress Note ... 3\nLabs ... 12\nImaging ... 20"
    assert method.index_page(low) is None
    entries = "\nProgress Note ... 3\nLabs ... 12\nImaging ... 20"
    lines = [types.SimpleNamespace(text="Patient Summary", top=0.05), types.SimpleNamespace(text="CONTENTS", top=0.55)]
    assert method.index_page("Patient Summary\nCONTENTS" + entries, lines=lines) is None
    lines[1].top = 0.2
    assert method.index_page("Patient Summary\nCONTENTS" + entries, lines=lines) is not None




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
    """2026-11-21 (user): matched lines removed; of the unique words left, dates and numbers left
    out: 1 or none -> Junk; header words / a title -> Junk; 2-4 -> Main."""
    _keep(monkeypatch)
    page1 = HEADER + "\n" + BODY + "\n" + FOOTER
    texts = {1: page1, 2: HEADER + "\nInsurance Guarantor Information\n" + FOOTER}
    rows = _short(texts, {2: ("Insurance Guarantor Information",)})
    assert rows[2]["flag"] == "junk" and "header" in rows[2]["reason"]
    rows = _run({1: page1, 2: HEADER + "\naddendum 12 01/02/2026\n" + FOOTER})
    assert rows[2]["flag"] == "junk" and "of its own" in rows[2]["reason"]
    rows = _run({1: page1, 2: HEADER + "\naddendum newword\n" + FOOTER})
    assert rows[2]["flag"] == "not_blank_junk" and "S6" in rows[2]["reason"]


def test_s6_dates_and_numbers_are_not_words_of_its_own(monkeypatch):
    """2026-11-21 (user): a line of only a date and a time is no content; two words of its own
    are. Each pair is compared on the same OCR engine."""
    _keep(monkeypatch)
    page1 = HEADER + "\n" + BODY + "\n" + FOOTER
    assert _run({1: page1, 2: HEADER + "\n01/28/26 0443"})[2]["flag"] == "junk"
    assert _run({1: page1, 2: HEADER + "\nAddendum reviewed 01/28/26"})[2]["flag"] == "not_blank_junk"
    garbled = "Radlology Lt Grean Valey Patlent Watsen DOB MRN ACC\naddendum"
    texts = {1: page1, 2: garbled}
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
    inputs[1].content_bottom, inputs[2].content_bottom = 0.9, 0.12
    engines = {1: {"docling": page1}, 2: {"azuredocintel": garbled, "docling": HEADER + "\naddendum"}}
    rows = {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs,
                                                       find_duplicates=True, engine_texts=engines)}
    assert rows[2]["flag"] == "junk" and "of its own" in rows[2]["reason"]       # only 'addendum' is left


def test_s1_the_previous_page_ending_in_a_note_header_keeps_the_short_page(monkeypatch):
    """2026-11-19 (user): the previous page's Progress Note header in its bottom third (one of its
    last 3) -> the short page carries that section on: Main."""
    _keep(monkeypatch)
    before = "\n".join([HEADER] + [f"line {i} {BODY[:40]}" for i in range(20)] + ["## Plan", FOOTER])
    rows = _run({1: before, 2: FOOTER + " " + HEADER + "\ncontinue current medications, follow up"})
    assert rows[2]["flag"] == "not_blank_junk" and "S1" in rows[2]["reason"]
    # 2026-11-23 (user): a page adding nothing over the previous one (its words are all there,
    # numbers and dates aside) carries nothing on.
    assert "S1" not in (_run({1: before, 2: FOOTER + " " + HEADER + " 12/01/2025 4"})[2]["reason"] or "")
    early = "\n".join([HEADER, "## Plan"] + [f"line {i} {BODY[:40]}" for i in range(20)] + [FOOTER])
    assert "S1" not in _run({1: early, 2: FOOTER + " " + HEADER})[2]["reason"]


def test_s2_s3_s4_keep_a_short_page(monkeypatch):
    """S2 a Progress Note header + 3 other words; S3 a signature not repeated nearby; S4
    measurements / doses / ICD codes -> Main."""
    _keep(monkeypatch)
    base = {1: HEADER + BODY + FOOTER}
    assert "S2" in _run({**base, 2: "## Orders\nCBC lipid panel chest xray"})[2]["reason"]
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


def test_s3_a_provider_in_the_same_spot_on_a_neighbour_does_not_keep(monkeypatch):
    """2026-11-19 (user): like the signature, a provider printed in the same spot (region and name)
    on the previous / next page is a running line; elsewhere or another name keeps the page."""
    _keep(monkeypatch)
    texts = {1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER}
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]

    def run(spot_1, spot_2):
        inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
        inputs[1].content_bottom, inputs[2].content_bottom = 0.9, 0.12
        for i, spot in ((1, spot_1), (2, spot_2)):
            inputs[i].provider, inputs[i].provider_spot = spot is not None, spot
        return {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True)}

    name = frozenset({"jane", "roe"})
    assert run(("footer", name), ("footer", name))[2]["flag"] == "junk"           # running provider line
    assert "S3" in run(("header", name), ("footer", name))[2]["reason"]            # another spot
    assert "S3" in run(("footer", frozenset({"amit", "patel"})), ("footer", name))[2]["reason"]  # another name
    assert "S3" in run(None, ("footer", name))[2]["reason"]                        # none nearby


def test_s3_compares_the_provider_boxes_when_kv_extract_gives_them():
    """2026-11-20 (user): kv_extract's credentials row ("Roberto Velasco MD", box) tells where the
    provider is printed: almost the same box on a neighbour is a running line."""
    name = frozenset({"roberto", "velasco"})
    here = ("line", name, (0.287, 0.31))          # box [463, 662, 511, 697] on a 1700 x 2200 page
    assert stage.same_spot(here, ("line", name, (0.29, 0.32)))        # almost the same box
    assert not stage.same_spot(here, ("line", name, (0.29, 0.80)))    # lower on the page
    assert not stage.same_spot(here, ("line", frozenset({"amit", "patel"}), (0.287, 0.31)))
    assert stage.same_spot(("footer", name), ("footer", name, (0.5, 0.95)))  # one box missing: the region


def test_pass_1_rules_rerun_only_on_handwritten_or_low_quality_pages():
    """2026-11-19 (user): pass 2 reruns pass 1's text rules only on a handwritten / mixed or Low
    page pass 1 never judged; a form or visual page is not."""

    from modules.ocr.routing import judged_in_pass2, page_kind

    def rerun(q):
        return judged_in_pass2(q) and (page_kind(q) in {"handwritten", "mixed"}
                                       or str(q.get("quality_tag") or "").lower() == "low")

    assert rerun({"document_type": "handwritten", "handwritten_area_pct": 60})
    assert rerun({"document_type": "printed", "handwritten_area_pct": 2, "quality_tag": "low"})
    assert not rerun({"document_type": "form", "handwritten_area_pct": 2})
    assert not rerun({"document_type": "visual", "handwritten_area_pct": 1})
    assert not rerun({"document_type": "printed", "handwritten_area_pct": 2, "quality_tag": "high"})


def test_sf_a_short_fax_routing_page_is_junk_even_with_a_provider(monkeypatch):
    """2026-11-19 (user): "To: Dr ... Fax: (555) ..." on a short page is Junk / Letter/Fax, even
    though it names a provider (S3 would keep it); note content still keeps it."""
    _keep(monkeypatch)
    routing = "To: Dr Jane Roe MD\nFax: (330) 555-0142\nFrom: Records Dept"
    texts = {1: HEADER + BODY + FOOTER, 2: routing}
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
    inputs[1].content_bottom, inputs[2].content_bottom = 0.9, 0.12
    inputs[2].provider, inputs[2].provider_spot = True, ("header", frozenset({"jane", "roe"}))
    rows = {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True)}
    assert rows[2]["flag"] == "junk" and "SF" in rows[2]["reason"]
    assert method.fax_routing("Fax: 330-555-0142") is None                      # no To / From line
    assert method.fax_routing("To: Dr Roe\nFax # 330.555.0142") is not None


def test_a_short_page_rules_verdict_reads_its_evidence(monkeypatch):
    """2026-11-21 (user; was a flat 0.60): the rule's margin, the page's uniqueness and the model's
    p -- the same S5 page is surer when the model agrees than when it disagrees."""
    _keep(monkeypatch)
    disagree = _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER})[2]
    junk = {"bert": {"p_keep": 0.1, "p_junk": 0.9, "service": {"flag": "JUNK", "subclass": "", "confidence": 0.9,
                                                              "decision_reason": "argmax"}}}
    monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: junk)
    agree = _run({1: HEADER + BODY + FOOTER, 2: HEADER + FOOTER})[2]
    assert "S5" in disagree["reason"] and "S5" in agree["reason"]
    assert 0.2 <= disagree["confidence"] < 0.6 < agree["confidence"] <= 1.0


def test_s6_page_numbers_and_a_lone_title_do_not_keep_a_short_page(monkeypatch):
    """2026-11-20 (user): "Printed on 5/26/26 11:43 AM Page 13" is page 12's footer (page numbers
    left out of the comparison); a lone title ("EKG/Stress Test") under the running header is
    junk too."""
    _keep(monkeypatch)
    head = "Optum Health Information Services Brennan Calvin MRN 33994923 DOB 11/02/1957\nLabs (continued)"
    page12 = head + "\n" + BODY + "\nPrinted on 5/26/26 11:43 AM Page 12"
    rows = _run({1: page12, 2: head + "\nPrinted on 5/26/26 11:43 AM Page 13"})
    assert rows[2]["flag"] == "junk"
    dignity = "Dignity Health NAME Jordan, George DOB 12/11/1997\nDate/Time Printed: 6/16/2026 09:49 PDT Page {} of 46"
    rows = _run({1: dignity.format(16).replace("\nDate", "\n" + BODY + "\nDate"),
                 2: "Dignity Health NAME Jordan, George DOB 12/11/1997\nEKG/Stress Test\nDate/Time Printed: 6/16/2026 09:49 PDT Page 17 of 46"})
    assert rows[2]["flag"] == "junk" and "title" in rows[2]["reason"]
    assert stage._title_like("EKG/Stress Test") and not stage._title_like("patient refused treatment today")


def test_a_note_opening_with_a_table_of_contents_bar_is_not_a_contents_page():
    """2026-11-20 (user): a Sharecare summary has "Table of Contents" and its sections at the top,
    then its content with a few dates: entries must be most of the lines after the title."""
    page = ("Patient Name: Blake Daniel DOB: 10/03/1998\nTable of Contents\n"
            "Encounter Details Social History Plan of Treatment Visit Diagnoses\nEncounter Details\n"
            "Date Type Department Care Team Description\n12/06/2025 Travel\nSocial History\n"
            "Tobacco Use Types Packs/Day Years Used Date\nSmoking Tobacco: Never\nSmokeless Tobacco: Never\n"
            "Sex Assigned at Birth Not on file\nLegal Sex Male 08/01/2017 8:00 PM PDT\nGender Identity Not on file\n"
            "Plan of Treatment\nNot on file\nVisit Diagnoses\nNot on file documented in this encounter\n"
            "Encounter Summary 01/02/2026")
    assert method.index_page(page) is None


def test_page_colon_number_is_left_out_of_the_line_comparison():
    """2026-11-20 (user): "Visit: 10/20/2025 Page: 3" is the next page's "... Page: 4"."""
    assert stage._line_key("Patient: Romero, Eli DOB: 09/07/1992 Visit: 10/20/2025 Page: 3") == \
        stage._line_key("Patient: Romero, Eli DOB: 09/07/1992 Visit: 10/20/2025 Page: 4")


def test_a_line_differing_in_one_number_still_matches():
    """2026-11-20 (user): one word different, both numbers -> the same line; a date is not."""
    theirs = {stage._line_key("Printed on 5/26/26 11:43 AM"), stage._line_key("Acct 1234 Visit 3")}
    assert stage._line_on(stage._line_key("Printed on 5/26/26 11:44 AM"), theirs)
    assert stage._line_on(stage._line_key("Acct 1234 Visit 4"), theirs)
    assert not stage._line_on(stage._line_key("Printed on 5/27/26 11:43 AM"), theirs)   # another date
    assert not stage._line_on(stage._line_key("Acct 9999 Visit 4"), theirs)             # two numbers differ


def test_a_lab_table_carried_onto_a_short_page_is_main(monkeypatch):
    """2026-11-20 (user): 'WBC 6.2 x10(3)/mcL 4.5-11.0x10(3)/mcL' rows at the top of a page,
    under nothing but the repeated footer -> measurements (S4) -> Main."""
    _keep(monkeypatch)
    footer = "Tenet Healthcare\nPatient: DAVIS, MICHAEL DOB: 05/31/1949\nPage {} of 12"
    rows = "\n".join("Sodium Lvl 139 mmol/L 136-145mmol/L 2026-01-26" for _ in range(30))
    rows = _run({1: "Lab\n" + rows + "\n" + footer.format(3),
                 2: "WBC 6.2 x10(3)/mcL 4.5-11.0x10(3)/mcL 2026-01-26\nRed Cells Complete\n" + footer.format(4)})
    assert rows[2]["flag"] == "not_blank_junk" and "S4" in rows[2]["reason"]


def test_a_short_page_whose_only_own_line_is_a_stage_6_header_is_junk(monkeypatch):
    """2026-11-20 (user): the only difference from the neighbour is the page's stage 6 section
    header ("EKG/Stress Test", here not title-cased the way OCR read it) -> Junk / Others."""
    _keep(monkeypatch)
    head = "Dignity Health\nNAME Jordan, George DOB: 12/11/1997 Age: n/a Sex: M\nPhysician: Tempe, Mario R DO"
    texts = {1: head + "\nLab Results\n" + BODY + "\nDate/Time Printed: 6/16/2026 09:49 PDT Page 16 of 46",
             2: head + "\nEKG/STRESS TEST results section\nDate/Time Printed: 6/16/2026 09:49 PDT Page 17 of 46"}
    rows = _short(texts, {2: ("EKG/STRESS TEST results section",)})
    assert rows[2]["flag"] == "junk" and "section header" in rows[2]["reason"]


P18 = """Dignity Health.
n/a n/a n/a
Facility Phone #: n/a
NAME: Jordan, George MRN: 9752541(AMB); 9752541(SJH); 3600306426(AG); Acct #: 8000008888(EV) Pt Loc: n/a
DOB: 12/11/1997 Age: n/a Sex: M
Admit Date: n/a
Disch Date: n/a
Physician:
Tempe, Mario R DO
Cardiology Reports
DOCUMENT NAME: RECEIVED DATE/TIME: RESULT STATUS: PERFORM INFORMATION: SIGN INFORMATION:
Electrocardiogram 1/29/2026 09:36 MST Auth (Verified) Jain, Shashank MD (1/30/2026 23:42 MST)
""" + BODY + """
Date/Time Printed: 6/16/2026 09:49 PDT
Page 16 of 46"""

P19 = """Dignity Health.
n/a n/a n/a
Facility Phone #: n/a
NAME: Jordan, George MRN: 9752541(AMB); 9752541(SJH); 3600306426(AG); Acct #: 8000008888(EV) Pt Loc: n/a
DOB: 12/11/1997 Age: n/a Sex: M
Admit Date: n/a Disch Date: n/a Physician: Tempe, Mario R DO
EKG/Stress Test
Date/Time Printed: 6/16/2026 09:49 PDT
Page 17 of 46"""


def test_lines_joined_differently_by_the_ocr_still_match(monkeypatch):
    """2026-11-20 (user): Dignity p18 / p19 (Doc Intel): the header's "Admit Date ... Physician"
    is four lines on p18 and one on p19; with it matched, only "EKG/Stress Test" is left -> Junk."""
    _keep(monkeypatch)
    rows = _run({1: P18, 2: P19})
    assert rows[2]["flag"] == "junk", rows[2]["reason"]
    rows = _short({1: P18, 2: P19}, {2: ("EKG/Stress Test",)})
    assert rows[2]["flag"] == "junk" and "header" in rows[2]["reason"]


def test_a_nextgen_contents_page_of_encounters_and_items():
    """2026-11-21 (user): CONTENTS, 'Encounter: <date>' entries, each with short item names under
    it (Letter_To_Patient, APSO Summary, intake_note ...) -> a contents page."""
    page = ("NextGen Patient Record\nName: Le Blanc Jonathan\nDOB: 09/07/1997\nCONTENTS\n"
            "Encounter: 11/25/2025 3:32:57 PM\nLetter_To_Patient\n"
            "Encounter: 3/25/2025 3:00:00 PM\nAPSO Summary\nintake_note\nMaster_im\nPatient Plan\n"
            "Encounter: 1/16/2025 12:40:00 PM\nAPSO Summary\nintake_note\nMaster_im\nPatient Plan\n"
            "Provider_Test_Action\nVVHC_Medications")
    assert method.index_page(page) is not None


# 2026-11-21 (user): line positions -- a line repeated from a neighbour is its running header /
# footer only in the same band of the page (or as a run of lines carried over).
GONZALEZ = "Patient Name: Gonzalez Nicholas Date of Birth: 06/18/1994 Sex:F Encounter Id:3663216 Encounter Date: December 16, 2025"


def _placed(texts, tops):
    """_classify with OCR line positions: tops[page] = {line: top}."""
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    inputs = {}
    for i, text in texts.items():
        lines = [method.Line(text=line, top=tops[i][line], bottom=tops[i][line] + 0.01)
                 for line in text.splitlines() if line.strip()]
        inputs[i] = method.PageInputs(text="", pass_no=2, lines=lines)
    return {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs, find_duplicates=True)}


def _spread(lines, start, end):
    return {line: start + (end - start) * k / max(1, len(lines) - 1) for k, line in enumerate(lines)}


def test_a_running_header_page_after_a_note_header_is_junk(monkeypatch):
    """Gonzalez p12 / p13: the previous page ends in "Physical Exam" (bottom third), p13 is only
    the running header and the page number -> nothing of its own: S1 does not keep it -> Junk."""
    _keep(monkeypatch)
    body = [f"Constitutional line {k} general appearance obese no acute distress" for k in range(12)]
    p1 = [GONZALEZ] + body + ["Physical Exam", "Eyes: PERRLA, extraocular movements intact", "2"]
    p2 = [GONZALEZ, "3"]
    tops = {1: {**_spread(p1, 0.02, 0.97)}, 2: {GONZALEZ: 0.02, "3": 0.97}}
    rows = _placed({1: "\n".join(p1), 2: "\n".join(p2)}, tops)
    assert rows[2]["flag"] == "junk" and "S1" not in rows[2]["reason"], rows[2]["reason"]


def test_a_none_recorded_section_page_is_junk(monkeypatch):
    """"DME Orders / None recorded." under the previous page's "Implant Orders / None recorded." -> Junk."""
    _keep(monkeypatch)
    body = [f"Diabetes Mellitus Y Oral Meds row {k} no information" for k in range(12)]
    p1 = body + ["Mental Status", "Unknown.", "Medical Equipment", "Implant Orders", "None recorded."]
    p2 = ["DME Orders", "None recorded."]
    tops = {1: _spread(p1, 0.02, 0.9), 2: {"DME Orders": 0.08, "None recorded.": 0.12}}
    rows = _placed({1: "\n".join(p1), 2: "\n".join(p2)}, tops)
    assert rows[2]["flag"] == "junk", rows[2]["reason"]


def test_a_list_carried_on_from_the_middle_of_the_previous_page_is_not_junk(monkeypatch):
    """z0910: the diagnosis list goes on at the top of the next page; its lines are also in the
    middle of the previous page (its problem list), in another order -> not the previous page's lines."""
    _keep(monkeypatch)
    dx = ["Pain of right lower extremity", "Lumbar post-laminectomy syndrome",
          "Long term (current) use of opiate analgesic", "Cervical radiculopathy"]
    middle = ["Musculoskeletal", "Lumbar radiculopathy", "Relevant Medications", dx[0], "Relevant Medications",
              dx[3], "Behavioral Health", dx[1], "Relevant Medications", "Other", dx[2], "Relevant Medications"]
    p1 = [GONZALEZ] + middle + ["Visit Diagnoses", "Diagnosis", "Periumbilical hernia - Primary", "6"]
    tops = {1: {GONZALEZ: 0.02, **_spread(middle, 0.35, 0.65), "Visit Diagnoses": 0.8, "Diagnosis": 0.82,
                "Periumbilical hernia - Primary": 0.85, "6": 0.97}}
    p2 = [GONZALEZ, "Diagnosis"] + dx
    tops[2] = {GONZALEZ: 0.02, "Diagnosis": 0.05, **_spread(dx, 0.08, 0.2)}
    rows = _placed({1: "\n".join(p1), 2: "\n".join(p2)}, tops)
    assert rows[2]["flag"] == "not_blank_junk", rows[2]["reason"]


def test_a_block_carried_over_is_the_neighbours_wherever_it_sits(monkeypatch):
    """A run of 2+ lines in the same order on the neighbour is its block, in any band."""
    block = ["Result type: Orthopedic Progress Note", "Result date: 01/14/2025", "Result status: Auth (Verified)",
             "Contributor system: PowerChart"]
    p1 = [GONZALEZ] + block + [f"exam line {k} range of motion strength sensation intact" for k in range(10)] + ["5"]
    p2 = [GONZALEZ] + block + ["6"]
    tops = {1: _spread(p1, 0.02, 0.97), 2: {GONZALEZ: 0.02, **_spread(block, 0.8, 0.9), "6": 0.97}}
    rows = _placed({1: "\n".join(p1), 2: "\n".join(p2)}, tops)
    assert rows[2]["flag"] == "junk", rows[2]["reason"]


def test_a_document_details_cover_sheet_is_junk(monkeypatch):
    """z0913: "Document Details / Date / Type: EKG / Subject: EKG / Approved By / Comments: No" -> Junk."""
    _keep(monkeypatch)
    footer = "06/16/2026 11:35 am Black, Tommy Lee DOB 01/26/1947 30/43"
    sheet = ["Document Details", "Date: 2/24/2026", "Type: EKG", "Subject: EKG", "Approved By: Jessica Vickery",
             "Comments: No", footer]
    before = ["Document Details", f"tracing {0} sinus rhythm"] + [f"lead {k} interval normal axis" for k in range(10)] + [
        footer.replace("30/43", "29/43")]
    tops = {1: _spread(before, 0.02, 0.97), 2: {**_spread(sheet[:-1], 0.03, 0.2), footer: 0.97}}
    rows = _placed({1: "\n".join(before), 2: "\n".join(sheet)}, tops)
    assert rows[2]["flag"] == "junk" and "SC" in rows[2]["reason"], rows[2]["reason"]
    # a short clinical page of fields is not a cover sheet.
    assert stage.cover_sheet_fields(["Date: 2/24/2026", "Provider: Jane Roe MD", "Diagnosis: hypertension"]) is None


WELLS = "Patient Name: Dorothy Wells, Date of Birth: 08/26/1981 Sex:M Encounter Date:January 22, 2026"


def test_a_line_under_the_header_there_and_after_a_title_here_is_the_pages_own(monkeypatch):
    """Wells p23 / p24 (2026-11-21): 'Chronic kidney disease, stage 3a' is right under the header on
    p23 and after 'Diagnosis' on p24 -- the header is matched from the top edge in order, so the
    diagnosis is p24's own: not junk."""
    _keep(monkeypatch)
    ckd = "Chronic kidney disease, stage 3a (CMS/HCC)"
    p23 = [WELLS, ckd, "GFR at 49 previously, which is low. Microalbumin elevated at 97. Continue losartan"] + [
        f"Diagnosis line {k} hypertension hyperlipidemia hypothyroidism" for k in range(20)] + ["6"]
    p24 = [WELLS, "Diagnosis", ckd, "7"]
    tops = {1: _spread(p23, 0.02, 0.97), 2: {WELLS: 0.02, "Diagnosis": 0.03, ckd: 0.04, "7": 0.97}}
    rows = _placed({1: "\n".join(p23), 2: "\n".join(p24)}, tops)
    assert rows[2]["flag"] == "not_blank_junk", rows[2]["reason"]


def test_a_signature_carried_onto_the_next_page_keeps_it(monkeypatch):
    """70069595 p54 / p55: p54 ends 'Signed By ... / Signed', p55 is 'Rajinder Grover on 7/30/2025
    at 1:12 PM' and the footer -> Main (S1S)."""
    _keep(monkeypatch)
    foot = "Tsagic Antonio DOB: 01/02/1950 (216009)"
    p54 = [f"Assessment line {k} multiple myeloma follow up in two weeks" for k in range(20)] + [
        "Signed By: Sarah Transcription on 7/29/2025 at 9:46 PM", "Signed", foot, "Page 3 of 4"]
    p55 = ["Rajinder Grover on 7/30/2025 at 1:12 PM", foot, "Page 4 of 4"]
    tops = {1: _spread(p54, 0.02, 0.97), 2: {p55[0]: 0.02, foot: 0.95, "Page 4 of 4": 0.97}}
    rows = _placed({1: "\n".join(p54), 2: "\n".join(p55)}, tops)
    assert rows[2]["flag"] == "not_blank_junk" and "S1S" in rows[2]["reason"], rows[2]["reason"]


def test_end_of_report_alone_is_a_cover_page(monkeypatch):
    """TMC p34: running header, 'End Of Report', footer -> Junk / Cover Page (SE), whatever the
    header's word count."""
    _keep(monkeypatch)
    head = ["TMC HEALTH 5301 E Grant Rd TUCSON AZ 85712 Brennan, Ronald MRN: 6056636, DOB: 05/08/1984 Legal Sex: M",
            "09/25/2025 - Office Visit in Sonoran Foot and Ankle Institute - Green Valley (continued)"]
    p33 = head + [f"Plan line {k} orthotics follow up wound care" for k in range(20)] + ["Printed on 4/6/26 10:09 AM Page 26"]
    p34 = head + ["End Of Report", "Printed on 4/6/26 10:09 AM Page 27"]
    tops = {1: _spread(p33, 0.02, 0.97), 2: {head[0]: 0.02, head[1]: 0.06, "End Of Report": 0.08, p34[-1]: 0.97}}
    rows = _placed({1: "\n".join(p33), 2: "\n".join(p34)}, tops)
    assert rows[2]["flag"] == "junk" and "SE" in rows[2]["reason"], rows[2]["reason"]
    assert rows[2]["subtype"] == "Cover Page" if "subtype" in rows[2] else True


def _run_lines(texts, lines, signed=()):
    """_run with measured OCR line positions (SH reads them)."""
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
    inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
    for i in texts:
        inputs[i].lines = [method.Line(text=t, top=top, bottom=bottom) for t, top, bottom in lines[i]]
        inputs[i].signed = i in signed
    rows = stage._classify(pages, texts, set(texts), inputs=inputs, signed_ids=set(signed), signatures={},
                           find_duplicates=True)
    return {row["page_id"]: row for row in rows}


FAX_HEAD = "From: Made Up Clinic 18885550100 6/1/2026 10:26:57 PDT Page 17 of 17"
PT_FOOT = "Patient: Madeup, Eli DOB: 09/07/1992 Visit: 06/02/2025 Page: 3"


def test_sh_a_page_of_only_a_fax_header_and_a_patient_footer_is_junk(monkeypatch):
    """2026-11-23 (user): the fax header and the patient footer carry dates, which kept the page
    (S1S / S6): every line at the page edges, each a running line -> Junk / Others."""
    _keep(monkeypatch)
    note = "Progress note\n" + BODY + "\nElectronically signed by Made Up MD 6/2/2025"
    texts = {1: "From: Made Up Clinic 18885550100 6/1/2026 10:26:50 PDT Page 16 of 17\n" + note
                + "\nPatient: Madeup, Eli DOB: 09/07/1992 Visit: 05/30/2025 Page: 2",
             2: FAX_HEAD + "\n" + PT_FOOT}
    lines = {1: [("x", 0.01, 0.02), ("y", 0.3, 0.5), ("z", 0.95, 0.96)],
             2: [(FAX_HEAD, 0.01, 0.02), (PT_FOOT, 0.94, 0.95)]}
    rows = _run_lines(texts, lines)
    assert rows[2]["flag"] == "junk" and "SH" in rows[2]["reason"]


def test_sh_needs_every_line_at_an_edge_and_running(monkeypatch):
    _keep(monkeypatch)
    assert stage.running_line(FAX_HEAD) and stage.running_line(PT_FOOT)
    assert not stage.running_line("Patient called back, pain improved after the new dose")
    page = method.PageInputs(text="", pass_no=2)
    page.lines = [method.Line(FAX_HEAD, 0.01, 0.02), method.Line(PT_FOOT, 0.94, 0.95)]
    assert stage.edge_only(page, FAX_HEAD + "\n" + PT_FOOT)
    page.lines = [method.Line(FAX_HEAD, 0.01, 0.02), method.Line("Addendum", 0.5, 0.52), method.Line(PT_FOOT, 0.94, 0.95)]
    assert stage.edge_only(page, FAX_HEAD + "\nAddendum\n" + PT_FOOT) is None
    page.lines = [method.Line("Seen today, stable, continue plan", 0.02, 0.03)]
    assert stage.edge_only(page, "Seen today, stable, continue plan") is None


def test_sh_a_signed_edge_page_is_not_junked_by_it(monkeypatch):
    _keep(monkeypatch)
    texts = {1: "Progress note\n" + BODY, 2: FAX_HEAD + "\n" + PT_FOOT}
    lines = {1: [("x", 0.3, 0.5)], 2: [(FAX_HEAD, 0.01, 0.02), (PT_FOOT, 0.94, 0.95)]}
    rows = _run_lines(texts, lines, signed={2})
    assert "SH" not in (rows[2]["reason"] or "")


def test_s1s_ignores_a_date_in_a_running_footer(monkeypatch):
    """2026-11-23 (user): page 16 after a signed page, only its patient footer of its own (no line
    positions, so SH cannot decide): the footer's dates are no signer carried over."""
    _keep(monkeypatch)
    rows = _run({1: "Progress note\n" + BODY + "\nElectronically signed by Made Up MD 6/2/2025\nPatient: Madeup, Eli Page: 2",
                 2: PT_FOOT})
    assert "S1S" not in (rows[2]["reason"] or "")
    rows = _run({1: "Progress note\n" + BODY + "\nElectronically signed by Made Up MD 6/2/2025",
                 2: "Made Up Signer MD on 7/30/2025 at 1:12 PM"})
    assert "S1S" in rows[2]["reason"]


def test_rf_a_financial_summary_page_is_junk(monkeypatch):
    """2026-11-23 (user): a page headed "Financials" / "Financial Summary" is Junk / Others."""
    _keep(monkeypatch)
    assert method.financial_heading("Visit\n## Financial Summary\nCharges 120.00") == "Financial Summary"
    assert method.financial_heading("Financials") == "Financials"
    assert method.financial_heading("Discussed financial summary with patient") is None
    rows = _run({1: HEADER + BODY + FOOTER, 2: HEADER + "\n**Financials**\nGuarantor Balance 120.00\n" + BODY + FOOTER,
                 3: "Financial Summary\nCharges"})
    assert rows[2]["flag"] == "junk" and "RF" in rows[2]["reason"]
    assert rows[3]["flag"] == "junk" and "RF" in rows[3]["reason"]


def test_ra_a_page_whose_text_covers_under_5_percent_is_junk(monkeypatch):
    """2026-11-23 (user): text covering < 5% of the page, no other ink, nothing of its own signed
    -> Junk / Others; a picture / handwriting (ink), a signature or a provider keep it out of RA."""
    _keep(monkeypatch)
    words = [{"content": "From", "polygon": [10, 10, 900, 10, 900, 30, 10, 30]},
             {"content": "Patient", "polygon": [10, 1250, 900, 1250, 900, 1270, 10, 1270]}]
    area = method.content_area(words, 1000, 1300)
    assert 0.02 < area < 0.04
    assert method.content_area([], 1000, 1300) == 0.0
    page = method.PageInputs(text="Page 3\nPatient: Madeup DOB 09/07/1992", pass_no=2, document_type="printed",
                             content_area=area, page_ink=0.004)
    verdict = method.decide(page)
    assert verdict.rule == "RA" and verdict.code != method.CODE_MAIN
    for kw in ({"page_ink": 0.03}, {"signed": True}, {"provider": True}, {"content_area": 0.08}, {"page_ink": None}):
        other = method.PageInputs(**{**dict(text=page.text, pass_no=2, document_type="printed",
                                             content_area=area, page_ink=0.004), **kw})
        assert method.decide(other).rule != "RA", kw


RESULT_BLOCK = ("Result type: Orthopedic Progress Note\nResult date: January 30, 2024 9:17 EST\nResult status: Auth (Verified)\n"
                "Result title: Orthopedic Office/Clinic Note\nPerformed by: Madeup, Made A MD on February 20, 2024 14:01 EST\n"
                "Encounter info: 5665966, Made Up Clinic, 1/30/2024 - 1/30/2024\nContributor system: MADE_UP_SYSTEM\n"
                "Printed by: Madeup Person\nPrinted on: 6/20/2025 12:11 EDT")
RUN_HEAD = "Orthopedic Progress Note\n* Final Report *\nMadeup Person 1285711"


def test_own_lines_compare_the_same_ocr_engine(monkeypatch):
    """2026-11-23 (user): page 6 of 6 is the running header and page 5's result block: compared on
    one OCR engine (Docling vs Docling) it adds nothing, whatever another engine's lines look like."""
    _keep(monkeypatch)
    page5 = RUN_HEAD + "\n" + BODY + "\n" + RESULT_BLOCK + "\nPage 5 of 6"
    page6 = RUN_HEAD + "\n" + RESULT_BLOCK + "\nPage 6 of 6"
    joined6 = page6.replace("\nResult", " Result").replace("\nPerformed", " Performed")  # another engine's lines
    pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in (5, 6)]
    inputs = {i: method.PageInputs(text="", pass_no=2) for i in (5, 6)}
    inputs[5].content_bottom, inputs[6].content_bottom = 0.9, 0.12
    rows = stage._classify(pages, {5: page5, 6: joined6}, {5, 6}, inputs=inputs, signed_ids=set(), signatures={},
                           find_duplicates=True,
                           engine_texts={5: {"docling": page5, "azuredocintel": page5},
                                         6: {"docling": page6, "azuredocintel": joined6}})
    by = {row["page_id"]: row for row in rows}
    assert by[6]["flag"] == "junk", by[6]["reason"]


def test_rb_the_model_sure_of_blank_is_junk_others():
    """2026-11-23 (user): p(blank) > 95% -> Junk / Others, before every keep."""
    bert = {"p_keep": 0.01, "p_junk": 0.03, "p_blank": 0.96, "top_junk_label": ""}
    page = method.PageInputs(text="Signed by Made Up MD 1/2/2025\nPatient Madeup DOB 1/1/1950", pass_no=2,
                             document_type="printed", bert=bert, signed=True)
    verdict = method.decide(page)
    assert verdict.rule == "RB" and verdict.code != method.CODE_MAIN
    page.bert = {**bert, "p_blank": 0.94, "p_keep": 0.03}
    assert method.decide(page).rule != "RB"


def test_sn_a_short_page_of_only_continued_on_next_page_is_junk(monkeypatch):
    """2026-11-23 (user): "Content on Next Page", "Continued on the following page" ... -> Junk / Others."""
    _keep(monkeypatch)
    for note in ("Content on Next Page", "Continued on the following page", "See next page", "(Continued)",
                 "Report continues on next page", "Please turn over", "Continued on Following Page"):
        assert stage.NEXT_PAGE_NOTE.search(note), note
    assert not stage.NEXT_PAGE_NOTE.search("Patient will continue on the next dose of metformin")
    full = HEADER + "\n" + BODY + "\n" + FOOTER
    rows = _run({1: full, 2: HEADER + "\nContent on Next Page\n" + FOOTER, 3: full})
    assert rows[2]["flag"] == "junk" and "SN" in rows[2]["reason"], rows[2]["reason"]
