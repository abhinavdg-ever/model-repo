"""Duplicate pages as Duplicate_Logic.md has them (2026-11-11, user): body text only, three
best earlier matches, hard blocks, exact at any distance, only exact within 3 pages, runs
(Rule A / Rule B) further apart, conflicts, "Duplicate of N" / "Copy of N"."""
from types import SimpleNamespace

import modules.blank_junk.stage as stage
from modules.blank_junk import dup_rules, method
from modules.continuity import duplicates as cont


def _letters(n: int) -> str:
    """12 -> 'bc': words without digits (a differing digit is the dates-and-numbers block)."""
    return "".join("abcdefghij"[int(d)] for d in str(n))


def note(seed: int, words: int = 120, swap: dict | None = None) -> str:
    """A page of ``words`` distinct words over several lines; ``swap`` replaces word i."""
    out = [f"w{_letters(seed)}x{_letters(i)}" for i in range(words)]
    for i, word in (swap or {}).items():
        out[i] = word
    return "\n".join(" ".join(out[i:i + 12]) for i in range(0, words, 12))


def other(n: int) -> str:
    return note(1000 + n)


def find(texts: dict[int, str], *, signed=(), signatures=None, needs_image=(), images_match=False, **kw):
    pages = [dup_rules.Page(page_id=pid, position=pid - 1, text=text, signed=pid in signed,
                            signature=(signatures or {}).get(pid, ("", "")), needs_image=pid in needs_image)
             for pid, text in sorted(texts.items())]
    return dup_rules.find(pages, image_match=lambda a, b: images_match, **kw)


def chart(n: int, **pages: str) -> dict[int, str]:
    texts = {i: other(i) for i in range(1, n + 1)}
    texts.update({int(k[1:]): v for k, v in pages.items()})
    return texts


class TestExact:
    def test_an_exact_copy_is_a_duplicate_at_any_distance(self):
        out = find(chart(60, p1=note(1), p55=note(1)))
        assert out[55].is_duplicate and out[55].original == 1 and out[55].rule == dup_rules.EXACT
        assert not out[1].label  # the original gets no label

    def test_stray_letters_and_punctuation_still_count_as_the_same(self):
        out = find(chart(30, p2=note(1), p20=note(1).replace("\n", " . |\n", 3) + " l"))
        assert out[20].is_duplicate and out[20].rule == dup_rules.EXACT

    def test_header_and_footer_lines_are_left_out(self):
        def paged(n):
            return f"Page {n} of 30\nPrinted on 11/03/2024 10:32\n{note(1)}\nFax: 555-123-4567 P.00{n}/030"
        out = find(chart(30, p2=paged(2), p20=paged(20)))
        assert out[20].is_duplicate and out[20].score == 1.0

    def test_several_exact_copies_all_point_at_the_earliest_page(self):
        out = find(chart(40, p3=note(1), p15=note(1), p30=note(1)))
        assert out[15].original == 3 and out[30].original == 3
        assert out[15].is_duplicate and out[30].is_duplicate


class TestWithinThreePages:
    def test_only_an_exact_match_counts(self):
        out = find(chart(10, p4=note(1), p6=note(1, swap={5: "different"})))
        assert not out[6].label

    def test_an_exact_neighbour_is_a_duplicate(self):
        out = find(chart(10, p4=note(1), p5=note(1)))
        assert out[5].is_duplicate and out[5].original == 4


class TestHardBlocks:
    def test_length(self):
        longer = note(1) + "\n" + " ".join(f"extra{_letters(i)}" for i in range(25))
        out = find(chart(30, p2=note(1), p20=longer))
        assert not out[20].label and out[20].closest["blocked_by"] == dup_rules.BLOCK_LENGTH

    def test_a_differing_date_or_number_blocks_a_run(self):
        base = {i: note(i) for i in (1, 2, 3)}
        texts = chart(30, p1=base[1], p2=base[2], p3=base[3],
                      p20=base[1], p21=note(2, swap={7: "05/06/2024"}), p22=base[3])
        out = find(texts)
        assert out[20].is_duplicate and out[22].is_duplicate
        assert not out[21].label  # its one differing word is a date

    def test_signature_blocks_differ_makes_a_copy(self):
        out = find(chart(30, p2=note(1), p20=note(1)), signed={20})
        assert out[20].label == "copy" and out[20].blocked_by == dup_rules.BLOCK_SIGNATURE
        out = find(chart(30, p2=note(1), p20=note(1)), signed={2, 20},
                   signatures={2: ("dr a", "2024-01-01"), 20: ("dr b", "2024-01-01")})
        assert out[20].label == "copy" and out[20].blocked_by == dup_rules.BLOCK_SIGNATURE

    def test_a_short_signed_page_is_never_a_duplicate_even_if_the_images_match(self):
        short = "progress note signed electronically by doctor"
        out = find(chart(30, p2=short, p20=short), signed={2, 20}, images_match=True)
        assert out[20].label == "copy" and out[20].blocked_by == dup_rules.BLOCK_SHORT_SIGNED

    def test_a_short_page_needs_its_image_to_match(self):
        short = "see attached results continued on next page"
        out = find(chart(30, p2=short, p20=short), images_match=False)
        assert out[20].label == "copy" and out[20].blocked_by == dup_rules.BLOCK_IMAGE
        out = find(chart(30, p2=short, p20=short), images_match=True)
        assert out[20].is_duplicate

    def test_a_form_or_handwritten_page_needs_its_image_to_match(self):
        out = find(chart(30, p2=note(1), p20=note(1)), needs_image={20}, images_match=False)
        assert out[20].label == "copy" and out[20].blocked_by == dup_rules.BLOCK_IMAGE

    def test_a_longer_signed_page_with_the_same_signature_can_be_a_duplicate(self):
        sig = {2: ("dr a", "2024-01-01"), 20: ("dr a", "2024-01-01")}
        out = find(chart(30, p2=note(1), p20=note(1)), signed={2, 20}, signatures=sig)
        assert out[20].is_duplicate


class TestRuns:
    def test_rule_a_grows_from_an_exact_duplicate(self):
        texts = chart(40, p5=note(5), p6=note(6), p25=note(5), p26=note(6, swap={3: "changed"}))
        out = find(texts)
        assert out[25].rule == dup_rules.EXACT
        # the page next to the exact one, about 98.3%: two pages, not three for Rule B
        assert out[26].is_duplicate and out[26].rule == dup_rules.RULE_A and out[26].original == 6

    def test_rule_a_needs_98_percent(self):
        texts = chart(40, p5=note(5), p6=note(6), p25=note(5),
                      p26=note(6, swap={i: f"new{_letters(i)}" for i in range(3)}))  # about 95%
        out = find(texts)
        assert out[25].is_duplicate and not out[26].label

    def test_rule_b_three_in_a_row(self):
        swap = {i: f"new{_letters(i)}" for i in range(3)}  # about 95% each
        texts = chart(40, p5=note(5), p6=note(6), p7=note(7),
                      p25=note(5, swap=swap), p26=note(6, swap=swap), p27=note(7, swap=swap))
        out = find(texts)
        assert all(out[p].is_duplicate and out[p].rule == dup_rules.RULE_B for p in (25, 26, 27))

    def test_rule_b_none_below_90_percent(self):
        swap, worse = {i: f"new{_letters(i)}" for i in range(2)}, {i: f"new{_letters(i)}" for i in range(14)}
        texts = chart(40, p5=note(5), p6=note(6), p7=note(7),
                      p25=note(5, swap=swap), p26=note(6, swap=worse), p27=note(7, swap=swap))
        out = find(texts)
        assert not any(out[p].label for p in (25, 26, 27))

    def test_runs_count_only_in_the_same_order(self):
        swap = {i: f"new{_letters(i)}" for i in range(3)}
        texts = chart(40, p5=note(5), p6=note(6), p7=note(7),
                      p25=note(7, swap=swap), p26=note(5, swap=swap), p27=note(6, swap=swap))
        out = find(texts)
        assert not any(out[p].label for p in (25, 26, 27))


class TestConflicts:
    def test_two_partial_matches_to_one_original_are_flagged_not_duplicates(self):
        a = {i: f"aa{_letters(i)}" for i in range(60, 63)}
        b = {i: f"bb{_letters(i)}" for i in range(120, 123)}
        # Long pages: the header / footer lines the three copies share are left out of all three.
        def n(seed, swap=None):
            return note(seed, words=360, swap=swap)

        texts = chart(60, p5=n(5), p6=n(6), p7=n(7),
                      p25=n(5, swap=a), p26=n(6, swap=a), p27=n(7, swap=a),
                      p45=n(5, swap=b), p46=n(6, swap=b), p47=n(7, swap=b))
        out = find(texts)
        for p in (25, 26, 27, 45, 46, 47):
            assert not out[p].is_duplicate and out[p].review == dup_rules.CONFLICT


class TestStage:
    def _rows(self, texts, signed=()):
        pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
        inputs = {i: method.PageInputs(text="", pass_no=2) for i in texts}
        for i in texts:
            inputs[i].content_bottom = 0.9
        best: dict = {}
        rows = stage._classify(pages, texts, set(texts), inputs=inputs, signed_ids=set(signed),
                               best_matches=best, find_duplicates=True)
        return {r["page_id"]: r for r in rows}, best

    def test_a_duplicate_row_and_its_record(self, monkeypatch):
        monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1}})
        rows, best = self._rows(chart(20, p2=note(1), p15=note(1)))
        assert rows[15]["flag"] == "duplicate" and rows[15]["duplicate_of"] == 2
        record = rows[15]["votes"]["duplicate_check"]
        assert record["label"] == "Duplicate of 2" and record["rule"] == "Exact" and record["duplicate"]

    def test_a_blocked_exact_match_is_a_copy_and_stays_main(self, monkeypatch):
        monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.9, "p_junk": 0.1}})
        rows, _ = self._rows(chart(20, p2=note(1), p15=note(1)), signed={15})
        row = rows[15]
        assert row["flag"] == "not_blank_junk" and row["duplicate_of"] == 2
        assert stage.SAME_CONTENT_MARK + "2" in row["reason"] and "signature" in row["reason"]
        assert row["votes"]["duplicate_check"]["label"] == "Copy of 2"

    def test_pass_1_marks_exact_copies_within_3_pages_only(self, monkeypatch):
        monkeypatch.setattr(stage, "model_scores", lambda text, docling=None: {"bert": {"p_keep": 0.5, "p_junk": 0.1}})
        texts = chart(20, p2=note(1), p4=note(1), p15=note(1))
        pages = [{"id": i, "page_name": f"{i}.jpg", "page_number": i} for i in texts]
        inputs = {i: method.PageInputs(text="", pass_no=1) for i in texts}
        rows = {r["page_id"]: r for r in stage._classify(pages, texts, set(texts), inputs=inputs,
                                                           find_duplicates=False, exact_near=True)}
        assert rows[4]["flag"] == "duplicate" and rows[4]["duplicate_of"] == 2
        assert rows[15]["flag"] != "duplicate"  # far away: pass 2


def _page(pid, kind="main", page_type="Progress Note", duplicate_of=None):
    return SimpleNamespace(page_id=pid, kind=kind, page_type=page_type, duplicate_of=duplicate_of)


def _row(dos=None):
    return {"final_dos_from": dos, "final_dos_to": dos, "segment_kind": "main" if dos else None}


class TestDateOfServiceBlock:
    def test_progress_notes_in_documents_with_different_dos_are_not_duplicates(self):
        pages = [_page(1), _page(2), _page(3), _page(4, "duplicate", duplicate_of=2)]
        rows = [_row("2024-01-01"), _row("2024-01-01"), _row("2024-05-05"), _row()]
        blocked = cont.dos_block(pages, rows, {})
        assert blocked == {4: (2, frozenset({"2024-05-05"}), frozenset({"2024-01-01"}))}

    def test_the_same_dos_or_another_page_type_stays_a_duplicate(self):
        pages = [_page(1), _page(2), _page(3), _page(4, "duplicate", duplicate_of=2)]
        rows = [_row("2024-01-01"), _row("2024-01-01"), _row("2024-01-01"), _row()]
        assert cont.dos_block(pages, rows, {}) == {}
        pages = [_page(1, page_type="Lab Report"), _page(2, page_type="Lab Report"), _page(3),
                 _page(4, "duplicate", duplicate_of=2)]
        rows = [_row("2024-01-01"), _row("2024-01-01"), _row("2024-05-05"), _row()]
        assert cont.dos_block(pages, rows, {}) == {}

    def test_a_copied_run_with_its_own_dates_uses_them(self):
        pages = [_page(1), _page(2), _page(3), _page(4, "duplicate", duplicate_of=1),
                 _page(5, "duplicate", duplicate_of=2)]
        rows = [_row("2024-01-01"), _row("2024-01-01"), _row("2024-05-05"), _row(), _row()]
        # the copied note carries its original's DOS: still a duplicate
        assert cont.dos_block(pages, rows, {4: frozenset({"2024-01-01"})}) == {}

    def test_the_reasons_round_trip(self):
        was = "duplicate_of_page_id:2 (Exact, score=1.0000, 5 pages apart)"
        reason = cont.blocked_reason(2, True, frozenset({"2024-05-05"}), frozenset({"2024-01-01"}), was)
        assert reason.startswith(cont.CONTENT_MARK + "2") and cont.DOS_NOTE in reason
        assert cont.restored_reason(reason, 2) == was
