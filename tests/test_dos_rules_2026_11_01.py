"""2026-11-01 (user): DOS rules -- amended dates dropped, exam dates last on a Progress
Note, Report Date as the discharge date, a keyword pass, the electronic-signature
fallback and consistency with neighbouring pages (core-pipeline/stages/lib/dos/dos_logic.py)."""
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
        hit = _pages("Follow up visit, plan reviewed.", kv_dates={"1.jpg": rows},
                     page_families={"1.jpg": "Progress Note"})[0]
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
        assert (hit["dos_from_iso"], hit["match_type"]) == ("2024-04-02", "keyword_pass")


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
        from stages.lib.names import trim_provider_name

        for raw, want in self.CASES.items():
            assert trim_provider_name(raw) == want, raw

    def test_review_ui_strips_them_from_older_rows(self):
        from app.services.imaging_overlays import provider_fields

        assert provider_fields("Legal Authenticator John Smith", "MD")[0] == "John Smith"
        assert provider_fields("Jane Doe Signing Authority", "")[0] == "Jane Doe"


class TestNonCodableCredentials:
    """2026-11-01 (user): a Codable page whose every credential is non-codable is Non-Codable in Final."""

    def test_only_non_codable_credentials(self):
        from stages.lib.non_codable_credentials import only_non_codable

        assert only_non_codable({"provider_credentials": "RN"}) == ["RN"]
        assert only_non_codable({"provider_credentials": "R.N., LPN"})
        assert only_non_codable({"provider_credentials": "MB ChB"})
        assert only_non_codable({"provider_credentials": "OA-C"}) and only_non_codable({"provider_credentials": "COTA"})
        assert only_non_codable({"provider_credentials": "MD, MS"}) is None  # an MD makes it codable
        assert only_non_codable({"provider_credentials": ""}) is None  # no credential: unchanged
        signers = [{"name": "A", "credentials": "PharmD"}, {"name": "B", "credentials": "DO"}]
        assert only_non_codable({"provider_credentials": "PharmD", "signers": signers}) is None
