"""2026-11-02 (user): a page kept as content with Fax in its header and no signature is
Junk / Letter/Fax in pass 2 (blank_junk/method.fax_unsigned)."""
import stages.lib.blank_junk.stage  # noqa: F401  (puts the blank/junk helpers on sys.path)
from stages.lib.blank_junk import method as m

FAX = "FAX: 555-1234 To: Dr Smith\nProgress Note\nHPI: cough\nPlan: rest\n"


def _verdict(text, **kw):
    inputs = m.PageInputs(text=text, pass_no=kw.pop("pass_no", 2), **kw)
    return m.post_process(m.Verdict(m.CODE_MAIN, 0.9, "R4", "kept"), inputs)


def test_an_unsigned_fax_page_is_junk():
    v = _verdict(FAX)
    assert (v.code, v.rule) == (m.CODE_LETTER_FAX, "FAX")


def test_a_signed_or_signature_mentioning_page_stays_main():
    assert _verdict(FAX, signed=True).code == m.CODE_MAIN
    assert _verdict(FAX + "Electronically signed by Dr X\n").code == m.CODE_MAIN


def test_fax_outside_the_header_or_in_pass_1_does_nothing():
    assert _verdict("Progress Note\nHPI\nA\nB\nC\nD\nFax results to clinic\n").code == m.CODE_MAIN
    assert _verdict(FAX, pass_no=1).code == m.CODE_MAIN
    lines = [m.Line("Clinic", 0.02, 0.04), m.Line("Fax 555-1234", 0.5, 0.52)]
    assert _verdict("Clinic\nFax 555-1234\n", lines=lines).code == m.CODE_MAIN
