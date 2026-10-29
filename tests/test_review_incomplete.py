"""2026-10-29 (user): marking a chart Reviewed warns about what its review still lacks."""
from app.services import ground_truth_updates as updates


class _Cursor:
    def __init__(self, counts, tables, rows):
        self.counts, self.tables, self.rows, self._next = counts, tables, rows, None

    def execute(self, sql, params=None):
        if "to_regclass" in sql:
            self._next = (params[0] in self.tables,)
        elif "EXISTS" in sql:
            table = sql.split("FROM ", 1)[1].split()[0]
            self._next = (table in self.rows,)
        else:
            self._next = self.counts

    def fetchone(self):
        return self._next


def test_every_missing_part_is_listed():
    cur = _Cursor((10, 7, 0), {"encounter_review", "chart_ground_truth"}, set())
    assert updates._missing(cur, "c") == [
        "Page Details: 3 of 10 pages have no ground truth",
        "Page Sequencing: not saved",
        "Encounter: no encounters saved",
        "Rejection Rules: no Accept / Reject decision",
    ]


def test_a_complete_review_lists_nothing():
    cur = _Cursor((10, 10, 10), {"encounter_review", "chart_ground_truth"},
                  {"encounter_review", "chart_ground_truth"})
    assert updates._missing(cur, "c") == []
    cur = _Cursor((10, 10, 4), {"encounter_review", "chart_ground_truth"},
                  {"encounter_review", "chart_ground_truth"})
    assert updates._missing(cur, "c") == ["Page Sequencing: 6 of 10 pages have no sequence"]
