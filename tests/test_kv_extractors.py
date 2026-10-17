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

    def test_the_kv_rules_read_it_like_any_other_date(self):
        from stages.lib.extraction.dos.extract import extract_page
        from stages.lib.extraction.util.keys import KeyHit

        def rows(text):
            words = _page((100, text))
            key = words[0]
            hit = KeyHit(field="dos", key="DATE:", word_indexes=[key.index], box=key.box, weak=True,
                         region="header", trusted=True, value_words=words[1:],
                         value_text=" ".join(word.content for word in words[1:]))
            return [row for row in extract_page([hit], words, PAGE_H) if row.accepted]

        (spaced,), (plain,) = rows("DATE: 05 / 20 / 2025"), rows("DATE: 05/20/2025")
        assert (spaced.value, spaced.score) == ("05/20/2025", plain.score)

    def test_the_edge_label_rule_reads_it_like_any_other_date(self):
        import stages.lib.dos.stage  # noqa: F401  — puts dos/ on sys.path
        from dos_logic import detect_dos_per_page

        hit = detect_dos_per_page("===== 7.png =====\nDate of Service: 05 / 20 / 2025\n")[0]
        plain = detect_dos_per_page("===== 7.png =====\nDate of Service: 05/20/2025\n")[0]
        assert hit["dos_from_iso"] == "2025-05-20" and hit["confidence"] == plain["confidence"]


def test_a_spaces_only_date_is_never_read():
    from stages.lib.extraction.util.dates import find_dates

    assert find_dates("Date: 06 12 24") == [] and find_dates("WBC 10 12 20") == []


def test_dos_has_two_states_kv_then_edge():
    import stages.lib.dos.stage as stage
    from dos_logic import detect_dos_per_page

    assert stage.DOS_COLS[-1] == "source"
    edge_page = "===== 1.png =====\nVisit Date: 03/01/2024\n"
    kv = {"1.png": [{"iso": "2024-07-01", "keyword": "DOS", "confidence": 0.31}]}
    sources = [
        detect_dos_per_page(edge_page, kv_dates=kv)[0],
        detect_dos_per_page(edge_page)[0],
        detect_dos_per_page("===== 1.png =====\nno date\n")[0],
    ]
    assert [(h.get("page_source") or "none", h["confidence"]) for h in sources] == [
        ("kv", 0.31), ("edge", 0.5), ("none", 0.0)]


def test_dictation_and_transcription_dates_are_never_an_edge_dos_or_a_range():
    import stages.lib.dos.stage  # noqa: F401
    from dos_logic import detect_dos_per_page

    def dos(text):
        hit = detect_dos_per_page(f"===== 1.png =====\nNote.\n{text}\n")[0]
        return hit["dos_from_iso"], hit["dos_to_iso"]

    assert dos("DOS: 03/01/2024\nD: 03/01/2024 T: 03/04/2024") == ("2024-03-01", "2024-03-01")
    assert dos("Date of Service: 03/01/2024 - T: 03/04/2024") == ("2024-03-01", "2024-03-01")
    # Not an edge date; an "other" page takes it only as its last resort.
    assert dos("Visit Date DT: 03/04/2024") == ("2024-03-04", "2024-03-04")
    assert dos("D: 03/01/2024 T: 03/04/2024") == ("2024-03-01", "2024-03-01")
    # Real ranges are unchanged.
    assert dos("Date of Service: 03/01/2024 - 03/04/2024") == ("2024-03-01", "2024-03-04")
    assert dos("Admission Date: 03/01/2024 Discharge Date: 03/04/2024") == ("2024-03-01", "2024-03-04")


def test_the_model_takes_its_best_dos_at_any_score():
    from stages.lib.extraction.util import config

    assert config.SELECT_THRESHOLDS["dos"] == 0.0


def test_a_print_date_is_never_a_dos():
    from stages.lib.extraction.dos.extract import extract_page
    from stages.lib.extraction.util.keys import find_key_hits

    words = _page((500, "Print Date: 03/01/2024 Visit Date: 02/27/2024"))
    rows = extract_page(find_key_hits(words, PAGE_W, PAGE_H), words, PAGE_H)
    printed = [row for row in rows if row.tier == "printed"]
    assert printed and not any(row.accepted for row in printed)
    assert [row.value for row in rows if row.accepted] == ["02/27/2024"]


def _dos_page(text, candidates=None):
    import stages.lib.dos.stage  # noqa: F401
    from dos_logic import detect_dos_per_page

    hit = detect_dos_per_page(f"===== 1.png =====\n{text}\n",
                              kv_dates={"1.png": candidates} if candidates else None)[0]
    return hit["dos_from_iso"], hit["dos_to_iso"], hit["page_source"], hit["confidence"]


def _kv(iso, tier, score, keyword=""):
    return {"iso": iso, "tier": tier, "confidence": score, "keyword": keyword or tier, "model_scored": True}


class TestDischargeDocuments:
    STAY = [_kv("2024-02-01", "admit", 0.7, "Admit Date"), _kv("2024-02-05", "discharge", 0.9, "Discharge Date")]
    VISIT = [_kv("2024-02-03", "service", 0.8, "Visit Date")]

    def test_admit_and_discharge_take_priority_over_the_visit_date(self):
        assert _dos_page("DISCHARGE SUMMARY\nDoing well.", self.VISIT + self.STAY) == (
            "2024-02-01", "2024-02-05", "admit_discharge", 0.9)

    def test_edge_labels_fill_in_without_kv(self):
        assert _dos_page("Admission Date: 02/01/2024 Discharge Date: 02/05/2024") == (
            "2024-02-01", "2024-02-05", "admit_discharge", 0.5)

    def test_not_a_discharge_document_keeps_the_service_date(self):
        assert _dos_page("Progress note body.", self.VISIT + self.STAY[:1])[:3] == (
            "2024-02-03", "2024-02-03", "kv")

    def test_a_discharge_before_the_admit_is_ignored(self):
        stay = [_kv("2024-02-05", "admit", 0.6), _kv("2024-02-01", "discharge", 0.6)]
        assert _dos_page("Discharge summary", stay)[:2] == ("2024-02-05", "2024-02-05")


class TestKvPriority:
    def test_other_pages_an_accepted_service_date_beats_a_higher_scored_weak_date(self):
        assert _dos_page("Office visit.", [_kv("2024-03-01", "weak", 0.95, "Date"),
                                          _kv("2024-03-02", "service", 0.65, "Date of Service")])[:4] == (
            "2024-03-02", "2024-03-02", "kv", 0.65)

    def test_the_order_counts_only_above_the_acceptance_score(self):
        # Date of Service at 0.40 is not accepted (below 0.6): the accepted Date wins.
        assert _dos_page("Office visit.", [_kv("2024-03-01", "weak", 0.95, "Date"),
                                          _kv("2024-03-02", "service", 0.40, "Date of Service")])[0] == "2024-03-01"
        # Nothing accepted: the highest score, whatever its label.
        assert _dos_page("Office visit.", [_kv("2024-03-01", "service", 0.20), _kv("2024-03-02", "weak", 0.45)])[:4] == (
            "2024-03-02", "2024-03-02", "kv", 0.45)

    def test_rules_only_dates_all_count_as_accepted(self):
        rules = [{**_kv("2024-03-01", "weak", 0.92), "model_scored": False},
                 {**_kv("2024-03-02", "service", 0.55), "model_scored": False}]
        assert _dos_page("Office visit.", rules)[0] == "2024-03-02"

    def test_within_a_group_the_model_score_decides(self):
        assert _dos_page("Office visit.", [_kv("2024-03-01", "service", 0.3), _kv("2024-03-02", "service", 0.7)])[0] == "2024-03-02"

    def test_lab_service_then_collection_then_received_then_report(self):
        lab = [_kv("2024-04-04", "weak", 0.9, "Report Date"), _kv("2024-04-03", "received", 0.9),
               _kv("2024-04-02", "collected", 0.9)]
        assert _dos_page("LAB REPORT\nSpecimen: blood", lab)[0] == "2024-04-02"
        assert _dos_page("LAB REPORT", lab[:2])[0] == "2024-04-03"
        assert _dos_page("LAB REPORT", lab[:1])[0] == "2024-04-04"
        assert _dos_page("LAB REPORT", lab + [_kv("2024-04-01", "service", 0.7)])[0] == "2024-04-01"

    def test_other_pages_end_with_dictation_then_transcription(self):
        assert _dos_page("Note.\nT: 03/04/2024 D: 03/01/2024")[:4] == ("2024-03-01", "2024-03-01", "dictation", 0.3)
        assert _dos_page("Note.\nTranscribed: 03/04/2024")[0] == "2024-03-04"
        # A real date wins; a lab or discharge page never uses them.
        assert _dos_page("Note.\nD: 03/01/2024", [_kv("2024-02-27", "weak", 0.1)])[2] == "kv"
        assert _dos_page("LAB REPORT\nD: 03/01/2024")[2] == ""


def test_staged_best_by_tier():
    from stages.lib.extraction.staging import StagedPage

    rows = [
        {"value": "2/1/24", "tier": "admit", "rule_accepted": True, "model_score": 0.2},
        {"value": "2/3/24", "tier": "service", "rule_accepted": True, "model_score": 0.9},
    ]
    page = StagedPage("1.png", {"fields": {"dos": rows}})
    assert page.best("dos")["value"] == "2/3/24"
    assert page.best("dos", tier="admit")["value"] == "2/1/24"
    assert page.best("dos", tier="discharge") is None
