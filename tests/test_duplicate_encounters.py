"""2026-10-29 (user): a far copy in a Progress Note / Discharge Summary is a duplicate only
when its whole encounter is a copy (core-pipeline/module/continuity/duplicates.py)."""
from types import SimpleNamespace

from module.continuity import duplicates


def _page(pid, kind="main", family="Progress Note", duplicate_of=None):
    return SimpleNamespace(page_id=pid, kind=kind, page_type=family, duplicate_of=duplicate_of)


def _words(tag, n=60):
    return " ".join(f"{tag}{i}" for i in range(n))


def _chart(copied_pages, family="Progress Note"):
    """Pages 1-3: document 1 (the anchor). Pages 4-9: other documents. Pages 10-12:
    document 2, in which ``copied_pages`` are duplicates of pages 1-3."""
    pages, rows, texts = [], [], {}
    for pid in range(1, 13):
        doc = 1 if pid <= 3 else 2 if pid >= 10 else 100 + pid
        source = pid - 9 if pid >= 10 else None
        dup = pid in copied_pages
        pages.append(_page(pid, "duplicate" if dup else "main", family, source if dup else None))
        # A duplicate is in no document (continuity's rows: segment_kind None).
        rows.append({"segment_kind": None if dup else "main", "document_seq": None if dup else doc})
        texts[pid] = _words(f"p{source}x") if source and (dup or pid in copied_pages) else _words(f"p{pid}x")
    for pid in copied_pages:
        texts[pid] = _words(f"p{pid - 9}x")
    return pages, rows, texts


def test_one_copied_page_in_another_encounter_is_duplicate_content():
    pages, rows, texts = _chart({11})
    result = duplicates.review(pages, rows, texts, partial_of={})
    assert result.demote.keys() == {11} and result.demote[11][0] == 2
    assert result.demote[11][1] <= 0.98


def test_a_copied_encounter_stays_a_duplicate_and_its_partials_join():
    pages, rows, texts = _chart({10, 12})
    texts[11] = _words("p2x")  # page 11 copies page 2 too, but was only a partial
    result = duplicates.review(pages, rows, texts, partial_of={11: 2})
    assert not result.demote
    assert result.promote.keys() == {11}


def test_near_or_same_dos_or_other_types_are_not_checked():
    pages, rows, texts = _chart({11})
    pages[10].duplicate_of = 9  # page 9: two pages away
    assert not duplicates.review(pages, rows, texts, partial_of={}).demote
    pages, rows, texts = _chart({11})
    same = {11: frozenset({"2024-03-15"}), 2: frozenset({"2024-03-15"})}
    assert not duplicates.review(pages, rows, texts, partial_of={}, extracted_dos=same).demote
    pages, rows, texts = _chart({11}, family="Lab Report")
    assert not duplicates.review(pages, rows, texts, partial_of={}).demote


class TestStringsAndCarriedForward:
    """2026-11-02 / 11-03 (user): a whole encounter copy or a string of more than 3 copied pages
    stays a duplicate; any other copied page is duplicate content at a 100% match, else
    carried-forward content (No)."""

    @staticmethod
    def _long_chart(copied):
        """Pages 1-6: document 1. Pages 7-12: other documents. Pages 13-20: document 2, in
        which page 13 + c copies page 1 + c for every c in ``copied``."""
        pages, rows, texts = [], [], {}
        for pid in range(1, 21):
            doc = 1 if pid <= 6 else 2 if pid >= 13 else 100 + pid
            dup = pid - 13 in copied
            pages.append(_page(pid, "duplicate" if dup else "main", "Progress Note", pid - 12 if dup else None))
            rows.append({"segment_kind": None if dup else "main", "document_seq": None if dup else doc})
            texts[pid] = _words(f"p{pid - 12}x") if dup else _words(f"p{pid}x")
        return pages, rows, texts

    def test_a_string_of_copied_pages_is_judged_page_by_page(self):
        """2026-11-03 (user): only a whole copied encounter stays a duplicate; no string rule."""
        pages, rows, texts = self._long_chart({1, 2, 3, 4})  # pages 14-17 copy 2-5
        result = duplicates.review(pages, rows, texts, partial_of={})
        assert result.demote.keys() == {14, 15, 16, 17} and not result.release

    def test_a_string_of_3_or_fewer_is_judged_page_by_page(self):
        pages, rows, texts = self._long_chart({1, 2, 3})  # pages 14-16 copy 2-4
        result = duplicates.review(pages, rows, texts, partial_of={})
        assert result.demote.keys() == {14, 15, 16} and not result.release

    def test_a_single_page_not_matching_100_percent_is_carried_forward(self):
        pages, rows, texts = _chart({11})
        texts[11] = texts[11] + " added today"
        result = duplicates.review(pages, rows, texts, partial_of={})
        assert result.release.keys() == {11} and not result.demote

    def test_the_note_families(self):
        """2026-11-03 (user): Progress, Operative, Procedure Notes, Discharge and Clinical Summaries."""
        for family in ("Operative Note", "Procedure Note", "Discharge Summary", "Clinical Summary"):
            pages, rows, texts = _chart({11}, family=family)
            assert duplicates.review(pages, rows, texts, partial_of={}).demote.keys() == {11}
        pages, rows, texts = _chart({11}, family="Demographics")
        assert not duplicates.review(pages, rows, texts, partial_of={}).demote  # stays a Duplicate

    def test_a_copied_encounter_with_other_dates_is_not_a_duplicate(self):
        pages, rows, texts = _chart({10, 11, 12})  # document 2 (pages 10-12) copies document 1
        assert not duplicates.review(pages, rows, texts, partial_of={}).demote  # a whole copy
        dos = {pid: frozenset({"2024-03-15"}) for pid in (1, 2, 3)} | {10: frozenset({"2025-01-02"})}
        result = duplicates.review(pages, rows, texts, partial_of={}, extracted_dos=dos)
        assert {11, 12} <= result.demote.keys()  # dates differ: each page judged on its own


def test_a_same_encounter_copy_is_a_duplicate_at_100_and_a_partial_below():
    """2026-11-03 (user): within one encounter, 100% = Duplicate, 95-99.9% = Partial."""
    pages, rows, texts = _chart({11})
    pages[10].duplicate_of = 9  # page 11 copies page 9, two pages away: the same encounter
    texts[11] = texts[9]
    assert not duplicates.review(pages, rows, texts, partial_of={}).partial  # 100%: stays
    texts[11] = texts[9] + " one more"
    assert duplicates.review(pages, rows, texts, partial_of={}).partial.keys() == {11}


class TestHalfExactAt95:
    """2026-11-06 (user): an encounter averaging 95% or more with at least half its pages an
    exact match is a duplicate too (was: an average of 98% only)."""

    @staticmethod
    def _near(tag):  # 57 of 63 distinct words shared with the page: overlap about 0.905
        return " ".join(f"{tag}{i}" for i in range(57)) + " " + " ".join(f"new{tag}{i}" for i in range(3))

    def _encounter(self, page_10, page_12):
        """Document 2 = pages 10-12 around the duplicate page 11 (an exact copy of page 2)."""
        pages, rows, texts = _chart({11})
        texts[10], texts[12] = page_10, page_12
        return duplicates.review(pages, rows, texts, partial_of={})

    def test_two_exact_pages_of_three_at_97_percent_is_a_duplicate(self):
        result = self._encounter(_words("p1x"), self._near("p3x"))  # (1 + 1 + 0.905) / 3 = 0.968
        assert 0.95 <= result.checked[11][1] < 0.98
        assert not result.demote and not result.release

    def test_one_exact_page_of_three_below_95_is_not(self):
        result = self._encounter(self._near("p1x"), self._near("p3x"))  # (0.905 + 1 + 0.905) / 3 = 0.937
        assert result.demote.keys() == {11}
