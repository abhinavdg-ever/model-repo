"""2026-11-01 (user): DOS rules -- amended dates dropped, exam dates last on a Progress
Note, Report Date as the discharge date, a keyword pass, the electronic-signature
fallback and consistency with neighbouring pages (core-pipeline/modules/dos/dos_logic.py)."""
import sys

from conftest import LIB

DOS = str(LIB / "dos")
if DOS not in sys.path:
    sys.path.insert(0, DOS)

from dos_logic import detect_dos_per_page  # noqa: E402


def _pages(*bodies, **kwargs):
    text = "".join(f"===== {i}.jpg =====\n{body}\n" for i, body in enumerate(bodies, 1))
    return detect_dos_per_page(text, **kwargs)


def _kv(key, tier, iso, sentence, model_score, raw=None, rule=0.8):
    return {"iso": iso, "iso_to": "", "raw": raw or iso, "sentence": sentence, "tier": tier, "keyword": key,
            "confidence": model_score, "model_scored": True, "rule_score": rule}


class TestAmended:
    def test_the_users_example_amended_is_dropped_the_next_option_used(self):
        rows = [
            _kv("Encounter", "service", "2025-07-23",
                "Encounter: Follow Up Amended 07/23/2025 11:32 by Christie Plan", 0.2028, raw="07/23/2025"),
            _kv("Date", "weak", "2025-03-04",
                "Date: 03/04/2025 Provider: Christina Encounter: Amended 07/23/2025 Plan", 0.1959, raw="03/04/2025"),
        ]
        page = "Date: 03/04/2025 Provider: Christina Encounter: Follow Up Amended 07/23/2025\nPlan reviewed."
        hit = _pages(page, kv_dates={"1.jpg": rows}, page_families={"1.jpg": "Progress Note"})[0]
        # The amended service date is gone; the weak Date is under the 0.3 floor, so the
        # keyword pass (floor relaxed) takes it.
        assert hit["dos_from_iso"] == "2025-03-04" and hit["match_type"] == "keyword_pass"

    def test_an_amended_edge_label_is_skipped(self):
        hit = _pages("Visit Date: Amended 07/23/2025\nnote")[0]
        assert hit["dos_from_iso"] != "2025-07-23"


class TestExamDateOnAProgressNote:
    def test_the_users_example_exam_dates_come_last(self):
        rows = [
            _kv("Exam Date", "service", "2024-12-12", "CT CHEST EXAM DATE: 12/12/2024 Impression", 0.4111),
            _kv("Exam Date", "service", "2024-08-05", "RENAL ULTRASOUND EXAM DATE: 8/5/2024", 0.6871, raw="8/5/2024"),
            _kv("Date", "weak", "2025-03-25", "Date: 03/25/2025 03:00 PM Visit Type:", 0.4459, raw="03/25/2025"),
        ]
        note = _pages("x", kv_dates={"1.jpg": rows}, page_families={"1.jpg": "Progress Note"})[0]
        assert note["dos_from_iso"] == "2025-03-25"
        lab = _pages("x", kv_dates={"1.jpg": rows}, page_families={"1.jpg": "Lab Report"})[0]
        assert lab["dos_from_iso"] == "2024-08-05"  # a lab / radiology page keeps its exam date


class TestReportDateAsDischarge:
    def test_admit_without_discharge_takes_the_report_date(self):
        rows = [_kv("Admission Date", "admit", "2024-03-01", "Admission Date: 03/01/2024", 0.9),
                _kv("Report Date", "weak", "2024-03-05", "Report Date: 03/05/2024", 0.6)]
        hit = _pages("x", kv_dates={"1.jpg": rows}, page_families={"1.jpg": "Discharge Summary"})[0]
        assert (hit["dos_from_iso"], hit["dos_to_iso"]) == ("2024-03-01", "2024-03-05")

    def test_a_report_date_before_admission_is_not_used(self):
        rows = [_kv("Admission Date", "admit", "2024-03-01", "Admission Date: 03/01/2024", 0.9),
                _kv("Report Date", "weak", "2024-02-20", "Report Date: 02/20/2024", 0.6)]
        hit = _pages("x", kv_dates={"1.jpg": rows}, page_families={"1.jpg": "Discharge Summary"})[0]
        assert hit["dos_to_iso"] == "2024-03-01"


class TestKeywordPass:
    def test_a_label_in_the_middle_of_a_progress_note(self):
        middle = " ".join(["word"] * 40) + " Date of Service: 04/02/2024 " + " ".join(["word"] * 40)
        assert _pages(middle, page_families={"1.jpg": "Lab Report"})[0]["dos_from_iso"] == ""
        hit = _pages(middle, page_families={"1.jpg": "Progress Note"})[0]
        assert hit["dos_from_iso"] == "2024-04-02"


class TestSignatureFallback:
    def test_no_dos_takes_the_last_signature_date(self):
        hit = _pages("note with no date", signature_dates={"1.jpg": ["2024-05-01", "2024-05-03"]})[0]
        assert (hit["dos_from_iso"], hit["match_type"]) == ("2024-05-03", "signature_date")

    def test_a_sure_dos_is_kept(self):
        rows = [_kv("Date of Service", "service", "2024-04-30", "Date of Service: 04/30/2024", 0.9)]
        hit = _pages("x", kv_dates={"1.jpg": rows}, signature_dates={"1.jpg": ["2024-05-03"]})[0]
        assert hit["dos_from_iso"] == "2024-04-30"


class TestNeighbours:
    def test_an_undated_page_between_two_agreeing_pages_takes_their_date(self):
        rows = {p: [_kv("Date of Service", "service", "2024-06-01", "Date of Service: 06/01/2024", 0.9)]
                for p in ("1.jpg", "3.jpg")}
        hits = _pages("a", "middle, no date", "c", kv_dates=rows)
        assert (hits[1]["dos_from_iso"], hits[1]["match_type"]) == ("2024-06-01", "neighbours")

    def test_disagreeing_neighbours_or_a_lab_page_change_nothing(self):
        rows = {"1.jpg": [_kv("Date of Service", "service", "2024-06-01", "Date of Service: 06/01/2024", 0.9)],
                "3.jpg": [_kv("Date of Service", "service", "2024-06-09", "Date of Service: 06/09/2024", 0.9)]}
        assert _pages("a", "b", "c", kv_dates=rows)[1]["dos_from_iso"] == ""
        same = {p: rows["1.jpg"] for p in ("1.jpg", "3.jpg")}
        lab = _pages("a", "b", "c", kv_dates=same, page_families={"2.jpg": "Lab Report"})
        assert lab[1]["dos_from_iso"] == ""


class TestProviderRoleWords:
    """2026-11-01 (user): Legal Authenticator / Signing Authority / Authority never in a provider name."""

    CASES = {
        "Legal Authenticator: John Smith, MD": "John Smith, MD",
        "Jane Doe Signing Authority": "Jane Doe",
        "Authority Mary Jones": "Mary Jones",
        "Electronically signed by: Sameera T Raza": "Sameera T Raza",
    }

    def test_the_pipeline_strips_them(self):
        from modules.names import trim_provider_name

        for raw, want in self.CASES.items():
            assert trim_provider_name(raw) == want, raw

    def test_review_ui_strips_them_from_older_rows(self):
        from app.services.imaging_overlays import provider_fields

        assert provider_fields("Legal Authenticator John Smith", "MD")[0] == "John Smith"
        assert provider_fields("Jane Doe Signing Authority", "")[0] == "Jane Doe"


class TestNonCodableCredentials:
    """2026-11-01 (user): a Codable page whose every credential is non-codable is Non-Codable in Final."""

    def test_credentials_are_judged_per_document(self):
        """2026-11-01 (user): a nurse's line on one page of a physician's note does not make
        that page Non-Codable."""
        from modules.imaging_final.stage import _document_signature
        from modules.non_codable_credentials import only_non_codable

        signatures = {1: {"provider_credentials": "MD"}, 2: {"provider_credentials": "RN"},
                      3: {"provider_credentials": "RN"}}
        assert only_non_codable(_document_signature([1, 2], signatures)) is None
        assert only_non_codable(_document_signature([3], signatures)) == ["RN"]

    def test_only_non_codable_credentials(self):
        from modules.non_codable_credentials import only_non_codable

        assert only_non_codable({"provider_credentials": "RN"}) == ["RN"]
        assert only_non_codable({"provider_credentials": "R.N., LPN"})
        assert only_non_codable({"provider_credentials": "MB ChB"})
        assert only_non_codable({"provider_credentials": "OA-C"}) and only_non_codable({"provider_credentials": "COTA"})
        assert only_non_codable({"provider_credentials": "MD, MS"}) is None  # an MD makes it codable
        assert only_non_codable({"provider_credentials": ""}) is None  # no credential: unchanged
        signers = [{"name": "A", "credentials": "PharmD"}, {"name": "B", "credentials": "DO"}]
        assert only_non_codable({"provider_credentials": "PharmD", "signers": signers}) is None


class TestStrayFallbackDates:
    def test_a_signature_date_outside_the_neighbours_dates_is_dropped(self):
        rows = {p: [_kv("Date of Service", "service", "2024-06-01", "Date of Service: 06/01/2024", 0.9)]
                for p in ("1.jpg",)}
        rows["3.jpg"] = [_kv("Date of Service", "service", "2024-06-09", "Date of Service: 06/09/2024", 0.9)]
        hits = _pages("a", "b", "c", kv_dates=rows, signature_dates={"2.jpg": ["2025-01-15"]})
        assert hits[1]["dos_from_iso"] == "" and hits[1]["match_type"] == "no_date_found"
        inside = _pages("a", "b", "c", kv_dates=rows, signature_dates={"2.jpg": ["2024-06-05"]})
        assert inside[1]["dos_from_iso"] == "2024-06-05"

    def test_a_label_in_the_middle_counts_on_a_note_in_the_label_step(self):
        middle = " ".join(["word"] * 40) + " Date of Service: 04/02/2024 " + " ".join(["word"] * 40)
        hit = _pages(middle, page_families={"1.jpg": "Progress Note"})[0]
        assert (hit["dos_from_iso"], hit["match_type"]) == ("2024-04-02", "edge_keyword")


class TestStrongLabelsAndEdges:
    """2026-11-01 (user): beyond the edges only a strong label ('Date of Service: ...'); a
    bare 'Date:' only in the first / last 20 words; extraction.json first."""

    FILL = " ".join(["word"] * 40)

    def test_a_bare_date_in_the_middle_is_not_taken_by_the_keyword_pass(self):
        rows = [_kv("Date", "weak", "2025-03-04", "Date: 03/04/2025", 0.2, raw="03/04/2025")]
        middle = f"{self.FILL} Date: 03/04/2025 {self.FILL}"
        assert _pages(middle, kv_dates={"1.jpg": rows}, page_families={"1.jpg": "Progress Note"})[0]["dos_from_iso"] == ""
        top = f"Date: 03/04/2025 {self.FILL} {self.FILL}"
        assert _pages(top, kv_dates={"1.jpg": rows}, page_families={"1.jpg": "Progress Note"})[0]["dos_from_iso"] == "2025-03-04"

    def test_a_weak_label_in_the_middle_needs_two_words_and_a_colon(self):
        loose = f"{self.FILL} date of service was 04/02/2024 {self.FILL}"
        assert _pages(loose, page_families={"1.jpg": "Progress Note"})[0]["dos_from_iso"] == ""
        single = f"{self.FILL} DOS: 04/02/2024 {self.FILL}"
        assert _pages(single, page_families={"1.jpg": "Progress Note"})[0]["dos_from_iso"] == ""


def test_a_leading_by_street_or_road_is_not_part_of_a_name():
    """2026-11-02 (user): member and provider names lose a leading By / Street / Road."""
    from modules.names import trim_name, trim_provider_name

    assert trim_name("By John Smith") == "John Smith"
    assert trim_name("Street: Mary Jones") == "Mary Jones"
    assert trim_name("ROAD Jane Doe") == "Jane Doe"
    assert trim_provider_name("Legal Authenticator By Jane Doe") == "Jane Doe"
    assert trim_name("Byron Smith") == "Byron Smith" and trim_name("John Street") == "John Street"
