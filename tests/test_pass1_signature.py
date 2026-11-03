"""2026-11-03 (user): Tesseract can miss "Signed". A page pass 1 junked on DistilBERT alone
whose Final OCR 1 text shows a signature is not settled, so pass 2 can keep it."""
import db
from stages import _support


def test_a_model_junk_page_with_a_signature_in_final_ocr_1_is_not_settled(monkeypatch):
    monkeypatch.setattr(db, "non_printed_page_ids", lambda conn, chart: set())
    monkeypatch.setattr(db, "low_quality_page_ids", lambda conn, chart: set())
    monkeypatch.setattr(db, "get_blank_junk_flags", lambda conn, chart, pass_no: {1: "junk", 2: "junk", 3: "blank", 4: "junk"})
    monkeypatch.setattr(db, "get_blank_junk_reasons", lambda conn, chart, pass_no: {
        1: "R5 junk (final): p(junk) 0.97", 2: "R5 junk (final): p(junk) 0.98", 3: "R1 no text", 4: "R3 card"})
    monkeypatch.setattr(db, "get_ocr_texts", lambda conn, chart, kind: {
        1: "Signed Shannon McLean on 4/30/2026 at 6:08 PM", 2: "Invoice amount due", 3: "", 4: "Signature"})
    assert _support.pass1_ruled_out(None, 7) == {2, 3, 4}  # page 1 goes on to pass 2
