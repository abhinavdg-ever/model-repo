"""Manual Review scores Page Type with the client-label canon, as the Accuracy View does
(2026-11-16, user). The backend reads its own copies of the frontend's canon files."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "review-ui" / "backend"))

from app.services.excel_compare.field_comparator import compare_fields  # noqa: E402
from app.services.excel_compare.page_type_canon import our_path, verdict  # noqa: E402


def test_backend_canon_copies_match_the_frontend():
    for name in ("gtPageTypeCanon.json", "pageTaxonomy.json"):
        front = (ROOT / "review-ui" / "frontend" / "src" / name).read_bytes()
        back = (ROOT / "review-ui" / "backend" / "app" / "data" / name).read_bytes()
        assert front == back, f"copy review-ui/frontend/src/{name} to review-ui/backend/app/data/"


def test_our_path_reads_nested_and_bracketed_sub_types():
    assert our_path("Lab Report", "Diagnostic Study (Echocardiogram Report)") == [
        "labreport", "diagnosticstudy", "echocardiogramreport"]
    assert our_path("Lab Report", "Diagnostic Study) (Echocardiogram Report") == [
        "labreport", "diagnosticstudy", "echocardiogramreport"]
    assert our_path("Procedure Note", "Endoscopy (EGD)") == ["procedurenote", "endoscopyegd"]
    assert our_path("Problem List", "Problem List") == ["problemlist", "problemlist"]
    assert our_path("Invoice", "Invoice Page") == ["administrative", "billinginvoices"]


def test_verdicts_as_on_the_accuracy_view():
    assert verdict("Echo Report", "Lab Report", "Diagnostic Study (Echocardiogram Report)") == "match"
    assert verdict("Radiology Report", "Lab Report", "Radiology Report") == "match"
    assert verdict("progressNotes", "Progress Note", "Medication List") == "match"
    assert verdict("Memo", "Patient Communication", "Patient Message") == "match"
    assert verdict("Questionnaire", "Forms and Questionnaires", "Assessment") == "match"
    assert verdict("Invoice", "Invoice", "Invoice Page") == "match"
    assert verdict("Holter Report", "Lab Report", "Diagnostic Study") == "partial"
    assert verdict("Echo Report", "Lab Report", "Radiology Report") == "partial"
    assert verdict("Progress Note", "Lab Report", "Lab Result") == "mismatch"
    assert verdict("Some New Label Nobody Mapped", "Lab Report", "Lab Result") is None


def test_compare_fields_page_type_full_partial_and_text_fallback():
    def page_type(gt, algo):
        return compare_fields({"Page Type": gt}, algo, ["Page Type"])["Page Type"]

    full = page_type("Echo Report", {"Page Type": "Lab Report", "Page Subtype": "Diagnostic Study (Echocardiogram Report)"})
    assert full["match"] is True and "partial" not in full

    part = page_type("Holter Report", {"Page Type": "Lab Report", "Page Subtype": "Diagnostic Study"})
    assert part["match"] is False and part["partial"] is True

    # The Extracted value matching is enough, as on the Accuracy View.
    extracted = page_type("Holter Report", {
        "Page Type": "Lab Report", "Page Subtype": "Diagnostic Study",
        "Extracted Value: Page Type": "Lab Report",
        "Extracted Value: Page Subtype": "Diagnostic Study (Holter Monitor Report)",
    })
    assert extracted["match"] is True

    # A label the canon does not know is compared as text, as before.
    assert page_type("Procedure Notes", {"Page Type": "Procedure Note", "Page Subtype": ""})["match"] is True
