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
    assert (v.code, v.rule) == (m.CODE_LETTER_FAX, "FAX")


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


def test_fax_inside_a_note_is_not_junk_and_a_fax_letter_is():
    """2026-11-19 (user): "Fax to:" on a note's page (Progress Note headers, ICD codes, doses) is
    not junk; Fax + a letter's closing and no Electronic Signature is Junk / Letter/Fax."""
    assert _verdict(NOTE).code == m.CODE_MAIN
    letter = "Fax to: Records Dept\nPlease find the requested records enclosed.\nSincerely,\nMary Jones"
    v = _verdict(letter, headings=())
    assert (v.code, v.rule) == (m.CODE_LETTER_FAX, "FAX") and "letter" in v.reason
    assert _verdict(letter, headings=(), signed=True).code == m.CODE_MAIN
