"""2026-11-02 / 11-03 (user): a page kept as content with Fax in a section header (stage 6) and
no signature is Junk / Letter/Fax in pass 2 (blank_junk/method.fax_unsigned)."""
import modules.blank_junk.stage  # noqa: F401  (puts the blank/junk helpers on sys.path)
from modules.blank_junk import method as m

TEXT = "FAX: 555-1234 To: Dr Smith\nProgress Note\nRe: records you asked for\nPlease see the pages attached\n"
NOTE = "FAX: 555-1234 To: Dr Smith\nProgress Note\nHPI: cough\nPlan: rest\n"
HEADERS = ("FAX COVER SHEET", "Progress Note")


def _verdict(text, **kw):
    kw.setdefault("headings", HEADERS)
    keep = {"flag": "KEEP", "subclass": "", "confidence": 0.9, "decision_reason": "argmax",
            "p_keep": 0.9, "p_blank": 0.0, "p_junk": 0.1}
    inputs = m.PageInputs(text=text, pass_no=kw.pop("pass_no", 2),
                          bert={"p_keep": 0.9, "p_junk": 0.1, "p_blank": 0.0, "service": keep}, **kw)
    return m.post_process(m.decide(inputs), inputs)


def test_an_unsigned_page_with_fax_in_a_section_header_is_junk():
    v = _verdict(TEXT)
    assert (v.code, v.rule) == (m.CODE_LETTER_FAX, "G8")


def test_only_an_electronic_signature_keeps_a_fax_page():
    """2026-11-07 (user): kv_extract's Electronic Signature keeps it; signature words
    ("Orders for Signature", "Electronically signed by" in the text) do not."""
    assert _verdict(TEXT, signed=True).code == m.CODE_MAIN
    assert _verdict(TEXT + "Re: Orders for Signature\n").code != m.CODE_MAIN
    assert _verdict(TEXT + "Electronically signed by Dr X\n").code != m.CODE_MAIN


def test_fax_not_in_a_section_header_or_in_pass_1_does_nothing():
    assert _verdict(TEXT, headings=("Progress Note", "Plan")).code == m.CODE_MAIN  # fax only in the text
    assert _verdict(TEXT, headings=()).code == m.CODE_MAIN  # no section headers
    assert _verdict(TEXT, pass_no=1).code == m.CODE_MAIN


LETTER = "Fax to: Records Dept\nPlease find the requested records enclosed.\nSincerely,\nMary Jones"


def test_the_fax_letter_closing_rule_is_gone():
    """2026-11-20 (user): Fax + a letter's closing no longer junks a page: the model decides."""
    assert _verdict(LETTER, headings=()).code == m.CODE_MAIN
    v = _verdict(LETTER, headings=("Fax",))  # a Fax header and no signature: Junk / Letter/Fax
    assert (v.code, v.rule) == (m.CODE_LETTER_FAX, "G8")
    assert _verdict(LETTER, headings=("Fax",), signed=True).code == m.CODE_MAIN


def test_a_fax_header_between_progress_notes_goes_to_the_model():
    """2026-11-20 (user): "To: ... Fax" on a page that is not short with a Progress Note page on
    each side: no fax rule, the model decides."""
    v = _verdict(TEXT, flanked_by_notes=True)
    assert v.code == m.CODE_MAIN and "the model decides" in str(v.votes.get("fax_unsigned"))
    assert _verdict(TEXT, flanked_by_notes=False).code == m.CODE_LETTER_FAX


def test_no_rule_junks_a_long_page_the_model_keeps_at_99():
    """2026-11-19 (user): on a page that is not short, a Fax / Cover junk rule stands only while
    p(keep) < 99%."""
    sure = {"flag": "KEEP", "subclass": "", "confidence": 0.995, "decision_reason": "argmax",
            "p_keep": 0.995, "p_blank": 0.0, "p_junk": 0.005}
    inputs = m.PageInputs(text=LETTER, pass_no=2, headings=("Fax",),
                          bert={"p_keep": 0.995, "p_junk": 0.005, "p_blank": 0.0, "service": sure})
    v = m.decide(inputs)
    assert v.code == m.CODE_MAIN and v.rule == "O3" and v.confidence == 0.995
    assert _verdict(LETTER, headings=("Fax",)).code == m.CODE_LETTER_FAX  # p(keep) 0.9: the rule stands


def test_a_fax_title_with_from_and_to_lines_is_a_cover_sheet():
    """2026-11-20 (user): "CONFIDENTIAL Fax" + FROM: + TO: (no routing boxes) is a fax cover sheet."""
    page = ("12-18-2025 2:08 PM ET athena 14807839071 pg 1 of 3\nCONFIDENTIAL Fax\nFROM:\nJOSE MENENDEZ, MD\n"
            "1331 N 7TH ST STE 405, PHOENIX, AZ 85006-2754\nPhone: (602) 254-3151\nFax: (833) 320-2754\nTO:\n"
            "ROBERT TRUESDALE MD\n(480) 783-9071\nThis fax may contain sensitive and confidential personal "
            "health information that is being sent for the sole use of the intended recipient.")
    assert m.fax_cover_sheet(page)
    v = _verdict(page, headings=())
    assert (v.code, v.rule) == (m.CODE_LETTER_FAX, "R4e") and v.votes.get("fax_cover")
    # A letter that only mentions a fax number is not one.
    assert m.fax_cover_sheet("Dear Dr Roe,\nPlease fax 330-555-0142 the records.\nSincerely") is None
