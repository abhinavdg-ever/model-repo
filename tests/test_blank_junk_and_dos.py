"""Blank/junk duplicate scoping and DOS date handling.

Duplicate detection runs after the model, among Main pages only: 25 pages or 10% of the chart ahead,
similarity = the average of the character match and the word match. ≥ 98% (or one
page's text wholly contained in the other's) is a duplicate; [95%, 98%) a partial
duplicate, which stays Main. Blank / junk / short pages are excluded from
comparison; on a match the higher character-count page stays the original
(earlier page on a tie). UI: duplicate → Yes, partial → Partial, else No.
"""
from __future__ import annotations

from stages.lib.blank_junk.stage import SUBTYPE_DB, _classify, _to_db_flag

import classify  # noqa: E402  — on sys.path once the blank_junk stage is imported
from stages.lib.dos.stage import _date_rows, _split_dates


def page(page_id: int, number: int):
    return {"id": page_id, "page_name": f"{number}.jpg", "page_number": number}


def _body(n: int = 6) -> str:
    return "Office visit note for the patient. Assessment and plan follow. " * n


class TestDuplicateScope:
    def test_duplicate_within_one_pass_is_found(self):
        pages = [page(1, 1), page(2, 2)]
        body = _body()
        texts = {1: body, 2: body}
        rows = _classify(pages, texts, {1, 2})
        flags = {r["page_id"]: r["flag"] for r in rows}
        assert flags[1] == "not_blank_junk"
        assert flags[2] == "duplicate"
        assert next(r for r in rows if r["page_id"] == 2)["duplicate_of"] == 1

    def test_duplicate_of_a_page_judged_in_an_earlier_pass_is_found(self):
        """Pass 2 can still match a neighbor that was main in pass 1."""
        body = _body()
        pages = [page(1, 1), page(2, 2)]
        rows1 = _classify(pages, {1: body, 2: "x"}, {1})
        assert rows1[0]["flag"] == "not_blank_junk"
        rows2 = _classify(
            pages, {1: body, 2: body}, {2}, prior_main_ids={1}
        )
        assert rows2[0]["flag"] == "duplicate"
        assert rows2[0]["duplicate_of"] == 1

    def test_a_page_is_never_a_duplicate_of_itself(self):
        body = _body()
        again = _classify([page(1, 1)], {1: body}, {1})
        assert again[0]["flag"] == "not_blank_junk"

    def test_equal_length_keeps_earlier_as_original(self):
        pages = [page(1, 1), page(2, 2), page(3, 3)]
        body = _body()
        texts = {1: body, 2: body, 3: body}
        rows = _classify(pages, texts, {1, 2, 3})
        by_id = {r["page_id"]: r for r in rows}
        assert by_id[1]["flag"] == "not_blank_junk"
        assert by_id[2]["duplicate_of"] == 1
        # page 3 is within ±2 of page 1 and page 2
        assert by_id[3]["flag"] == "duplicate"
        assert by_id[3]["duplicate_of"] == 1

    def test_a_near_identical_later_page_is_the_duplicate_even_if_longer(self):
        # 2026-10-22 (user): contained, fewer than 10 words different -> the later page.
        pages = [page(1, 1), page(2, 2)]
        long = _body(8)
        short = long[:-3]
        rows = _classify(pages, {1: short, 2: long}, {1, 2})
        by_id = {r["page_id"]: r for r in rows}
        assert by_id[1]["flag"] == "not_blank_junk"
        assert by_id[2]["flag"] == "duplicate"
        assert by_id[2]["duplicate_of"] == 1

    def test_pages_more_than_twenty_five_apart_are_not_compared(self):
        pages = [page(i, i) for i in range(1, 28)]
        body = _body()
        # page 1 and page 27 are identical but 26 steps apart (> ±25)
        texts = {i: f"Unrelated progress note number {i} with its own findings. " * 6 for i in range(2, 27)}
        texts[1] = texts[27] = body
        rows = _classify(pages, texts, set(range(1, 28)))
        flags = {r["page_id"]: r["flag"] for r in rows}
        assert flags[1] == "not_blank_junk"
        assert flags[27] == "not_blank_junk"

    def test_the_window_grows_with_the_chart(self):
        from stages.lib.blank_junk.classify import duplicate_window

        assert [duplicate_window(n) for n in (1, 10, 26, 100, 250, 251, 400, 1000)] == [0, 9, 25, 25, 25, 26, 40, 100]

    def test_a_long_chart_compares_pages_ten_percent_apart(self):
        # 300 pages: the window is 30, so pages 1 and 31 (30 apart) are compared.
        pages = [page(i, i) for i in range(1, 301)]
        texts = {i: f"Unrelated progress note number {i} with its own findings. " * 6 for i in range(1, 301)}
        texts[1] = texts[31] = _body()
        rows = _classify(pages, texts, set(range(1, 301)))
        flags = {r["page_id"]: r["flag"] for r in rows}
        assert flags[31] == "duplicate"

    def test_pages_twenty_five_apart_are_compared(self):
        pages = [page(i, i) for i in range(1, 27)]
        body = _body()
        texts = {i: f"Unrelated progress note number {i} with its own findings. " * 6 for i in range(2, 26)}
        texts[1] = texts[26] = body
        rows = _classify(pages, texts, set(range(1, 27)))
        flags = {r["page_id"]: r["flag"] for r in rows}
        assert "duplicate" in (flags[1], flags[26])

    def test_pages_five_apart_are_compared(self):
        pages = [page(i, i) for i in range(1, 7)]
        body = _body()
        texts = {i: f"Unrelated progress note number {i} with its own findings. " * 6 for i in range(2, 6)}
        texts[1] = texts[6] = body
        rows = _classify(pages, texts, set(range(1, 7)))
        flags = {r["page_id"]: r["flag"] for r in rows}
        assert flags[6] == "duplicate"

    def test_similarity_below_threshold_is_not_duplicate(self):
        pages = [page(1, 1), page(2, 2)]
        a = ("Alpha clinical history. " * 20)
        b = ("Beta surgical findings. " * 20)
        rows = _classify(pages, {1: a, 2: b}, {1, 2})
        # Blank/junk is the model's call; this test is only about duplicates.
        for row in rows:
            assert row["flag"] != "duplicate"
            assert row["duplicate_of"] is None

    def test_short_identical_pages_are_not_compared(self):
        pages = [page(1, 1), page(2, 2)]
        short = "Office visit note. Assessment and plan follow. " * 2
        rows = _classify(pages, {1: short, 2: short}, {1, 2})
        for row in rows:
            assert row["flag"] != "duplicate"

    def test_page_not_main_in_an_earlier_pass_is_not_compared(self):
        """A prior-pass blank/junk page is never a duplicate original."""
        body = _body()
        pages = [page(1, 1), page(2, 2)]
        rows = _classify(pages, {1: body, 2: body}, {2}, prior_main_ids=set())
        assert rows[0]["flag"] != "duplicate"
        assert rows[0]["duplicate_of"] is None

    def test_blank_and_short_pages_are_not_compared(self):
        pages = [page(1, 1), page(2, 2)]
        body = _body()
        rows = _classify(pages, {1: body, 2: "   "}, {1, 2})
        by_id = {r["page_id"]: r for r in rows}
        assert by_id[1]["flag"] == "not_blank_junk"
        assert by_id[2]["flag"] == "blank"

    def test_only_requested_pages_are_classified(self):
        pages = [page(1, 1), page(2, 2)]
        texts = {1: "alpha content here " * 10, 2: "beta content here " * 10}
        rows = _classify(pages, texts, {2})
        assert [r["page_id"] for r in rows] == [2]

    def test_blank_page_is_flagged_blank(self):
        rows = _classify([page(1, 1)], {1: "   "}, {1})
        assert rows[0]["flag"] == "blank"


class TestClinicalNotJunk:
    """Clinical progress notes must not become Letter/Fax or Invoice junk."""

    def test_progress_note_with_confidentiality_footer_is_main(self):
        import sys
        from pathlib import Path

        junk = Path("core-pipeline/stages/lib/blank_junk").resolve()
        if str(junk) not in sys.path:
            sys.path.insert(0, str(junk))
        from classify import CLASSIFICATION_LABELS, CODE_MAIN, classify_text

        text = (
            "Progress Note\n"
            "Reason for Appointment: 6 month follow up.\n"
            "History of Present Illness: Patient presents with SOB.\n"
            "Current Medications: Amlodipine, Apixaban.\n"
            "Vital Signs: BP 132/64. Assessment: Hypertension.\n"
            "CONFIDENTIALITY NOTICE: This transmission is intended only "
            "for the intended recipient and may contain confidential "
            "medical records.\n"
        )
        code, reason = classify_text(text)
        assert code == CODE_MAIN
        assert reason == "clinical_content"
        assert CLASSIFICATION_LABELS[code] == "Main"

    def test_short_fax_cover_still_junk(self):
        import sys
        from pathlib import Path

        junk = Path("core-pipeline/stages/lib/blank_junk").resolve()
        if str(junk) not in sys.path:
            sys.path.insert(0, str(junk))
        from classify import CLASSIFICATION_LABELS, classify_text

        text = "Fax cover sheet\nThis fax is for the intended recipient only.\n"
        code, reason = classify_text(text)
        assert CLASSIFICATION_LABELS[code] == "Letter/Fax"
        assert reason == "letter_fax"

    def test_fax_footer_alone_is_letter_fax_not_record_request(self):
        """Bare confidentiality footers must not become Record Request junk."""
        import sys
        from pathlib import Path

        junk = Path("core-pipeline/stages/lib/blank_junk").resolve()
        if str(junk) not in sys.path:
            sys.path.insert(0, str(junk))
        from classify import CLASSIFICATION_LABELS, classify_text

        text = (
            "CONFIDENTIALITY NOTICE: This fax transmission is intended "
            "only for the intended recipient. If you are not the intended "
            "recipient please destroy this transmission.\n"
        )
        code, reason = classify_text(text)
        assert CLASSIFICATION_LABELS[code] == "Letter/Fax"
        assert reason == "letter_fax"


class TestModelBridge:
    """DistilBERT's probabilities feed the vote; TF-IDF is not read."""

    @staticmethod
    def _bridge():
        import sys

        from conftest import LIB

        junk = str(LIB / "blank_junk")
        if junk not in sys.path:
            sys.path.insert(0, junk)
        import model_bridge

        return model_bridge

    def test_model_dir_comes_from_config(self, monkeypatch, tmp_path):
        import config

        bridge = self._bridge()
        monkeypatch.setattr(config, "BLANK_JUNK_MODEL_DIR", tmp_path)
        status = bridge.model_status()
        assert status["path"] == str(tmp_path / "bert")
        assert (status["ready"], status["engine"]) == (False, "keywords")
        assert "BERT folder missing" in status["reason"]

    def test_scores_are_distilbert_only(self, monkeypatch):
        bridge = self._bridge()
        monkeypatch.setattr(bridge, "_load_model", lambda: None)
        assert bridge.model_scores("Invoice Number 123 Amount Due") == {"bert": None}

    def test_vendored_src_package_does_not_stay_on_sys_path(self):
        import sys

        bridge = self._bridge()
        bridge._load_model()
        assert str(bridge._VENDOR) not in sys.path

    def test_health_reports_the_model_without_loading_it(self):
        status = self._bridge().model_status()
        assert status["model_version"]
        # Without the BERT folder or torch/transformers the vote runs on keywords
        # alone, and /health says so.
        if status["engine"] == "bert":
            assert status["ready"] is True and status["reason"] is None
        else:
            assert status["engine"] == "keywords" and status["ready"] is False
            assert "keywords alone" in status["reason"]


class TestSubtypeMapping:
    def test_junk_always_gets_a_legal_subtype(self):
        """JUNK_CODES includes CODE_BLANK; blank is its own flag and takes
        precedence, so only the remaining codes become flag='junk'."""
        from classify import CODE_BLANK, CODE_JUNK, JUNK_CODES

        for code in JUNK_CODES - {CODE_BLANK, CODE_JUNK}:
            flag, subtype = _to_db_flag(code)
            assert flag == "junk"
            assert subtype in set(SUBTYPE_DB.values())
        # Plain Junk (a blank page with text) has no sub-type.
        assert _to_db_flag(CODE_JUNK) == ("junk", None)

    def test_blank_wins_over_junk_even_though_it_is_in_junk_codes(self):
        from classify import CODE_BLANK, JUNK_CODES

        assert CODE_BLANK in JUNK_CODES
        flag, subtype = _to_db_flag(CODE_BLANK)
        assert flag == "blank"
        assert subtype is None

    def test_non_junk_has_no_subtype(self):
        from classify import CODE_BLANK, CODE_DUPLICATE, CODE_MAIN

        for code in (CODE_BLANK, CODE_DUPLICATE, CODE_MAIN):
            _flag, subtype = _to_db_flag(code)
            assert subtype is None


class TestDosDates:
    def test_single_date_makes_one_row(self):
        rows = _date_rows({"dos_from": "03-14-2024", "dos_to": "03-14-2024"})
        assert len(rows) == 1
        assert rows[0]["dos_from"] == "2024-03-14"
        assert rows[0]["dos_to"] == "2024-03-14"

    def test_multi_date_page_makes_one_row_per_date(self):
        """v6's single from/to columns could hold only one pair, while the CSV
        contract already allowed comma-separated lists."""
        rows = _date_rows(
            {"dos_from": "03-14-2024, 04-02-2024", "dos_to": "03-14-2024, 04-02-2024"}
        )
        assert len(rows) == 2
        assert [r["dos_from"] for r in rows] == ["2024-03-14", "2024-04-02"]

    def test_range_without_matching_to_list_reuses_the_last(self):
        rows = _date_rows({"dos_from": "03-14-2024, 04-02-2024", "dos_to": "04-05-2024"})
        assert len(rows) == 2
        assert rows[1]["dos_to"] == "2024-04-05"

    def test_no_dates_makes_no_rows(self):
        assert _date_rows({"dos_from": "", "dos_to": ""}) == []
        assert _date_rows({}) == []

    def test_keyword_is_carried_through(self):
        rows = _date_rows(
            {"dos_from": "03-14-2024", "dos_to": "03-14-2024", "keyword": "Date Of Service"}
        )
        assert rows[0]["source_keyword"] == "Date Of Service"

    def test_split_dates_trims(self):
        assert _split_dates(" 01-01-2024 ,02-02-2024 ") == ["01-01-2024", "02-02-2024"]


class TestDosStageWritesEachPageOnce:
    def test_found_dates_are_not_overwritten_and_pages_count_once(self, monkeypatch, tmp_path):
        from contextlib import contextmanager, nullcontext
        from types import SimpleNamespace

        import stages.lib.dos.stage as dos_stage

        pages = [page(1, 1), page(2, 2), page(3, 3)]
        ctx = SimpleNamespace(chart_name="c", pages=pages, todo={1, 2, 3}, done=0, skipped=0)
        written: dict[int, list] = {}
        completed: list[int] = []

        @contextmanager
        def fake_stage_run(*_a, **_k):
            yield ctx

        def fake_upsert(_conn, *, page_id, date_of_service_from, **_k):
            written.setdefault(page_id, []).append(date_of_service_from)

        def fake_mark_completed(_conn, _ctx, page_id):
            completed.append(page_id)
            _ctx.done += 1

        monkeypatch.setattr(dos_stage, "stage_run", fake_stage_run)
        monkeypatch.setattr(dos_stage, "connect", lambda: nullcontext(None))
        monkeypatch.setattr(dos_stage, "get_blank_junk_flags", lambda *a, **k: {})
        monkeypatch.setattr(dos_stage, "get_page_classification_map", lambda *a, **k: {})
        monkeypatch.setattr(dos_stage, "get_ocr_texts", lambda *a, **k: {})
        monkeypatch.setattr(dos_stage, "mark_skipped", lambda *a, **k: None)
        monkeypatch.setattr(dos_stage, "_combined_text", lambda *a, **k: ("", {}))
        monkeypatch.setattr(
            dos_stage,
            "detect_dos_per_page",
            lambda *a, **k: [
                {"page_name": "1.jpg", "dos_from_iso": "2024-03-14"},
                {"page_name": "2.jpg", "dos_from_iso": "2024-04-01"},
            ],
        )
        monkeypatch.setattr(dos_stage, "upsert_dos", fake_upsert)
        monkeypatch.setattr(dos_stage, "mark_completed", fake_mark_completed)
        monkeypatch.setattr(dos_stage, "imaging_csv", lambda *a: tmp_path / "dos.csv")
        monkeypatch.setattr(dos_stage, "write_csv", lambda path, *a: path)

        dos_stage.run(7)

        assert written == {1: ["2024-03-14"], 2: ["2024-04-01"], 3: [None]}
        assert sorted(completed) == [1, 2, 3]
        assert ctx.done == 3


def _pages(*bodies, **kwargs):
    from dos_logic import detect_dos_per_page

    text = "".join(f"===== {i}.jpg =====\n{body}\n" for i, body in enumerate(bodies, 1))
    return detect_dos_per_page(text, **kwargs)


FILLER = " ".join(f"word{i}" for i in range(40))


class TestDosTesseractOnlyWithoutFinal2:
    def test_tesseract_is_offered_only_for_pages_without_final2(self, monkeypatch):
        import stages.lib.dos.stage as dos_stage

        texts = {
            "azuredocintel": {1: "Final2 text"},
            "docling": {1: "Final1 text", 2: "Final1 text"},
            "tesseract": {1: "Encounter Date: 01/01/2024", 2: "Encounter Date: 02/02/2024",
                          3: "Encounter Date: 03/03/2024"},
        }
        monkeypatch.setattr(dos_stage, "get_ocr_texts", lambda _c, _id, kind: texts[kind])
        monkeypatch.setattr(dos_stage, "get_quality_map", lambda *_a: {3: {"printed_or_handwritten": "handwritten"}})
        pages = [{"id": n, "page_name": f"{n}.jpg"} for n in (1, 2, 3)]
        _text, tesseract = dos_stage._combined_text(None, 7, pages, {1, 2, 3})
        # 1 has Final2; 3 is handwritten, so its Tesseract text is not trusted.
        assert tesseract == {"2.jpg": "Encounter Date: 02/02/2024"}


class TestDosEdgeKeyword:
    """Rule 1: a DOS label in the first or last 20 words, with its date."""

    def test_the_encounter_date_in_the_header_wins_over_the_hpi_admission(self):
        """The screenshot case: header Encounter Date, admit/discharge deep in the HPI."""
        from dos_logic import extract_dos_from_page_text

        text = (
            "Patient Name: Anderson, Lisa Date of Birth: 04/06/1993 Sex: F "
            "Encounter Id: 1092110 Encounter Date: May 20, 2025\n"
            f"{FILLER}\nAdmission date: 05/13/25 Discharge date: 05/14/25\n{FILLER}"
        )
        hit = extract_dos_from_page_text(text)
        assert (hit["dos_from"], hit["dos_to"]) == ("05-20-2025", "05-20-2025")
        assert hit["keyword"] == "Encounter Date"

    def test_a_label_in_the_last_20_words_counts(self):
        from dos_logic import extract_dos_from_page_text

        hit = extract_dos_from_page_text(f"{FILLER}\nVisit Date: 03/14/2024")
        assert hit["dos_from"] == "03-14-2024"

    def test_a_label_in_the_middle_of_the_page_does_not(self):
        from dos_logic import extract_dos_from_page_text

        assert extract_dos_from_page_text(f"{FILLER}\nVisit Date: 03/14/2024\n{FILLER}") is None

    def test_admission_is_from_and_discharge_is_to(self):
        from dos_logic import extract_dos_from_page_text

        hit = extract_dos_from_page_text(
            f"Admission Date: 03/01/2024\n{FILLER}\nDischarge Date: 03/05/2024"
        )
        assert (hit["dos_from"], hit["dos_to"]) == ("03-01-2024", "03-05-2024")
        assert hit["match_type"] == "admit_discharge_pair"

    def test_a_discharge_date_alone_is_both_ends(self):
        from dos_logic import extract_dos_from_page_text

        hit = extract_dos_from_page_text(f"{FILLER}\nDischarged on 03/05/2024")
        assert (hit["dos_from"], hit["dos_to"]) == ("03-05-2024", "03-05-2024")

    def test_an_encounter_label_beats_admit_and_discharge(self):
        from dos_logic import extract_dos_from_page_text

        hit = extract_dos_from_page_text("Admit Date: 03/01/2024 Visit Date: 03/03/2024")
        assert hit["dos_from"] == "03-03-2024"

    def test_filler_words_between_label_and_date_are_allowed(self):
        from dos_logic import extract_dos_from_page_text

        assert extract_dos_from_page_text("Encounter Date | at January 28, 2025")["dos_from"] == "01-28-2025"
        assert extract_dos_from_page_text("Visit Date/Time: 03/14/2024 10:00")["dos_from"] == "03-14-2024"

    def test_a_label_never_takes_the_next_fields_date(self):
        from dos_logic import extract_dos_from_page_text

        assert extract_dos_from_page_text("Visit Date: | DOB: 01/02/1960") is None

    def test_an_unlabelled_date_is_not_a_dos(self):
        from dos_logic import extract_dos_from_page_text

        assert extract_dos_from_page_text("Ph: 801.568.0200\n12/31/2025 | Hoopes Vision") is None

    def test_a_written_range_keeps_both_ends(self):
        from dos_logic import extract_dos_from_page_text

        hit = extract_dos_from_page_text("DOS: 03/01/2024 to 03/05/2024")
        assert (hit["dos_from"], hit["dos_to"]) == ("03-01-2024", "03-05-2024")

    def test_the_four_date_shapes(self):
        from dos_logic import extract_dos_from_page_text

        for raw in ("12/31/2025", "2025-12-31", "December 31, 2025", "31 December 2025"):
            assert extract_dos_from_page_text(f"Date of Service: {raw}")["dos_from"] == "12-31-2025"

    def test_impossible_days_are_not_dates(self):
        from dos_logic import extract_dos_from_page_text

        assert extract_dos_from_page_text("Date of Service: 02/30/2024") is None


class TestDosDriver:
    def test_each_page_keeps_its_own_date_nothing_is_carried(self):
        hits = _pages("Office Visit  Date of Service: 03/14/2024", "Continued note with no date on it.")
        assert hits[0]["dos_from"] == "03-14-2024"
        assert hits[0]["final_dos"] == "2024-03-14"
        assert hits[1]["dos_from"] == ""
        assert hits[1]["final_dos"] == ""
        assert hits[1]["match_type"] == "no_date_found"

    def test_a_page_with_no_date_reports_the_default_at_document_level(self):
        from dos_logic import profile

        hit = _pages("No dates whatsoever on this page.")[0]
        assert hit["confidence"] == 0
        assert hit["is_default"] is True
        assert hit["doc_dos_from_iso"] == profile().default_date

    def test_iso_columns_are_actually_iso(self):
        hit = _pages("Date of Service: 03/14/2024\nVisit note.")[0]
        assert hit["dos_from_iso"] == "2024-03-14"
        assert hit["doc_dos_from_iso"] == "2024-03-14"
        assert hit["match_type"] == "edge_keyword"
        assert hit["page_source"] == "edge"

    def test_tesseract_top_and_bottom_fill_in_when_the_final_text_dropped_the_banner(self):
        """Docling can drop the page-header banner; Tesseract keeps it."""
        final = "Reason for Visit\nFollow Up\nChief Complaint\nFollow Up\nAssessment"
        tesseract = ("Patient Name: Anderson, Lisa Date of Birth: 04/05/1993 Sex:F "
                     "Encounter Id:1220216 Encounter Date: December 03, 2025\n" + final)
        hit = _pages(final, tesseract_texts={"1.jpg": tesseract})[0]
        assert hit["dos_from_iso"] == "2025-12-03"
        assert hit["match_type"] == "edge_keyword_tesseract"

    def test_the_final_text_comes_before_tesseract(self):
        hit = _pages("Visit Date: 03/14/2024",
                     tesseract_texts={"1.jpg": "Visit Date: 03/15/2024"})[0]
        assert (hit["dos_from_iso"], hit["page_source"]) == ("2024-03-14", "edge")

    def test_the_key_value_date_comes_before_tesseract(self):
        kv = {"1.jpg": [{"iso": "2024-06-01", "tier": "weak", "keyword": "Date"}]}
        hit = _pages("no label here", kv_dates=kv,
                     tesseract_texts={"1.jpg": "Encounter Date: 03/15/2024"})[0]
        assert (hit["dos_from_iso"], hit["page_source"]) == ("2024-06-01", "kv")

    def test_tesseract_is_read_when_there_is_no_key_value_date(self):
        hit = _pages("no label here", tesseract_texts={"1.jpg": "Encounter Date: 03/15/2024"})[0]
        assert (hit["dos_from_iso"], hit["page_source"]) == ("2024-03-15", "edge_tesseract")

    def test_candidate_log_has_every_edge_label(self):
        log: list = []
        _pages("Admit Date: 03/01/2024 Visit Date: 03/03/2024", candidate_log=log)
        assert [(r["label_class"], r["iso"], r["chosen"]) for r in log] == [
            ("admit", "2024-03-01", False), ("encounter", "2024-03-03", True),
        ]


def test_ccd_visit_page_dos_is_the_encounter_date():
    from pathlib import Path

    from dos_logic import extract_dos_from_page_text

    text = (Path(__file__).parent / "fixtures" / "ccd_visit_page.txt").read_text(encoding="utf-8")
    assert extract_dos_from_page_text(text)["dos_from"] == "01-28-2025"


class TestRegexFallbackClinicalContent:
    """Regex fallback: Main only with > 25 words and 2+ clinical section headings."""

    @staticmethod
    def _clinical(text):
        from text_utils import looks_like_clinical_content

        return looks_like_clinical_content(text)

    def test_two_headings_over_25_words(self):
        text = "Chief Complaint: cough\nAssessment: bronchitis\n" + "word " * 25
        assert self._clinical(text)

    def test_one_heading_on_a_long_page_is_not_enough(self):
        text = "Assessment: bronchitis\n" + "word " * 120
        assert not self._clinical(text)

    def test_25_words_or_fewer_is_not_enough(self):
        text = "Chief Complaint: cough\nAssessment: bronchitis\n" + "word " * 19
        assert not self._clinical(text)

    def test_clinical_words_in_sentences_do_not_count(self):
        text = "The patient was seen for a follow up and the assessment and plan were discussed. " * 4
        assert not self._clinical(text)


def test_votes_are_staged_beside_the_chart_and_each_pass_keeps_its_own(tmp_path, monkeypatch):
    import json

    from stages.lib.blank_junk import stage as bj_stage

    monkeypatch.setattr(bj_stage, "staging_dir", lambda name: tmp_path / name / "staging")
    row = {"page_name": "1.jpg", "label": "Main", "confidence": 0.9, "reason": "R7", "flag": "not_blank_junk",
           "votes": {"rule": "R7", "bert": {"p_junk": 0.1, "p_keep": 0.9}, "empty": False, "card_keywords": []}}
    bj_stage._write_votes("c", 1, [row])
    bj_stage._write_votes("c", 2, [{**row, "reason": "R3", "votes": {**row["votes"], "rule": "R3"}}])
    bj_stage._write_votes("c", 2, [{**row, "reason": "R9", "votes": {**row["votes"], "rule": "R9"}}])
    staging = tmp_path / "c" / "staging"
    detail = json.loads((staging / "blank_junk_detail.json").read_text(encoding="utf-8"))
    assert detail["pages"]["1.jpg"]["pass_1"]["rule"] == "R7"
    assert detail["pages"]["1.jpg"]["pass_2"]["rule"] == "R9"
    simple = json.loads((staging / "blank_junk.json").read_text(encoding="utf-8"))
    page = simple["pages"]["1.jpg"]
    assert (page["pass_1"]["rule"], page["pass_2"]["rule"]) == ("R7", "R9")
    assert page["pass_2"]["p_junk"] == 0.1 and page["pass_2"]["result"] == "Main"
    assert page["final"] == {"decision": "Main", "rule": "R9", "why": "R9", "pass": 2}


def test_blank_junk_json_is_one_simple_block_per_page(tmp_path, monkeypatch):
    """2026-10-23 (user): per pass p(junk), p(keep), empty, where the text ends, card
    words, result, rule, the duplicate status (closest page from 70%), and the final."""
    import json

    from stages.lib.blank_junk import stage as bj_stage

    monkeypatch.setattr(bj_stage, "staging_dir", lambda name: tmp_path / name / "staging")
    votes = {"rule": "R5", "bert": {"p_junk": 0.02, "p_keep": 0.98}, "empty": False, "card_keywords": []}
    settled = {"page_name": "1.jpg", "label": "Main", "confidence": 0.98, "flag": "not_blank_junk",
               "reason": "R5 keep (final): sure", "votes": votes, "best_match": {"page_name": "2.jpg", "similarity": 0.62}}
    copy = {"page_name": "3.jpg", "label": "Duplicate", "confidence": 0.99, "flag": "duplicate",
            "reason": "duplicate_of_page_id:9", "votes": None, "best_match": {"page_name": "2.jpg", "similarity": 0.99}}
    card = {"page_name": "4.jpg", "label": "Others", "confidence": 0.9, "flag": "junk", "reason": "R3 card",
            "votes": {**votes, "rule": "R3", "card_keywords": ["driver's license"],
                      "short_page": {"content_bottom": 0.4}}}
    bj_stage._write_votes("c", 1, [settled])
    bj_stage._write_votes("c", 2, [copy, card])
    pages = json.loads((tmp_path / "c" / "staging" / "blank_junk.json").read_text(encoding="utf-8"))["pages"]
    assert pages["1.jpg"]["pass_2"] == "not judged: settled in pass 1"
    assert pages["1.jpg"]["pass_1"]["duplicate"] == {"status": "No"}  # 62%: below 70%, no closest
    assert pages["1.jpg"]["final"]["pass"] == 1
    assert pages["3.jpg"]["pass_2"]["duplicate"] == {"status": "Duplicate", "closest_page": "2.jpg", "similarity": 0.99}
    assert pages["3.jpg"]["final"]["decision"] == "Duplicate"
    assert pages["4.jpg"]["pass_2"]["card_keywords"] == ["driver's license"]
    assert pages["4.jpg"]["pass_2"]["text_ends_at"] == 0.4


def _prose(seed: int, words: int = 420) -> list[str]:
    """A page of prose-like text: Zipf-weighted words, so pages share common words."""
    import random

    rng = random.Random(seed)
    vocab = [f"w{i}" for i in range(3000)]
    weights = [1 / (i + 1) for i in range(3000)]
    return rng.choices(vocab, weights, k=words)


def _edited(words: list[str], changes: int) -> str:
    """The page with ``changes`` words replaced, spread through it."""
    out = list(words)
    for i in range(0, len(out), max(1, len(out) // changes))[:changes]:
        out[i] = f"edit{i}"
    return " ".join(out)


class TestDuplicatePageType:
    def test_a_duplicate_takes_its_originals_page_type(self, tmp_path, monkeypatch):
        import config
        import db
        from db import (
            connect,
            disable_skip_db_write,
            enable_skip_db_write,
            init_page_stages,
            list_pages,
            upsert_blank_junk,
            upsert_chart,
            upsert_ocr_result,
            upsert_pages,
        )
        from stages.lib.page_classify import stage as classify_stage

        monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
        monkeypatch.setattr(classify_stage, "connect", db.connect)
        store = enable_skip_db_write(reset=True)
        try:
            note = "Progress Note\nChief complaint: cough. Assessment and plan: follow up in two weeks. " * 8
            with connect() as conn:
                cid = int(upsert_chart(conn, chart_name="chart_dup", source="local")["id"])
                upsert_pages(conn, cid, [{"page_name": f"{n}.jpg", "page_number": n} for n in (1, 2)])
                first, second = (p["id"] for p in list_pages(conn, cid))
                init_page_stages(conn, cid)
                for pid in (first, second):
                    upsert_ocr_result(conn, chart_id=cid, page_id=pid, ocr_type="azuredocintel", raw_text=note)
                # The duplicate comes first in the chart; its original is page 2.
                upsert_blank_junk(conn, chart_id=cid, page_id=first, blank_junk_flag="duplicate",
                                  pass_no=2, ocr_source="azuredocintel", duplicate_of_page_id=second,
                                  confidence=0.99, is_final=True)
                upsert_blank_junk(conn, chart_id=cid, page_id=second, blank_junk_flag="not_blank_junk",
                                  pass_no=2, ocr_source="azuredocintel", is_final=True)
            classify_stage.run(cid, force=True)
            rows = {r["page_id"]: r for r in store.page_classifications.values()}
            assert rows[second]["page_type"]
            assert rows[first]["page_type"] == rows[second]["page_type"]
            assert rows[first]["page_subtype"] == rows[second]["page_subtype"]
            assert rows[first]["duplicate_flag"] is True
            assert rows[first]["classification_category"] == "non_codeable"
            assert rows[first]["decided_by"] == f"duplicate_of:{second}"
        finally:
            disable_skip_db_write()


class TestPass1SureJunk:
    """2026-10-22 (user): pages pass 1 settles (DistilBERT's sure junk among them) get
    Final OCR 1, but it re-grades nothing and no later stage reads them."""

    def test_pass1_ruled_out_lists_what_later_stages_skip(self):
        from db import (
            connect,
            disable_skip_db_write,
            enable_skip_db_write,
            list_pages,
            set_quality_tag,
            upsert_blank_junk,
            upsert_chart,
            upsert_pages,
            upsert_quality,
        )
        from stages._support import FINAL_JUNK, pass1_ruled_out

        enable_skip_db_write(reset=True)
        try:
            with connect() as conn:
                cid = int(upsert_chart(conn, chart_name="chart_p1", source="local")["id"])
                upsert_pages(conn, cid, [{"page_name": f"{n}.jpg", "page_number": n} for n in (1, 2, 3)])
                ids = [p["id"] for p in list_pages(conn, cid)]
                for pid, flag, reason in zip(ids, ("junk", "not_blank_junk", "blank"),
                                             (f"{FINAL_JUNK}: sure", "P1 deferred", "R1 blank")):
                    upsert_blank_junk(conn, chart_id=cid, page_id=pid, pass_no=1,
                                      blank_junk_flag=flag, ocr_source="tesseract", reason=reason)
                assert pass1_ruled_out(conn, cid) == {ids[0], ids[2]}
                # A low-quality page is pass 2's, whatever a pass-1 row says.
                upsert_quality(conn, chart_id=cid, page_id=ids[2], printed_or_handwritten="printed")
                set_quality_tag(conn, page_id=ids[2], quality_tag="low", quality_detail={},
                                review_required=True)
                assert pass1_ruled_out(conn, cid) == {ids[0]}
        finally:
            disable_skip_db_write()


class TestOnlyMainPagesAreCompared:
    """2026-10-22 (user): only Main pages take part in the duplicate check."""

    JUNK = {"bert": {"p_keep": 0.02, "p_junk": 0.98, "top_junk_label": "JUNK_FAX_TRANSMISSION"}}
    KEEP = {"bert": {"p_keep": 0.98, "p_junk": 0.02, "top_junk_label": ""}}

    def _run(self, monkeypatch, texts, junk_ids):
        import method
        from stages.lib.blank_junk import stage as bj_stage

        by_text = {texts[i]: (self.JUNK if i in junk_ids else self.KEEP) for i in texts}
        monkeypatch.setattr(bj_stage, "model_scores", lambda text, docling=None: by_text.get(text, self.KEEP))
        inputs = {i: method.PageInputs(text=texts[i], pass_no=2) for i in texts}
        rows = _classify([page(i, i) for i in texts], texts, set(texts), inputs=inputs)
        return {r["page_id"]: r for r in rows}

    def test_junk_pages_are_not_compared(self, monkeypatch):
        fax = " ".join(_prose(20))
        by = self._run(monkeypatch, {1: fax, 2: fax}, junk_ids={1, 2})
        assert [by[i]["flag"] for i in (1, 2)] == ["junk", "junk"]
        assert by[1]["duplicate_of"] is None and by[2]["duplicate_of"] is None

    def test_a_main_page_is_not_matched_to_a_junk_page(self, monkeypatch):
        body = " ".join(_prose(21))
        # One word apart: a duplicate between Main pages, and the stub tells them apart.
        by = self._run(monkeypatch, {1: body, 2: body + " faxed"}, junk_ids={2})
        assert (by[1]["flag"], by[1]["duplicate_of"]) == ("not_blank_junk", None)
        assert (by[2]["flag"], by[2]["duplicate_of"]) == ("junk", None)

    def test_two_main_pages_are(self, monkeypatch):
        body = " ".join(_prose(22))
        by = self._run(monkeypatch, {1: body, 2: body}, junk_ids=set())
        assert (by[2]["flag"], by[2]["duplicate_of"]) == ("duplicate", 1)


class TestDuplicateConfidenceStored:
    """2026-10-22 (user): every compared page stores its best similarity, which
    review-ui turns into the Duplicate confidence."""

    def test_every_compared_page_carries_its_best_similarity(self):
        words = _prose(30)
        texts = {1: " ".join(words), 2: _edited(words, 14), 3: " ".join(_prose(31)), 4: "   "}
        rows = {r["page_id"]: r for r in _classify([page(i, i) for i in texts], texts, set(texts))}
        sim = classify.text_similarity(texts[1], texts[2])
        assert abs(rows[1]["duplicate_confidence"] - round(sim, 4)) < 1e-9
        assert abs(rows[2]["duplicate_confidence"] - round(sim, 4)) < 1e-9
        # Never near 95%: the bound on its average, below the cut.
        assert rows[3]["duplicate_confidence"] < 0.95
        assert rows[4]["duplicate_confidence"] is None  # blank: never compared

    def test_the_store_keeps_it(self):
        from db import connect, disable_skip_db_write, enable_skip_db_write, upsert_blank_junk

        store = enable_skip_db_write(reset=True)
        try:
            with connect() as conn:
                upsert_blank_junk(conn, chart_id=1, page_id=7, pass_no=2, blank_junk_flag="not_blank_junk",
                                  ocr_source="azuredocintel", duplicate_confidence=0.9123)
            assert store.blank_junk[(7, 2)]["duplicate_confidence"] == 0.9123
        finally:
            disable_skip_db_write()


# An Epic After Visit Summary embedded twice in one record (pages 22 and 34 of a
# reviewed chart, 2026-10-22): the same document under two sections, chart pages 15
# and 27. Read 97-98% alike, but they are not duplicates.
_AVS_HEADER = (
    "CHRISTUS St. Vincent Urology Associates 465 SAINT MICHAELS DR SANTA FE NM 87505-7670 "
    "Gordon, Donald MRN: 0000590471, DOB: 01/09/1962 Legal Sex: M Visit date: 8/13/2025\n"
    "08/13/2025 - Office Visit in CHRISTUS St. Vincent Urology Associates (continued)\n"
)
_AVS_BODY = """AFTER VISIT SUMMARY
Gordon, Donald DOB 01/09/1962 8/13/2025 3:45 PM CHRISTUS St Vincent Urology Associates 505-982-3534
Instructions from C Stewart, MD Read the attached information Cystoscopy (English)
Return for cystoscopy and trus next visit in ~ 1 week or sooner if possible
Today's Visit You saw C Stewart, MD on Wednesday August 13, 2025. The following issues were addressed: Urinary retention Indwelling Foley catheter present
Blood Pressure 107/58 BMI 26.98 Weight 188 lb Height 5' 10" Temperature (Temporal) 98.7 °F Pulse 89 Oxygen Saturation 93%
What's Next AUG 22 2025 PROCEDURE 15 with C Stewart, MD Friday August 22 3:00 PM (Arrive by 2:45 PM) CHRISTUS St Vincent Urology Associates 465 St Michaels Dr Ste 110 Santa Fe NM 87505-7670 505-982-3534
DEC 26 2025 CSV PRE-OP with T Jefferson, PA-C Friday December 26 2:00 PM (Arrive by 1:45 PM) CHRISTUS St Vincent Orthopaedic Specialty Clinic 2968 Rodeo Park Dr W Ste 150 SANTA FE NM 87505-6383 505-982-5014
JAN 8 2026 LEFT ARTHROPLASTY TOTAL KNEE with J Carothers, MD CSVH OR 455 St Michael's Dr Santa Fe NM 87505
MyChart View your After Visit Summary and more online at https://mychart.christushealth.org/mychart/. If you have questions, please call (833)91CHART to speak with our MyChart staff.
Joseph Gordon, (MRN: 0000590471) Printed at 8/13/2025 5:10 PM Page 1 of 7 Epic"""


def _avs_page(wrapper: str, chart_page: int) -> str:
    return f"{_AVS_HEADER}{wrapper}\n{_AVS_BODY}\nPrinted on 4/3/26 9:22 PM Page {chart_page}\n"


class TestWordOverlap:
    """2026-10-22 (user): pages compared on their distinct words, as read; a duplicate
    at >= 98% overlap or when the smaller page's words are all on the other."""

    def test_overlap_and_contained(self):
        words = classify.word_overlap("a b c d", "a b c e f")
        assert (words.words_a, words.words_b, words.shared) == (4, 5, 3)
        assert words.overlap == 0.5  # 3 shared / 6 distinct in all
        assert words.contained == 0.75  # 3 of the smaller page's 4
        assert classify.word_overlap("b a a", "a b").overlap == 1.0  # order and repeats ignored

    def test_bands(self):
        words = _prose(50)
        a = " ".join(words)
        pages = [page(1, 1), page(2, 2)]
        for changes, expected in ((1, "duplicate"), (7, "partial"), (40, None)):  # 7: 95.6%, 10 different
            b = _edited(words, changes)
            rows = _classify(pages, {1: a, 2: b}, {1, 2})
            flags = {r["flag"] for r in rows}
            pointing = [r for r in rows if r["duplicate_of"] is not None]
            overlap = classify.word_overlap(a, b).overlap
            if expected == "duplicate":
                assert overlap >= 0.98 and "duplicate" in flags, overlap
            elif expected == "partial":
                assert 0.95 <= overlap < 0.98 and "duplicate" not in flags and pointing, overlap
                assert "partial_duplicate_of_page_id:" in pointing[0]["reason"]
            else:
                assert overlap < 0.95 and not pointing, overlap

    def test_reading_order_does_not_matter(self):
        words = _prose(51)
        half = len(words) // 2
        rows = _classify([page(1, 1), page(2, 2)],
                         {1: " ".join(words), 2: " ".join(words[half:] + words[:half])}, {1, 2})
        assert any(r["flag"] == "duplicate" for r in rows)

    def test_a_page_inside_a_longer_one_is_its_duplicate(self):
        inner = " ".join(_prose(52))
        outer = inner + " " + " ".join(_prose(53, words=40))
        assert classify.word_overlap(inner, outer).contained == 1.0
        rows = {r["page_id"]: r for r in _classify([page(1, 1), page(2, 2)], {1: inner, 2: outer}, {1, 2})}
        assert (rows[1]["flag"], rows[1]["duplicate_of"], rows[1]["confidence"]) == ("duplicate", 2, 1.0)
        assert rows[2]["flag"] == "not_blank_junk"  # the longer page is the original

    def test_best_match_shows_the_counts(self):
        words = _prose(54)
        texts = {1: " ".join(words), 2: _edited(words, 4)}
        best: dict = {}
        _classify([page(1, 1), page(2, 2)], texts, {1, 2}, best_matches=best)
        match = best["2.jpg"]
        words2 = classify.word_overlap(texts[2], texts[1])
        assert (match["page_name"], match["page_number"]) == ("1.jpg", 1)
        assert match["overlap"] == round(words2.overlap, 4) == match["similarity"]
        assert match["shared_words"] == words2.shared
        assert (match["distinct_words"], match["other_distinct_words"]) == (words2.words_a, words2.words_b)
        assert match["contained"] is False

    def test_one_document_under_two_sections(self):
        # The After Visit Summary embedded on pages 22 and 34 (chart pages 15 and 27):
        # 97.7% overlap, and every distinct word of page 22 is on page 34 -- so, as
        # contained, a duplicate. Nothing in the words tells the two apart.
        p22 = _avs_page("After Visit Summary (continued)", 15)
        p34 = _avs_page(
            "Documents (continued)\nAfter Visit Summary - Document on 8/13/2025 5:10 PM: "
            "After Visit Summary\nDocument (below)", 27,
        )
        words = classify.word_overlap(p22, p34)
        assert (words.words_a, words.words_b, words.shared) == (171, 175, 171)
        assert round(words.overlap, 4) == 0.9771 and words.contained == 1.0
        assert words.different == 4  # page 34 adds 4 distinct words
        rows = {r["page_id"]: r for r in _classify([page(22, 22), page(34, 34)], {22: p22, 34: p34}, {22, 34})}
        # Contained with fewer than 10 words different: the later page is the duplicate.
        assert (rows[34]["flag"], rows[34]["duplicate_of"]) == ("duplicate", 22)
        assert rows[22]["flag"] == "not_blank_junk"

    def test_which_page_of_a_contained_pair_is_the_duplicate(self):
        inner = " ".join(_prose(55))
        few_more = inner + " reviewed signed today"  # < 10 distinct words added
        many_more = inner + " " + " ".join(f"extra{i}" for i in range(12))
        pages = [page(1, 1), page(2, 2)]
        # Fewer than 10 words different: the later page, even though it is larger.
        rows = {r["page_id"]: r for r in _classify(pages, {1: inner, 2: few_more}, {1, 2})}
        assert (rows[2]["flag"], rows[2]["duplicate_of"]) == ("duplicate", 1)
        # 10 or more: the larger page stays, whichever comes first.
        rows = {r["page_id"]: r for r in _classify(pages, {1: many_more, 2: inner}, {1, 2})}
        assert (rows[2]["flag"], rows[2]["duplicate_of"]) == ("duplicate", 1)
        rows = {r["page_id"]: r for r in _classify(pages, {1: inner, 2: many_more}, {1, 2})}
        assert (rows[1]["flag"], rows[1]["duplicate_of"]) == ("duplicate", 2)

    def test_few_words_different_is_a_duplicate_or_partial_whatever_the_overlap(self):
        # A short page: fewer distinct words, so each difference costs more overlap.
        words = [f"word{i}" for i in range(60)]
        base = " ".join(words)
        pages = [page(1, 1), page(2, 2)]
        # 2026-10-23 (user): duplicate below 8 distinct words different, partial below 15.
        for replaced, expected in ((3, "duplicate"), (4, "partial"), (6, "partial"), (8, None)):
            other = " ".join(f"new{i}" if i < replaced else w for i, w in enumerate(words))
            pair = classify.word_overlap(base, other)
            assert pair.different == 2 * replaced and pair.overlap < 0.95
            rows = _classify(pages, {1: base, 2: other}, {1, 2})
            flags = {r["flag"] for r in rows}
            pointing = [r for r in rows if r["duplicate_of"] is not None]
            if expected == "duplicate":
                assert "duplicate" in flags
            elif expected == "partial":
                assert "duplicate" not in flags and pointing
            else:
                assert not pointing

    def test_unrelated_short_pages_are_not_flagged_on_their_word_count(self):
        a, b = "Alpha clinical history. " * 20, "Beta surgical findings. " * 20
        pair = classify.word_overlap(a, b)
        assert pair.different == 6 and pair.overlap == 0.0
        rows = _classify([page(1, 1), page(2, 2)], {1: a, 2: b}, {1, 2})
        assert all(r["duplicate_of"] is None for r in rows)

    def test_a_duplicate_is_about_as_long_as_its_original(self):
        # 2026-10-22 (user): contained, but the other page is a third longer -> not a
        # duplicate (a partial at most).
        inner = " ".join(_prose(56))
        outer = inner + " " + " ".join(_prose(57, words=140))
        assert classify.word_overlap(inner, outer).contained == 1.0
        assert classify.duplicate_char_count(inner) < 0.9 * classify.duplicate_char_count(outer)
        rows = _classify([page(1, 1), page(2, 2)], {1: inner, 2: outer}, {1, 2})
        assert all(r["flag"] != "duplicate" for r in rows)

    def test_the_later_page_is_flagged_even_when_it_is_longer(self):
        # 2026-10-22 (user): not contained, 98%+ alike -> the later page, whatever its length.
        words = _prose(58)
        earlier = " ".join(words)
        later = _edited(words, 1) + " signed"  # a word replaced, one added: longer, not contained
        pair = classify.word_overlap(earlier, later)
        assert pair.contained < 1.0 and pair.overlap >= 0.98
        assert classify.duplicate_char_count(later) > classify.duplicate_char_count(earlier)
        rows = {r["page_id"]: r for r in _classify([page(1, 1), page(2, 2)], {1: earlier, 2: later}, {1, 2})}
        assert (rows[2]["flag"], rows[2]["duplicate_of"]) == ("duplicate", 1)
        assert rows[1]["flag"] == "not_blank_junk"

