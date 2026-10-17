"""KV extractor rules that need no model: page numbers, member ids, signer credentials."""
from __future__ import annotations

from stages.lib.extraction.electronic_signature.extract import split_name_designation
from stages.lib.extraction.member_id.extract import _id_pieces, accept
from stages.lib.extraction.member_name.extract import _ADDRESS
from stages.lib.extraction.page_no.extract import _INLINE_SLASH, extract_page
from stages.lib.extraction.util.geometry import words_from_page

PAGE_W, PAGE_H = 1000, 1000


def _page(*lines: tuple[float, str]) -> list:
    """Words laid out left to right, one line per (y, text)."""
    words = []
    for y, text in lines:
        x = 50.0
        for part in text.split():
            right = x + 12 * len(part)
            words.append({"content": part, "polygon": [x, y, right, y, right, y + 14, x, y + 14]})
            x = right + 8
    return words_from_page({"words": words, "width": PAGE_W, "height": PAGE_H})


def _page_numbers(*lines: tuple[float, str]) -> list[str]:
    return sorted(hit.value for hit in extract_page(_page(*lines), PAGE_H))


class TestPageNo:
    def test_a_slash_pair_inside_a_date_is_not_a_page(self):
        assert _INLINE_SLASH.search("Order date/time: 07/11/24 1432") is None
        assert _INLINE_SLASH.search("6/6-6/20/2024: mBCG x 3") is None
        assert _INLINE_SLASH.search("Smith, John 2/5").group(0) == "2/5"

    def test_a_page_word_label_beats_an_inline_pair_in_the_other_band(self):
        values = _page_numbers((20, "MRN: 023466 Dose 1/3"), (500, "body text"), (970, "Pg 2 of 10"))
        assert values == ["Pg 2 of 10"]

    def test_n_of_m_beats_a_slash_pair(self):
        values = _page_numbers((20, "MRN: 023466 Dose 3/3"), (500, "body text"), (970, "Smith 6 of 10"))
        assert values == ["6 of 10"]

    def test_an_inline_page_counts_when_nothing_else_does(self):
        assert _page_numbers((20, "Smith, John 01/02/2024 2 of 5"), (500, "body text")) == ["2 of 5"]


class TestMemberId:
    def test_an_id_glued_to_its_key_is_read(self):
        assert [value for value, _, _ in _id_pieces("MRN:88234.0")] == ["88234.0"]
        assert [value for value, _, _ in _id_pieces("(ID #718,")] == ["718"]

    def test_a_decimal_mrn_is_an_id_but_a_short_decimal_is_not(self):
        assert accept("88234.0") == "88234.0"
        assert accept("3.14") == ""


class TestSignature:
    def test_the_credential_is_split_out_wherever_it_sits(self):
        assert split_name_designation("LAUBERT CNP, MATTHEW") == ("LAUBERT MATTHEW", "CNP")
        assert split_name_designation("Reilly, Thomas J MD") == ("Reilly, Thomas J", "MD")


class TestMemberNameEdgeLine:
    def test_a_street_address_is_not_a_demographic_strip(self):
        assert _ADDRESS.search("MF Lab 800 E. DAWSON TYLER 1612 TX 75701 USA")
        assert not _ADDRESS.search("Patient: Smith, John DOB: 01/02/1950 MRN: 023466")


def _dos_row(key: str, tier: str, value: str):
    from stages.lib.extraction.dos.extract import DosHit

    return DosHit(key=key, region="body", sentence="", tier=tier, value=value,
                  dos_from=value, dos_to=value, score=0.9, accepted=True)


def test_lab_dos_collected_then_received_then_printed():
    from stages.lib.extraction.dos.extract import mark_selected
    from stages.lib.extraction.util.keys import load_key_tiers

    tiers = load_key_tiers("dos")
    assert tiers["collection date"] == "collected"
    assert tiers["date received"] == "received"
    assert tiers["printed"] == "printed"

    def chosen(*rows):
        return [row.key for row in mark_selected(list(rows)) if row.selected]

    collected = _dos_row("Collection Date", "collected", "10/01/2024")
    received = _dos_row("Received", "received", "10/02/2024")
    printed = _dos_row("Printed", "printed", "10/05/2024")
    order = _dos_row("Order Date", "service", "09/30/2024")
    assert chosen(printed, received, collected, order) == ["Collection Date"]
    received, printed = _dos_row("Received", "received", "10/02/2024"), _dos_row("Printed", "printed", "10/05/2024")
    assert chosen(printed, received) == ["Received"]
    weak, printed = _dos_row("Report Date", "weak", "10/04/2024"), _dos_row("Printed", "printed", "10/05/2024")
    assert chosen(printed, weak) == ["Report Date"]
    assert chosen(_dos_row("Printed", "printed", "10/05/2024")) == ["Printed"]


class TestSpacedSlashDates:
    """'DATE: 05 / 20 / 2025' on a printed form (chart 67992303_56072120 p7)."""

    def test_the_date_is_found_and_normalised(self):
        from stages.lib.extraction.util.dates import canonical_date, find_date_ranges, find_dates, normalize_date

        found = [m.group(1) for m in find_dates("DATE: 05 / 20 / 2025 FACILITY: GREENVALLEY")]
        assert found == ["05 / 20 / 2025"]
        assert normalize_date(found[0]) == "05/20/2025" and canonical_date(found[0]) == "2025-05-20"
        # Not dates: a spaced '-' (the range separator) and a blood pressure.
        assert find_dates("1 - 2 - 2024") == [] and find_dates("BP 120 / 80") == []
        assert [(m.group(1), m.group(2)) for m in find_date_ranges("01/02/2024 - 01/05/2024")] == [
            ("01/02/2024", "01/05/2024")
        ]

    def test_the_ocr_words_are_merged_into_one_date(self):
        words = _page((100, "DATE: 05 / 20 / 2025 FACILITY"), (200, "BP 120 / 80"))
        assert [word.content for word in words] == ["DATE:", "05/20/2025", "FACILITY", "BP", "120", "/", "80"]

    def test_the_model_gets_it_like_any_other_date(self):
        from stages.lib.extraction.dos.extract import extract_page, pattern_page
        from stages.lib.extraction.util.keys import KeyHit

        def rows(text):
            words = _page((100, text))
            key = words[0]
            hit = KeyHit(field="dos", key="DATE:", word_indexes=[key.index], box=key.box, weak=True,
                         region="header", trusted=True, value_words=words[1:],
                         value_text=" ".join(word.content for word in words[1:]))
            accepted = [row for row in extract_page([hit], words, PAGE_H) if row.accepted]
            return accepted, pattern_page([hit], words)

        (spaced,), pattern = rows("DATE: 05 / 20 / 2025")
        (plain,), _ = rows("DATE: 05/20/2025")
        assert pattern == []
        assert (spaced.value, spaced.score, spaced.pattern) == ("05/20/2025", plain.score, "")

    def test_the_edge_label_rule_reads_it_like_any_other_date(self):
        import stages.lib.dos.stage  # noqa: F401  — puts dos/ on sys.path
        from dos_logic import detect_dos_per_page

        hit = detect_dos_per_page("===== 7.png =====\nDate of Service: 05 / 20 / 2025\n", None, use_llm=False)[0]
        plain = detect_dos_per_page("===== 7.png =====\nDate of Service: 05/20/2025\n", None, use_llm=False)[0]
        assert hit["dos_from_iso"] == "2025-05-20" and hit["confidence"] == plain["confidence"]


class TestSpacesOnlyDates:
    """'Date: 06 12 24': the pattern fallback only, right after a DOS key, at a very low score."""

    @staticmethod
    def pages(text, extra=None):
        from stages.lib.extraction.dos.extract import extract_page, pattern_page
        from stages.lib.extraction.util.keys import KeyHit

        lines = [(100, text)] + ([(300, extra)] if extra else [])
        words = _page(*lines)
        hits = []
        for y, line in lines:
            line_words = [word for word in words if abs(word.box.top - y) < 1]
            key = line_words[0]
            hits.append(KeyHit(field="dos", key=key.content, word_indexes=[key.index], box=key.box, weak=True,
                               region="header", trusted=True, value_words=line_words[1:],
                               value_text=" ".join(word.content for word in line_words[1:])))
        model = [row for row in extract_page(hits, words, PAGE_H) if row.accepted]
        return model, pattern_page(hits, words)

    def test_right_after_the_key_at_a_very_low_score(self):
        from stages.lib.extraction.dos.extract import SPACES_ONLY_SCORE

        model, [row] = self.pages("DATE: 06 12 24")
        assert model == []
        assert (row.value, row.score, row.pattern) == ("06/12/24", SPACES_ONLY_SCORE, "spaces_only")
        assert SPACES_ONLY_SCORE <= 0.3

    def test_not_after_another_word_and_not_an_impossible_month(self):
        assert self.pages("DATE: WBC 10 12 20")[1] == []
        assert self.pages("DATE: 13 12 24")[1] == []

    def test_a_plain_date_goes_to_the_model_the_pattern_waits(self):
        model, pattern = self.pages("DATE: 06 12 24", extra="Visit: 06/15/2024")
        assert [row.value for row in model] == ["06/15/2024"]
        assert [row.value for row in pattern] == ["06/12/24"]

    def test_the_general_date_finder_does_not_read_spaces_only(self):
        from stages.lib.extraction.util.dates import find_dates

        assert find_dates("WBC 10 12 20") == [] and find_dates("DOB 06 12 54") == []

    def test_the_dos_stage_reads_the_pattern_after_the_model_and_the_edge_label(self):
        import stages.lib.dos.stage  # noqa: F401
        from dos_logic import detect_dos_per_page

        edge_page = "===== 1.png =====\nVisit Date: 03/01/2024\n"
        bare_page = "===== 1.png =====\nno label here\n"
        pattern = {"1.png": [{"iso": "2024-06-12", "keyword": "DATE:", "pattern": "spaces_only"}]}
        model = {"1.png": [{"iso": "2024-07-01", "keyword": "DOS"}]}

        hit = detect_dos_per_page(edge_page, None, use_llm=False, kv_dates=model, kv_pattern=pattern)[0]
        assert (hit["dos_from_iso"], hit["page_source"]) == ("2024-07-01", "kv")
        # No model date: the edge label comes before the pattern.
        hit = detect_dos_per_page(edge_page, None, use_llm=False, kv_pattern=pattern)[0]
        assert (hit["dos_from_iso"], hit["page_source"]) == ("2024-03-01", "edge")
        # Neither: the pattern.
        hit = detect_dos_per_page(bare_page, None, use_llm=False, kv_pattern=pattern)[0]
        assert (hit["dos_from_iso"], hit["page_source"], hit["match_type"]) == ("2024-06-12", "kv_pattern", "kv_pattern")
        assert hit["confidence"] == 0.3


def test_every_dos_backup_is_flagged_low_and_the_models_pick_is_not():
    import stages.lib.dos.stage  # noqa: F401
    from dos_logic import detect_dos_per_page

    def conf(text, **kw):
        return detect_dos_per_page(f"===== 1.png =====\n{text}\n", None, use_llm=False, **kw)[0]

    model = conf("no label", kv_dates={"1.png": [{"iso": "2024-06-12", "keyword": "DOS"}]})
    edge = conf("Visit Date: 06/12/2024")
    fallback = conf("no label", kv_fallback={"1.png": [{"iso": "2024-06-12", "keyword": "Date", "confidence": 0.8}]})
    assert (model["confidence"], model["page_source"]) == (0.9, "kv")
    assert (edge["confidence"], edge["page_source"]) == (0.5, "edge")
    assert (fallback["confidence"], fallback["page_source"]) == (0.3, "kv_low")


def test_the_dos_csv_names_the_rule_that_gave_the_date():
    import stages.lib.dos.stage as stage
    from dos_logic import detect_dos_per_page

    assert stage.DOS_COLS[-1] == "source"
    page = "===== 1.png =====\nVisit Date: 03/01/2024\n"
    pattern = {"1.png": [{"iso": "2024-06-12", "keyword": "DATE:", "pattern": "spaces_only"}]}
    sources = [
        detect_dos_per_page(page, None, use_llm=False, kv_dates={"1.png": [{"iso": "2024-07-01"}]})[0],
        detect_dos_per_page("===== 1.png =====\nno label\n", None, use_llm=False, kv_pattern=pattern)[0],
        detect_dos_per_page(page, None, use_llm=False)[0],
        detect_dos_per_page("===== 1.png =====\nno date\n", None, use_llm=False)[0],
    ]
    assert [hit.get("page_source") or "none" for hit in sources] == ["kv", "kv_pattern", "edge", "none"]


def test_a_failed_llm_call_is_counted_and_gives_no_date(monkeypatch):
    import stages.lib.dos.stage  # noqa: F401
    import azure_retry
    import dos_logic

    class Refused:
        class chat:
            class completions:
                @staticmethod
                def create(**_):
                    raise PermissionError("401 invalid subscription key")

    monkeypatch.setattr(azure_retry, "call_with_retry", lambda fn, **_: fn())
    dos_logic.LLM_CALLS.update(made=0, failed=0, error="")
    assert dos_logic.extract_dos_range_with_llm("Office visit note", "1.png", Refused()) is None
    assert (dos_logic.LLM_CALLS["made"], dos_logic.LLM_CALLS["failed"]) == (1, 1)
    assert "401" in dos_logic.LLM_CALLS["error"]



def test_dictation_and_transcription_dates_are_never_an_edge_dos_or_a_range():
    import stages.lib.dos.stage  # noqa: F401
    from dos_logic import detect_dos_per_page

    def dos(text):
        hit = detect_dos_per_page(f"===== 1.png =====\nNote.\n{text}\n", None, use_llm=False)[0]
        return hit["dos_from_iso"], hit["dos_to_iso"]

    def source(text):
        return detect_dos_per_page(f"===== 1.png =====\nNote.\n{text}\n", None, use_llm=False)[0]["page_source"]

    assert dos("DOS: 03/01/2024\nD: 03/01/2024 T: 03/04/2024") == ("2024-03-01", "2024-03-01")
    assert dos("Date of Service: 03/01/2024 - T: 03/04/2024") == ("2024-03-01", "2024-03-01")
    # Not the edge label's date: only the last resort takes it.
    assert source("Visit Date DT: 03/04/2024") == "dictation"
    assert source("Visit Date: Dictated: 03/04/2024") == "dictation"
    # Real ranges are unchanged.
    assert dos("Date of Service: 03/01/2024 - 03/04/2024") == ("2024-03-01", "2024-03-04")
    assert dos("Admission Date: 03/01/2024 Discharge Date: 03/04/2024") == ("2024-03-01", "2024-03-04")


def test_kv_low_needs_a_model_score_of_at_least_0_3_and_the_model_picks_at_0_6():
    import stages.lib.dos.stage  # noqa: F401
    from dos_logic import detect_dos_per_page

    from stages.lib.extraction.util import config

    def kv_low(score):
        fallback = {"1.png": [{"iso": "2024-06-12", "keyword": "Date", "confidence": score}]}
        return detect_dos_per_page("===== 1.png =====\nno label\n", None, use_llm=False, kv_fallback=fallback)[0]

    assert kv_low(0.29)["match_type"] == "no_date_found"
    assert (kv_low(0.3)["match_type"], kv_low(0.3)["confidence"]) == ("kv_low", 0.3)
    assert config.SELECT_THRESHOLDS["dos"] == 0.6


def test_dictation_dates_are_the_last_resort_dictated_before_transcribed():
    import stages.lib.dos.stage  # noqa: F401
    from dos_logic import detect_dos_per_page

    def dos(text, **kw):
        hit = detect_dos_per_page(f"===== 1.png =====\nNote.\n{text}\n", None, use_llm=False, **kw)[0]
        return hit["dos_from_iso"], hit["dos_to_iso"], hit["page_source"], hit["confidence"]

    assert dos("D: 03/01/2024 T: 03/04/2024") == ("2024-03-01", "2024-03-01", "dictation", 0.3)
    assert dos("T: 03/04/2024 D: 03/01/2024")[0] == "2024-03-01"
    assert dos("Transcribed: 03/04/2024")[:3] == ("2024-03-04", "2024-03-04", "dictation")
    # A real date wins over it.
    assert dos("Visit Date: 02/27/2024\nD: 03/01/2024 T: 03/04/2024")[:3] == ("2024-02-27", "2024-02-27", "edge")
    fallback = {"1.png": [{"iso": "2024-02-20", "keyword": "Date", "confidence": 0.4}]}
    assert dos("D: 03/01/2024", kv_fallback=fallback)[2] == "kv_low"
