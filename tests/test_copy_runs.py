"""2026-11-06 (user): a block of pages copying a block of pages gets one label (not a mix of
Duplicate / Partial / Repeated Content / No), and a consent page never joins a Progress Note."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import module.blank_junk.stage as bj_stage  # noqa: E402,F401  (puts blank_junk on sys.path)
from classify import copy_runs  # noqa: E402
from module.blank_junk.stage import _duplicate_rules, _settle_copy_runs  # noqa: E402
from module.continuity import duplicates  # noqa: E402
from module.continuity.engine import Line, PageInput, assign  # noqa: E402


def _words(tag, n=80):
    return " ".join(f"{tag}{i}" for i in range(n))


class TestCopyRuns:
    def test_consecutive_pages_with_one_offset_are_a_run(self):
        order = list(range(1, 15))
        target = {8: 1, 9: 2, 10: 3, 11: 4, 13: 2}
        assert copy_runs(order, target) == [[(8, 1), (9, 2), (10, 3), (11, 4)]]

    def test_a_single_copied_page_is_no_run(self):
        assert copy_runs([1, 2, 3, 4], {4: 1}) == []


def _swap(text, pid, n=3):
    words = text.split()
    return " ".join(words[:-n] + [f"swap{pid}w{i}" for i in range(n)])


def _block_chart(dates_on=()):
    """Pages 1-7 and their copies 8-14. Pages 10 and 11 add a few words (95-98% alike)."""
    pages = [{"id": pid} for pid in range(1, 15)]
    texts = {pid: _words(f"p{pid}x") for pid in range(1, 8)}
    for pid in range(8, 15):
        texts[pid] = texts[pid - 7]
    texts[10] += " extra1 extra2"
    texts[11] += " extra3 extra4"
    for pid in dates_on:
        texts[pid] += " printed 8/15/2025"
    return pages, texts


def _settle(pages, texts, *, dup_of, partial_of, content_of, best=None):
    dup_sim, dup_contained, partial_sim, content_score, pair_words = {}, {}, {}, {}, {}
    for pid in partial_of:
        partial_sim[pid] = 0.97
    for pid in content_of:
        content_score[pid] = (1.0, 0)
    _settle_copy_runs(pages, texts, _duplicate_rules(), {}, best or {},
                      dup_of, dup_sim, dup_contained, partial_of, partial_sim, content_of, content_score,
                      pair_words)
    return dup_of, partial_of, content_of


class TestBlankJunkRun:
    def test_a_mixed_run_that_is_a_copy_is_all_duplicates(self):
        pages, texts = _block_chart(dates_on=(8,))  # one page's printed date differs
        dup_of, partial_of, content_of = _settle(
            pages, texts, dup_of={12: 5, 13: 6, 14: 7}, partial_of={10: 3, 11: 4}, content_of={8: 1},
            best={9: {"page_id": 2, "similarity": 1.0, "blocked_by": "signature page apart"}},
        )
        assert dup_of == {pid: pid - 7 for pid in range(8, 15)}
        assert not partial_of and not content_of

    def test_dates_differing_on_half_the_run_make_it_repeated_content(self):
        pages, texts = _block_chart(dates_on=(8, 9, 12, 13))
        dup_of, partial_of, content_of = _settle(
            pages, texts, dup_of={14: 7}, partial_of={10: 3, 11: 4}, content_of={8: 1, 9: 2, 12: 5, 13: 6},
        )
        assert content_of == {pid: pid - 7 for pid in range(8, 15)}
        assert not dup_of and not partial_of

    def test_a_run_below_the_bar_is_all_partials(self):
        pages, texts = _block_chart()
        for pid in (8, 9, 12, 13, 14):  # three words swapped: about 93% alike
            texts[pid] = _swap(texts[pid], pid)
        dup_of, partial_of, content_of = _settle(
            pages, texts, dup_of={12: 5, 13: 6}, partial_of={8: 1, 9: 2, 10: 3, 11: 4, 14: 7}, content_of={},
        )
        assert partial_of == {pid: pid - 7 for pid in range(8, 15)}
        assert not dup_of and not content_of


def _cont_page(pid, kind="main", duplicate_of=None):
    return SimpleNamespace(page_id=pid, kind=kind, page_type="Progress Note", duplicate_of=duplicate_of)


class TestContinuityRun:
    """The same encounter (same DOS): the run is judged as one, not 100% page by page."""

    @staticmethod
    def _chart():
        pages, rows, texts = [], [], {}
        for pid in range(1, 15):
            dup = pid >= 8
            pages.append(_cont_page(pid, "duplicate" if dup else "main", pid - 7 if dup else None))
            rows.append({"segment_kind": None if dup else "main", "document_seq": None if dup else 1})
            texts[pid] = _words(f"p{pid - 7 if dup else pid}x")
        return pages, rows, texts

    def test_a_copied_run_stays_duplicates(self):
        pages, rows, texts = self._chart()
        texts[10] += " extra1 extra2"
        dos = {pid: frozenset({"2025-08-13"}) for pid in range(1, 15)}
        result = duplicates.review(pages, rows, texts, partial_of={}, extracted_dos=dos)
        assert not result.partial

    def test_a_run_below_the_bar_is_all_partials(self):
        pages, rows, texts = self._chart()
        for pid in range(8, 13):
            texts[pid] = _swap(texts[pid], pid)
        dos = {pid: frozenset({"2025-08-13"}) for pid in range(1, 15)}
        result = duplicates.review(pages, rows, texts, partial_of={}, extracted_dos=dos)
        assert set(result.partial) == set(range(8, 15))


STICKY = "08/13/2025 - office visit in urology associates (continued)"


def _page(n, page_type, headers=()):
    lines = [Line(STICKY, 0.03, 0.05), Line(f"k{n}v{n}j", 0.30, 0.31), Line(f"q{n}x{n}z", 0.79, 0.80)]
    return PageInput(
        page_id=n, page_name=f"{n}.jpg", page_number=n, text="\n".join(l.text for l in lines), lines=lines,
        page_type=page_type, dos_from="2025-08-13", dos_trust=5,
        section_headers=[{"text": h, "matched_canonical": h} for h in headers],
    )


class TestConsentBreak:
    def test_a_consent_page_never_joins_a_progress_note(self):
        pages = [_page(1, "Progress Note"), _page(2, "Progress Note"),
                 _page(3, "Consent and Authorization"), _page(4, "Progress Note")]
        rows = assign(pages)
        assert [r["relation"] for r in rows] == ["new_document", "continue", "new_document", "new_document"]
        assert "consent_boundary" in str(rows[2]["signals"]["before"])

    def test_a_consent_header_counts_too(self):
        pages = [_page(1, "Progress Note"), _page(2, "Patient Communication", headers=("Consent form",))]
        rows = assign(pages)
        assert rows[1]["relation"] == "new_document"
