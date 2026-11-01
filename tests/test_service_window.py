"""2026-11-01 (user): the requested DOS window from the manifest's service note
(review-ui services/service_window.py), for Rejection Rules -> Service Date."""
from app.services.service_window import service_window

NOTE = (
    "TRC - REQUIRED - Member demographic sheet: Name and date of birth, 2026 Evidence of discharge medication "
    "reconciliations, 2026 Hospital discharge summaries. Note: Inpatient notes should be only from one facility.\n"
    "BCS - REQUIRED - Include each and every DOS for Oct 2024 to Dec 2026. Evidence of a bilateral/Unilateral "
    "mastectomy anytime in the member's history through 12/31/2026."
)


def test_the_measures_own_section():
    assert service_window(NOTE, "TRC") == ("2026-01-01", "2026-12-31", "2026")
    assert service_window(NOTE, "BCS") == ("2024-10-01", "2026-12-31", "Oct 2024 to Dec 2026")


def test_written_date_ranges_and_nothing():
    assert service_window("Visits 01/01/2025 - 06/30/2025")[:2] == ("2025-01-01", "2025-06-30")
    assert service_window("February 2025 through April 2025")[:2] == ("2025-02-01", "2025-04-30")
    assert service_window("Send all progress notes") == (None, None, "")
    assert service_window(None) == (None, None, "")


def test_a_through_date_alone_is_not_a_window():
    # How far back evidence may go, not which visits are asked for.
    assert service_window("Evidence of a mastectomy anytime through 12/31/2026") == (None, None, "")
