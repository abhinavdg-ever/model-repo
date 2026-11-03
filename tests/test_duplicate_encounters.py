"""2026-10-29 (user): a far copy in a Progress Note / Discharge Summary is a duplicate only
when its whole encounter is a copy (core-pipeline/stages/lib/continuity/duplicates.py)."""
from types import SimpleNamespace

from stages.lib.continuity import duplicates


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
        rows.append({"segment_kind": "main", "document_seq": 1 if dup else doc})
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
            rows.append({"segment_kind": "main", "document_seq": doc})
            texts[pid] = _words(f"p{pid - 12}x") if dup else _words(f"p{pid}x")
        return pages, rows, texts

    def test_a_string_of_more_than_3_copied_pages_stays_a_duplicate(self):
        pages, rows, texts = self._long_chart({1, 2, 3, 4})  # pages 14-17 copy 2-5
        result = duplicates.review(pages, rows, texts, partial_of={})
        assert not result.demote and not result.release

    def test_a_string_of_3_or_fewer_is_judged_page_by_page(self):
        pages, rows, texts = self._long_chart({1, 2, 3})  # pages 14-16 copy 2-4
        result = duplicates.review(pages, rows, texts, partial_of={})
        assert result.demote.keys() == {14, 15, 16} and not result.release

    def test_a_single_page_not_matching_100_percent_is_carried_forward(self):
        pages, rows, texts = _chart({11})
        texts[11] = texts[11] + " added today"
        result = duplicates.review(pages, rows, texts, partial_of={})
        assert result.release.keys() == {11} and not result.demote

    def test_other_families_now_checked(self):
        for family in ("Operative Note", "Clinical Summary"):
            pages, rows, texts = _chart({11}, family=family)
            assert duplicates.review(pages, rows, texts, partial_of={}).demote.keys() == {11}
