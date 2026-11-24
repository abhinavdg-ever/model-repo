"""The running header / footer's hard rules (2026-11-24, user): a CCD print of several visits, each
page headed with its Encounter Id and Encounter Date and numbered 1, 2, 3 at the foot. Made-up
values, shaped like the user's chart."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.continuity import running_header as rh  # noqa: E402
from modules.continuity.engine import Line, PageInput, assign  # noqa: E402

# Four visits: (encounter id, encounter date, pages).
VISITS = [("1111111", "January 15, 2025", 3), ("2222222", "April 04, 2025", 2),
          ("3333333", "May 10, 2025", 2), ("4444444", "January 22, 2026", 3)]
SAME_BODY = ["medications", "albuterol sulfate inhaler 2 puffs every 4 hours as needed", "vital signs",
             "allergies", "problems", "moderate persistent asthma active", "social history never smoker"]


def ccd_page(n, enc, date, number, *, body=None, signed=False):
    lines = [Line(f"patient name: madeup, person date of birth: 01/01/1950 sex: f encounter id:{enc} "
                  f"encounter date:{date}".lower(), 0.02, 0.03)]
    for i, text in enumerate(body or SAME_BODY):
        lines.append(Line(text, 0.20 + i * 0.05, 0.21 + i * 0.05))
    lines.append(Line(str(number), 0.97, 0.98))
    return PageInput(page_id=n, page_name=f"{n}.jpg", page_number=n, text="\n".join(l.text for l in lines),
                     lines=lines, page_type="Progress Note", signed=signed)


def chart():
    pages, n, starts = [], 0, []
    for enc, date, count in VISITS:
        starts.append(n + 1)
        for k in range(1, count + 1):
            n += 1
            pages.append(ccd_page(n, enc, date, k))
    return pages, starts


def documents(rows):
    seqs = [r["document_seq"] for r in rows]
    return [i + 1 for i, s in enumerate(seqs) if i == 0 or s != seqs[i - 1]]


class TestReading:
    def test_ids_dates_and_numbers(self):
        head = ["Patient Name: X  Encounter Id:2033825  Encounter Date: January 15, 2025"]
        assert rh.header_ids(head) == {"2033825"}
        assert rh.header_date(head) == "2025-01-15"
        assert rh.header_date(["Visit Date: 4/4/25"]) == "2025-04-04"
        assert rh.header_ids(["Encounter Date: 01/02/2025"]) == frozenset()
        assert rh.footer_number(["Printed 05/21/2026", "2"]) == 2
        assert rh.footer_number(["Page 3"]) == 3
        assert rh.footer_number(["Page 3 of 7"]) is None  # 'N of M' is pagination's

    def test_decisions(self):
        a, b = ["encounter id: 111111"], ["encounter id: 222222"]
        assert rh.decision(a, b, None, None)[0] is True
        assert rh.decision(a, a, ["1"], ["2"])[:2] == (False, "page_number_follows")
        assert rh.decision(a, a, ["2"], ["1"])[:2] == (True, "page_number_restart")
        assert rh.decision(a, a, ["1"], ["2"], a_signed_ends=True) is None
        assert rh.decision(["encounter date: 01/15/2025"], ["encounter date: 04/04/2025"], None, None)[1] == \
            "header_date_change"
        assert rh.decision(a, a, None, None) is None


class TestChart:
    def test_four_visits_four_documents(self):
        pages, starts = chart()
        assert documents(assign(pages)) == starts

    def test_the_rules_overrule_the_clue_model(self):
        # The model joins everything and splits inside a visit: the header / footer decide.
        pages, starts = chart()
        model = {}
        for a, b in zip(pages, pages[1:]):
            model[(a.page_id, b.page_id)] = {"break": b.page_id in (2, 5), "p_break": 0.6, "score": 0.8,
                                             "decided_by": "model", "evidence": "made up"}
        rows = assign(pages, model=model)
        assert documents(rows) == starts
        assert rows[3]["decided_by"] == "decisive"

    def test_same_visit_same_header_numbers_follow(self):
        body2 = ["plan", "follow up in 6 months"]
        pages = [ccd_page(1, "1111111", "January 15, 2025", 1),
                 ccd_page(2, "1111111", "January 15, 2025", 2, body=body2)]
        assert documents(assign(pages)) == [1]


def test_a_signed_ccd_page_one_and_its_page_two_stay_one_document():
    """2026-11-24 (user: two Progress Notes with the same DOS split): page 1 of a CCD print carries
    'Legal authenticator ... signed at' and page 2 opens with a note heading; the clue model split
    them. Same encounter id, footer 1 -> 2: one document."""
    prev = ccd_page(38, "1111111", "March 17, 2026", 3, signed=True)
    a = ccd_page(39, "2222222", "April 01, 2026", 1, body=["legal authenticator signed at april 10, 2026",
                                                            "allergies"], signed=True)
    b = ccd_page(40, "2222222", "April 01, 2026", 2, body=["patient care coordination note", "problem"],
                 signed=True)
    model = {(38, 39): {"break": True, "p_break": 0.9, "score": 4.0, "decided_by": "model", "evidence": ""},
             (39, 40): {"break": True, "p_break": 0.7, "score": 1.7, "decided_by": "model", "evidence": ""}}
    assert documents(assign([prev, a, b], model=model)) == [1, 2]
