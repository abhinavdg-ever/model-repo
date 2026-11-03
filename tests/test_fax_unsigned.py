"""2026-11-02 / 11-03 (user): a page kept as content with Fax in a section header (stage 6) and
no signature is Junk / Letter/Fax in pass 2 (blank_junk/method.fax_unsigned)."""
import stages.lib.blank_junk.stage  # noqa: F401  (puts the blank/junk helpers on sys.path)
from stages.lib.blank_junk import method as m

TEXT = "FAX: 555-1234 To: Dr Smith\nProgress Note\nHPI: cough\nPlan: rest\n"
HEADERS = ("FAX COVER SHEET", "Progress Note")


def _verdict(text, **kw):
    kw.setdefault("headings", HEADERS)
    inputs = m.PageInputs(text=text, pass_no=kw.pop("pass_no", 2), **kw)
    return m.post_process(m.Verdict(m.CODE_MAIN, 0.9, "R4", "kept"), inputs)


def test_an_unsigned_page_with_fax_in_a_section_header_is_junk():
    v = _verdict(TEXT)
    assert (v.code, v.rule) == (m.CODE_LETTER_FAX, "FAX")


def test_a_signed_or_signature_mentioning_page_stays_main():
    assert _verdict(TEXT, signed=True).code == m.CODE_MAIN
    assert _verdict(TEXT + "Electronically signed by Dr X\n").code == m.CODE_MAIN


def test_fax_not_in_a_section_header_or_in_pass_1_does_nothing():
    assert _verdict(TEXT, headings=("Progress Note", "Plan")).code == m.CODE_MAIN  # fax only in the text
    assert _verdict(TEXT, headings=()).code == m.CODE_MAIN  # no section headers
    assert _verdict(TEXT, pass_no=1).code == m.CODE_MAIN
