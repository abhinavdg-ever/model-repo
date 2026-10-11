"""Blank/junk duplicate scoping and DOS date handling.

Duplicate detection runs after the model, among Main pages only: ±2 neighbor
similarity (≥98%) or one page's text wholly contained in the other's.
Blank / junk / short pages are excluded from comparison; on a match the higher
character-count page stays the original (earlier page on a tie).
UI: similarity 100% → Yes; [98%, 100%) → May Be; else No.
"""
from __future__ import annotations

from datetime import date

import pytest

from stages.lib.blank_junk.stage import SUBTYPE_DB, _classify, _to_db_flag
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

    def test_pages_more_than_two_apart_are_not_compared(self):
        pages = [page(1, 1), page(2, 2), page(3, 3), page(4, 4)]
        body = _body()
        # page 1 and page 4 are identical but 3 steps apart (> ±2)
        texts = {
            1: body,
            2: "Completely different progress note content here. " * 6,
            3: "Another unrelated assessment and plan page text. " * 6,
            4: body,
        }
        rows = _classify(pages, texts, {1, 2, 3, 4})
        flags = {r["page_id"]: r["flag"] for r in rows}
        assert flags[1] == "not_blank_junk"
        assert flags[4] == "not_blank_junk"

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
    """The blank/junk model decides the flag and the junk subtype."""

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
        assert status["path"] == str(tmp_path / "tfidf_flat.joblib")
        assert status["ready"] is False
        assert "model file missing" in status["reason"]

    def test_model_keep_is_not_rewritten_by_a_blank_phrase(self):
        from classify import CODE_BLANK, CODE_MAIN

        code, reason, conf = self._bridge().classify_page(
            "This page intentionally left blank"
        )
        assert code in {CODE_BLANK, CODE_MAIN}
        assert reason.startswith("model:")
        assert "regex_fallback" not in reason
        assert conf is not None

    def test_clinical_note_is_main(self):
        from classify import CODE_MAIN

        text = (
            "Progress Note\n"
            "History of Present Illness: Patient presents with SOB.\n"
            "Assessment: Hypertension.\n"
            "Current Medications: Amlodipine.\n"
        )
        code, reason, conf = self._bridge().classify_page(text)
        assert code == CODE_MAIN
        assert reason.startswith("model:")
        assert conf is not None and conf >= 0.7

    def test_model_junk_subtype_comes_from_the_model_label(self):
        from classify import JUNK_CODES, CODE_BLANK

        text = (
            "MEDICAL RECORDS REQUEST\n"
            "Request for medical records\n"
            "Please send the following records for the patient listed.\n"
            "Records retrieval vendor: Copy service\n"
            "Fulfillment due within 10 business days"
        )
        code, reason, conf = self._bridge().classify_page(text)
        assert reason.startswith("model:")
        if code in JUNK_CODES - {CODE_BLANK}:
            assert "subtype:model:" in reason
        assert "subtype:regex:" not in reason
        assert "regex_fallback" not in reason
        assert conf is not None

    def test_junk_subtype_is_the_model_label(self):
        from classify import CODE_LETTER_FAX, CODE_OTHERS

        bridge = self._bridge()
        code, why = bridge._junk_subtype("JUNK_FAX_TRANSMISSION")
        assert (code, why) == (CODE_LETTER_FAX, "subtype:model:JUNK_FAX_TRANSMISSION")
        code, why = bridge._junk_subtype("JUNK")
        assert (code, why) == (CODE_OTHERS, "subtype:model:JUNK")

    def test_missing_model_is_stamped_as_a_fallback(self, monkeypatch):
        bridge = self._bridge()
        monkeypatch.setattr(bridge, "_load_service", lambda: None)
        _code, reason, conf = bridge.classify_page("Invoice Number 123 Amount Due")
        assert reason.startswith("regex_fallback:model_unavailable")
        assert conf is None

    def test_vendored_src_package_does_not_stay_on_sys_path(self):
        import sys

        bridge = self._bridge()
        assert bridge._load_service() is not None
        assert str(bridge._VENDOR) not in sys.path

    def test_health_reports_the_model_without_loading_it(self):
        status = self._bridge().model_status()
        assert status["ready"] is True
        assert status["model_version"]
        assert status["reason"] is None


class TestSubtypeMapping:
    def test_junk_always_gets_a_legal_subtype(self):
        """JUNK_CODES includes CODE_BLANK; blank is its own flag and takes
        precedence, so only the remaining codes become flag='junk'."""
        from classify import CODE_BLANK, JUNK_CODES

        for code in JUNK_CODES - {CODE_BLANK}:
            flag, subtype = _to_db_flag(code)
            assert flag == "junk"
            assert subtype in set(SUBTYPE_DB.values())

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
        monkeypatch.setattr(dos_stage, "get_chart", lambda *a, **k: None)
        monkeypatch.setattr(dos_stage, "get_page_classification_map", lambda *a, **k: {})
        monkeypatch.setattr(dos_stage, "mark_skipped", lambda *a, **k: None)
        monkeypatch.setattr(dos_stage, "_combined_text", lambda *a, **k: "")
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


class TestDosLayouts:
    def test_encounter_date_with_at_before_the_month(self):
        """CCD header: the visit date follows 'at', and file dates must not win."""
        from dos_logic import extract_dos_from_page_text

        text = (
            "Date of birth | 04/05/1993\n"
            "Document Created: | April 15, 2026, 16:02:25 -0400\n"
            "Encounter Date | at January 28, 2025\n"
            "Legal authenticator | ZIYI WANG, MD signed at January 28, 2025, 12:17:31, EST\n"
            "CCD Rendered Date | April 18, 2026\n"
            "Reason for Visit\n"
        )
        hit = extract_dos_from_page_text(text)
        assert hit["dos_from"] == "01-28-2025"
        assert hit["dos_to"] == "01-28-2025"
        assert hit["keyword"].casefold() == "encounter date"

    def test_header_date_on_a_visit_form_ignores_the_dob(self):
        """The visit date is a bare cell under a Date column, rows below the DOB."""
        from dos_logic import extract_dos_from_page_text

        text = (
            "Ph: 801.568.0200\n"
            "Name - Sanders, Charles | DOB - 03/06/1972 | (69)\n"
            "Date | Location | Location\n"
            "PCP | Insurance | Insurance\n"
            "12/31/2025 | Hoopes Vision Correction Center\n"
            "Reason For Visit: Post-op Check - S/P Phaco PC IOL OD.\n"
            "HPI: Post Op: vision is out of focus OD.\n"
        )
        assert extract_dos_from_page_text(text) is None


RECEIVED = date(2026, 5, 1)


class TestDosDriver:
    def test_a_page_with_no_date_keeps_the_default(self):
        """A page with no date of its own gets the default, not the previous visit."""
        from dos_logic import detect_dos_per_page, profile

        text = (
            "===== 1.jpg =====\n"
            "Office Visit  Date of Service: 03/14/2024\n"
            "Patient seen for follow up.\n"
            "\n"
            "===== 2.jpg =====\n"
            "Continued progress note with no date on it.\n"
        )
        hits = detect_dos_per_page(text, None, use_llm=False, received_date=RECEIVED)
        assert len(hits) == 2
        assert hits[0]["dos_from"] == "03-14-2024"
        # No date on the page: the default prevails. The visit is not carried down.
        assert hits[1]["dos_from"] == ""
        assert hits[1]["is_default"] is True
        assert hits[1]["doc_dos_from_iso"] == profile().default_date
        assert hits[1]["match_type"] == "no_date_found"

    def test_iso_columns_are_actually_iso(self):
        from dos_logic import detect_dos_per_page

        text = "===== 1.jpg =====\nDate of Service: 03/14/2024\nVisit note.\n"
        hits = detect_dos_per_page(text, None, use_llm=False, received_date=RECEIVED)
        assert hits[0]["dos_from_iso"] == "2024-03-14"
        assert hits[0]["doc_dos_from_iso"] == "2024-03-14"

    def test_llm_is_not_called_when_no_client(self):
        from dos_logic import detect_dos_per_page

        text = "===== 1.jpg =====\nNo dates whatsoever on this page.\n"
        hits = detect_dos_per_page(text, None, use_llm=False)
        assert len(hits) == 1
        assert hits[0]["match_type"] != "llm"

    def test_nothing_found_is_the_flagged_default(self):
        from dos_logic import detect_dos_per_page, profile

        text = "===== 1.jpg =====\nNo dates whatsoever on this page.\n"
        hit = detect_dos_per_page(text, None, use_llm=False)[0]
        assert hit["dos_from"] == ""
        assert hit["match_type"] == "no_date_found"
        assert hit["confidence"] == 0
        assert hit["is_default"] is True
        assert hit["doc_dos_from_iso"] == profile().default_date

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
        hits = dos_logic.detect_dos_per_page(
            text, object(), use_llm=True, received_date=RECEIVED
        )
        assert len(calls) == 1  # page 1 scored a date; page 3 has no cue
        assert hits[1]["dos_from_iso"] == "2024-05-06"
        assert hits[1]["page_source"] == "llm"
        assert hits[0]["page_source"] == "rules"



def _scored(text, **kwargs):
    """Stages A–C on one page; returns (candidates, profile)."""
    from dos_logic import find_candidates, page_features, profile, score_candidate

    prof = profile()
    cands = find_candidates(text)
    page_features(
        text,
        cands,
        page_type=kwargs.get("page_type", ""),
        received_year=RECEIVED.year,
        prof=prof,
    )
    for cand in cands:
        score_candidate(cand, prof)
    return cands, prof


class TestDosCandidates:
    def test_four_shapes(self):
        from dos_logic import find_candidates

        text = "12/31/2025 and 1/5/24 and 2025-12-31 and January 28, 2025 and 28 January 2025"
        assert [c.iso for c in find_candidates(text)] == [
            "2025-12-31", "2024-01-05", "2025-12-31", "2025-01-28", "2025-01-28",
        ]

    def test_two_digit_years_pivot_at_50(self):
        from dos_logic import find_candidates

        assert [c.iso for c in find_candidates("1/5/49 1/5/50")] == ["2049-01-05", "1950-01-05"]

    def test_impossible_days_are_dropped_old_years_are_kept(self):
        from dos_logic import find_candidates

        assert [c.iso for c in find_candidates("02/30/2024 DOB 04/05/1998")] == ["1998-04-05"]

    def test_offsets_point_at_the_raw_text(self):
        from dos_logic import find_candidates

        text = "Seen on Jan. 3, 2024 today"
        (cand,) = find_candidates(text)
        assert text[cand.start : cand.end] == cand.raw == "Jan. 3, 2024"


class TestDosFeaturesAndScore:
    def test_label_classes(self):
        cands, _ = _scored(
            "Date of Service: 03/14/2024. DOB: 01/02/1960. Printed on 03/20/2024. "
            "Return visit 06/01/2024. Colonoscopy 02/02/2024. Admit Date 03/10/2024. "
            "Discharged 03/12/2024."
        )
        assert [c.label_class for c in cands] == [
            "encounter", "birth", "doc_meta", "future", "procedure", "admit", "discharge",
        ]

    def test_a_label_does_not_reach_past_an_earlier_date(self):
        cands, _ = _scored("DOB 03/06/1972 | 12/31/2025")
        assert [c.label_class for c in cands] == ["birth", "none"]

    def test_distance_decays_the_label(self):
        near, _ = _scored("Date of Service: 03/14/2024")
        far, _ = _scored("Date of Service" + " " * 60 + "03/14/2024")
        assert far[0].label_distance > near[0].label_distance
        assert far[0].score < near[0].score

    def test_dob_is_a_weight_not_a_veto(self):
        cands, prof = _scored("x " * 100 + "DOB: 01/02/2024" + " y" * 100)
        (cand,) = cands
        expected = prof.base + prof.label_weights["birth"] - prof.label_distance_decay * 1
        assert cand.score == pytest.approx(max(0.0, expected))

    def test_old_years_are_penalised_not_deleted(self):
        from dos_logic import profile

        prof = profile()
        old_year = RECEIVED.year - prof.max_age_years - 1
        pad = "x " * 100  # off the page edges, so neither score is clamped at 1
        cands, _ = _scored(f"{pad}Date of Service: 03/14/{old_year}{pad}")
        recent, _ = _scored(f"{pad}Date of Service: 03/14/{RECEIVED.year - 1}{pad}")
        assert len(cands) == 1
        assert cands[0].score == pytest.approx(recent[0].score - prof.age_penalty)

    def test_cluster_counts_other_dates_within_30_days(self):
        from dos_logic import chart_features, find_candidates, profile

        cands = find_candidates("03/01/2024 03/20/2024 03/31/2024 06/01/2024")
        chart_features(cands, profile())
        assert [c.cluster_size for c in cands] == [2, 2, 2, 0]

    def test_admit_discharge_pair_emits_a_range(self):
        from dos_logic import best_page_date

        cands, prof = _scored("Admit Date: 03/01/2024  Discharge Date: 03/05/2024")
        assert all(c.in_range_pair for c in cands)
        best = best_page_date(cands, prof)
        assert (best.dos_from, best.dos_to) == ("2024-03-01", "2024-03-05")
        assert all(c.chosen for c in cands)

    def test_below_threshold_is_no_date(self):
        from dos_logic import best_page_date

        cands, prof = _scored("x " * 100 + "Printed on 03/20/2024" + " y" * 100)
        assert cands and best_page_date(cands, prof) is None


class TestDosTimestamps:
    def test_time_beside_a_date_is_detected(self):
        cands, _ = _scored(
            "03/20/2024 10:15 AM | 2025-12-31T10:00:00 | 10:15 03/21/2024 | "
            "03/22/2024, 4:05 pm | 03/23/2024 visit"
        )
        assert [c.has_time for c in cands] == [True, True, True, True, False]

    def test_unlabelled_print_stamp_at_the_edge_does_not_win(self):
        from dos_logic import extract_dos_from_page_text

        body = "Chief Complaint: cough. " + "word " * 150
        text = f"03/20/2025 10:15 AM\n{body}\nPage 1 of 2 03/20/2025 10:15 AM"
        assert extract_dos_from_page_text(text, received_date=RECEIVED) is None

    def test_print_stamp_loses_to_the_visit_date(self):
        from dos_logic import extract_dos_from_page_text

        text = (
            "Printed: 04/18/2025 09:12 AM\n"
            "Chief Complaint: cough\n"
            "Seen 03/14/2025 in clinic.\n"
            "04/18/2025 09:12 AM Page 1 of 1"
        )
        assert extract_dos_from_page_text(text, received_date=RECEIVED) is None

    def test_a_labelled_encounter_time_under_0_75_is_not_used(self):
        from dos_logic import extract_dos_from_page_text

        text = "Arrival Date: 03/14/2025 14:32\nChief Complaint: chest pain"
        assert extract_dos_from_page_text(text, received_date=RECEIVED) is None


class TestDosResolve:
    @staticmethod
    def _run(pages, **kwargs):
        from dos_logic import detect_dos_per_page

        text = "".join(f"===== {i}.jpg =====\n{body}\n" for i, body in enumerate(pages, 1))
        return detect_dos_per_page(text, None, use_llm=False, received_date=RECEIVED, **kwargs)

    def test_every_page_keeps_its_own_date(self, monkeypatch):
        """No progress-note span: an undated page after a note stays undated,
        and final_dos is the page's own date. The continuity stage carries the
        note's date across its pages as the Final DOS."""
        import dos_logic

        types = {"Progress Note\nDate of Service: 03/14/2024": "Progress Note"}
        monkeypatch.setattr(dos_logic, "_page_type_name", lambda t, _n: types.get(t.strip(), ""))
        hits = self._run([
            "Progress Note\nDate of Service: 03/14/2024",
            "Vitals stable. No date on this page.",
            "Date of Service: 04/02/2024",
        ])
        assert hits[0]["final_dos"] == "2024-03-14"
        assert hits[1]["dos_from_iso"] == ""
        assert hits[1]["final_dos"] == ""
        assert hits[1]["match_type"] == "no_date_found"
        assert hits[2]["final_dos"] == "2024-04-02"
        assert all(h["match_type"] not in ("span", "span_start") for h in hits)

    def test_demographics_and_injection_pages_keep_the_default(self, monkeypatch):
        import dos_logic

        def page_type(text, _n):
            if "Demographics" in text:
                return "Patient Demographics"
            if "Injection" in text:
                return "Injection Visit"
            return ""

        monkeypatch.setattr(dos_logic, "_page_type_name", page_type)
        hits = self._run([
            "Date of Service: 03/14/2024",
            "Demographics  Date of Service: 05/01/2024",
            "Injection record  Date of Service: 06/01/2024",
        ])
        default = dos_logic.profile().default_date
        assert hits[1]["dos_from"] == ""
        assert hits[1]["doc_dos_from_iso"] == default
        assert hits[1]["is_default"] is True
        assert hits[1]["match_type"] == "default_page"
        assert hits[2]["doc_dos_from_iso"] == default
        assert hits[2]["is_default"] is True

    def test_the_page_type_stage_sub_type_beats_the_keyword_guess(self, monkeypatch):
        """Page type runs before DOS; its Extracted sub-type decides a
        default-date page even when the keyword model would not."""
        import dos_logic

        monkeypatch.setattr(dos_logic, "_page_type_name", lambda t, _n: "")
        hits = self._run(
            ["Date of Service: 03/14/2024", "Date of Service: 05/01/2024"],
            page_types={"2.jpg": "Patient Demographics"},
        )
        assert hits[0]["dos_from_iso"] == "2024-03-14"
        assert hits[1]["match_type"] == "default_page"
        assert hits[1]["dos_from"] == ""

    def test_a_text_date_below_dos_min_score_is_not_used(self):
        """The regex sweep is the backup to the key/value date, at 0.75."""
        import dos_logic

        assert dos_logic.profile().min_score == 0.75

    def test_non_encounter_page_never_replaces_an_encounter(self, monkeypatch):
        import dos_logic

        monkeypatch.setattr(
            dos_logic,
            "_page_type_name",
            lambda t, _n: "Medication List" if "Medication" in t else "",
        )
        hits = self._run([
            "Date of Service: 03/14/2024",
            "Medication List  Date of Service: 05/01/2024",
        ])
        assert hits[1]["dos_from_iso"] == "2024-05-01"
        assert hits[1]["doc_dos_from_iso"] == "2024-03-14"
        assert hits[1]["match_type"] == "non_encounter_page"

    def test_candidate_log_has_every_candidate(self):
        log: list = []
        self._run(["DOB 01/02/1960  Date of Service: 03/14/2024"], candidate_log=log)
        assert [(r["iso"], r["chosen"]) for r in log] == [
            ("1960-01-02", False), ("2024-03-14", True),
        ]
        assert {"label_class", "label_distance", "position", "edge_position", "page_type",
                "has_clinical_cue", "year_delta", "cluster_size", "in_range_pair",
                "score"} <= log[0].keys()


def test_ccd_visit_page_dos_is_the_encounter_date():
    from pathlib import Path

    from dos_logic import extract_dos_from_page_text

    text = (Path(__file__).parent / "fixtures" / "ccd_visit_page.txt").read_text(encoding="utf-8")
    assert extract_dos_from_page_text(text, received_date=RECEIVED)["dos_from"] == "01-28-2025"
