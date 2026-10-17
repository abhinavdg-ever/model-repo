"""Blank/junk duplicate scoping and DOS date handling.

Duplicate detection runs after the model, among Main pages only: ±2 neighbor
similarity (≥98%) or one page's text wholly contained in the other's.
Blank / junk / short pages are excluded from comparison; on a match the higher
character-count page stays the original (earlier page on a tie).
UI: similarity 100% → Yes; [98%, 100%) → May Be; else No.
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

    def test_longer_later_page_wins_as_original(self):
        pages = [page(1, 1), page(2, 2)]
        long = _body(8)
        # Tiny truncation keeps SequenceMatcher ratio well above 98%.
        short = long[:-3]
        rows = _classify(pages, {1: short, 2: long}, {1, 2})
        by_id = {r["page_id"]: r for r in rows}
        assert by_id[2]["flag"] == "not_blank_junk"
        assert by_id[1]["flag"] == "duplicate"
        assert by_id[1]["duplicate_of"] == 2

    def test_pages_more_than_five_apart_are_not_compared(self):
        pages = [page(i, i) for i in range(1, 8)]
        body = _body()
        # page 1 and page 7 are identical but 6 steps apart (> ±5)
        texts = {i: f"Unrelated progress note number {i} with its own findings. " * 6 for i in range(2, 7)}
        texts[1] = texts[7] = body
        rows = _classify(pages, texts, set(range(1, 8)))
        flags = {r["page_id"]: r["flag"] for r in rows}
        assert flags[1] == "not_blank_junk"
        assert flags[7] == "not_blank_junk"

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

    def test_page_contained_in_a_neighbor_is_duplicate(self):
        pages = [page(1, 1), page(2, 2)]
        inner = _body(6)
        outer = inner + "Addendum with medication reconciliation and labs. " * 12
        rows = _classify(pages, {1: inner, 2: outer}, {1, 2})
        by_id = {r["page_id"]: r for r in rows}
        assert by_id[2]["flag"] == "not_blank_junk"
        assert by_id[1]["flag"] == "duplicate"
        assert by_id[1]["duplicate_of"] == 2
        assert by_id[1]["confidence"] == 1.0
        assert "contained" in by_id[1]["reason"]

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

        monkeypatch.setattr(dos_stage, "_llm_client", lambda: None)
        monkeypatch.setattr(dos_stage, "stage_run", fake_stage_run)
        monkeypatch.setattr(dos_stage, "connect", lambda: nullcontext(None))
        monkeypatch.setattr(dos_stage, "get_blank_junk_flags", lambda *a, **k: {})
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
    return detect_dos_per_page(text, None, use_llm=False, **kwargs)


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

    def test_llm_is_the_fallback_on_clinical_pages_only(self, monkeypatch):
        import dos_logic

        calls = []

        def fake_llm(page_text, *_a, **_k):
            calls.append(page_text)
            return ("05-06-2024", "05-06-2024")

        monkeypatch.setattr(dos_logic, "extract_dos_range_with_llm", fake_llm)
        text = (
            "===== 1.jpg =====\nDate of Service: 03/14/2024\nChief Complaint: cough\n"
            "===== 2.jpg =====\nChief Complaint: knee pain, no date here.\n"
            "===== 3.jpg =====\nPlain page without a date or cue.\n"
        )
        hits = dos_logic.detect_dos_per_page(text, object(), use_llm=True)
        assert len(calls) == 1  # page 1 had a date; page 3 has no cue
        assert hits[1]["dos_from_iso"] == "2024-05-06"
        assert hits[1]["page_source"] == "llm"
        assert hits[0]["page_source"] == "edge"

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

    def test_tesseract_comes_before_the_key_value_date(self):
        kv = {"1.jpg": [{"iso": "2024-06-01", "keyword": "Date"}]}
        hit = _pages("no label here", kv_dates=kv,
                     tesseract_texts={"1.jpg": "Encounter Date: 03/15/2024"})[0]
        assert hit["dos_from_iso"] == "2024-03-15"

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
    row = {"page_name": "1.jpg", "label": "Main", "confidence": 0.9, "reason": "R7", "votes": {"rule": "R7"}}
    bj_stage._write_votes("c", 1, [row])
    bj_stage._write_votes("c", 2, [{**row, "reason": "R3", "votes": {"rule": "R3"}}])
    bj_stage._write_votes("c", 2, [{**row, "reason": "R9", "votes": {"rule": "R9"}}])
    data = json.loads((tmp_path / "c" / "staging" / "blank_junk.json").read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["chart_name"] == "c"
    assert data["pages"]["1.jpg"]["pass_1"]["rule"] == "R7"
    assert data["pages"]["1.jpg"]["pass_2"]["rule"] == "R9"
