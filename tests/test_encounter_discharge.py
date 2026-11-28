"""2026-11-28 (user): a document with a discharge-related page is Inpatient on every page."""
from __future__ import annotations

import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.encounter.encounter_classify import classify_pages  # noqa: E402


def _page(n, family, names=(), text="follow up visit in clinic", doc=1):
    return {"page_id": n, "page_name": f"{n}.png", "page_number": n, "text": text, "dos_from": "2025-01-02",
            "dos_to": "2025-01-02", "page_family": family, "page_type_name": family, "page_names": list(names),
            "document": doc}


def test_discharge_page_makes_the_document_inpatient():
    rows = classify_pages([_page(1, "Progress Note", ("Progress Note",)),
                           _page(2, "Discharge Summary", ("Discharge Summary", "Discharge Instruction"))])
    assert {r["encounter_type"] for r in rows} == {"inpatient"}
    assert rows[0]["decided_by"] == "discharge_page"


def test_without_a_discharge_page_nothing_changes():
    rows = classify_pages([_page(1, "Progress Note", ("Progress Note",), text="follow up visit in clinic")])
    assert rows[0]["decided_by"] != "discharge_page"
