"""Document continuity: the gap scorer that splits a chart into documents
(stages/lib/continuity, docs/APPROACH.md §4)."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from stages.lib.continuity.engine import (  # noqa: E402
    DECIDED_BY,
    Line,
    PageInput,
    assign,
    load_rules,
    pagination,
    text_lines,
    trust_points,
)
from stages.lib.continuity.layout import lines_from_final1, lines_from_final2  # noqa: E402
from stages.lib.continuity.signals import fill_value, normalized_line  # noqa: E402

BANNER = "patient: jane doe dob 01/02/1950 mrn 123456"
FOOTER = "community clinic 100 main street springfield"
RULES = load_rules()


def page(n, body="", *, head=BANNER, foot=FOOTER, ends=0.80, **kw):
    """A located page: head line at the top, body ending ``ends`` down the page, foot at
    the bottom. ``ends`` 0.80 is where the page-fill curve says nothing."""
    lines = []
    if head:
        lines.append(Line(head, 0.03, 0.05))
    for i, text in enumerate((body or f"k{n}v{n}j").splitlines()):
        lines.append(Line(text.lower(), 0.30 + i * 0.02, 0.31 + i * 0.02))
    # A closing line of its own, so pages share no wording unless a test gives them some.
    lines.append(Line(f"q{n}x{n}z", ends - 0.01, ends))
    if foot:
        lines.append(Line(foot, 0.95, 0.97))
    return PageInput(
        page_id=n, page_name=f"{n}.jpg", page_number=n,
        text="\n".join(l.text for l in lines), lines=lines, **kw,
    )


def bare(n, body="", **kw):
    return page(n, body, head="", foot="", **kw)


def junk(n, subtype="Record Request/Transmittal", confidence=0.96, **kw):
    return bare(n, f"records request {n}", kind="junk", junk_subtype=subtype, junk_confidence=confidence, **kw)


def gap(row):
    return row["signals"]["before"]


class TestDecisive:
    def test_page_numbers_running_on_join(self):
        rows = assign([bare(1, printed=(1, 2)), bare(2, printed=(2, 2))])
        assert rows[1]["relation"] == "continue" and rows[1]["decided_by"] == "decisive"
        assert gap(rows[1])["pair"]["pagination_follows"] == -5
        assert rows[1]["confidence_level"] == "high" and rows[1]["confidence"] >= 0.9

    def test_a_different_labelled_date_breaks(self):
        rows = assign([bare(1, dos_from="2024-01-01", dos_trust=5), bare(2, dos_from="2024-02-01", dos_trust=5)])
        assert (rows[1]["relation"], rows[1]["decided_by"]) == ("new_document", "decisive")

    def test_an_edge_keyword_date_is_strong_not_decisive(self):
        rows = assign([bare(1, dos_from="2024-01-01", dos_trust=5), bare(2, dos_from="2024-02-01", dos_trust=3)])
        assert gap(rows[1])["pair"]["different_dos"] == 3 and rows[1]["decided_by"] == "sum"

    def test_the_same_date_joins(self):
        rows = assign([bare(1, dos_from="2024-01-01", dos_trust=5), bare(2, dos_from="2024-01-01", dos_trust=5)])
        assert rows[1]["relation"] == "continue" and gap(rows[1])["pair"]["same_dos"] == -5

    def test_a_date_beats_page_numbers_at_medium_confidence(self):
        rows = assign([bare(1, dos_from="2024-01-01", dos_trust=5, printed=(1, 2)),
                       bare(2, dos_from="2024-02-01", dos_trust=5, printed=(2, 2))])
        assert rows[1]["relation"] == "new_document" and rows[1]["decided_by"] == "decisive_conflict"
        assert rows[1]["confidence_level"] == "medium" and rows[1]["review_required"]

    def test_dos_trust_by_how_the_date_was_found(self):
        assert [trust_points(t, RULES) for t in ("service", "admit", "weak", "keyless", "collected", None)] == [5, 5, 3, 2, 3, 3]


class TestEvidence:
    def test_a_signed_last_page_then_a_title_breaks(self):
        rows = assign([bare(1, signed=True, signed_in_tail=True, printed=(2, 2)), bare(2, title_hit=True)])
        before = gap(rows[1])
        assert rows[1]["relation"] == "new_document"
        assert before["ending"]["signed_in_tail"] == 3 and before["ending"]["signed_anywhere"] == 2
        assert before["ending"]["last_numbered_page"] == 3 and before["opening"]["has_title"] == 3

    def test_the_page_fill_curve(self):
        cfg = RULES.fill
        assert fill_value(0.95, cfg) == -1 and fill_value(0.60, cfg) == 1 and fill_value(0.20, cfg) == 0
        assert -1 < fill_value(0.86, cfg) < 0 and 0 < fill_value(0.72, cfg) < 1 and 0 < fill_value(0.42, cfg) < 1

    def test_a_tie_joins_and_is_flagged(self):
        rows = assign([bare(1), bare(2)])
        assert rows[1]["relation"] == "continue" and rows[1]["decided_by"] == "tie"
        assert rows[1]["review_required"]

    def test_header_lines_match_once_numbers_are_placeholders(self):
        assert normalized_line("Smith, John | MRN 004812 | Printed 03/28/24 | Page 3 of 7") == (
            "smith, john | mrn # | printed <date> | page # of #")
        rows = assign([page(1, head="smith john mrn 004812 printed 03/28/24 visit summary"),
                       page(2, head="smith john mrn 004812 printed 03/29/24 visit summary")])
        assert gap(rows[1])["pair"]["same_header_line"] == -2

    def test_shared_furniture_is_not_wording(self):
        rows = assign([page(1, "aaaa bbbb cccc dddd"), page(2, "xxxx yyyy wwww vvvv")])
        assert "wording_char" not in gap(rows[1])["pair"]

    def test_the_next_page_start_is_recorded_not_scored(self):
        rows = assign([page(1), page(2)])
        assert "text_start" in gap(rows[1])["recorded"] and "text_start" not in gap(rows[1])["opening"]


class TestTransparentPages:
    def test_a_blank_page_is_scored_across_with_one_point(self):
        rows = assign([bare(1, printed=(1, 2)), bare(2, kind="blank"), bare(3, printed=(2, 2))])
        assert gap(rows[2])["pair"]["blank_between"] == 1
        assert rows[1]["label"] == "Blank (in Doc 1)" and rows[1]["break_before"] is None

    def test_a_blank_page_between_documents(self):
        rows = assign([bare(1, dos_from="2024-01-01", dos_trust=5), bare(2, kind="blank"),
                       bare(3, dos_from="2024-02-01", dos_trust=5)])
        assert rows[1]["label"] == "Blank" and rows[1]["segment_kind"] is None

    def test_a_single_junk_page_scores_nothing_and_stays_inside(self):
        rows = assign([bare(1, printed=(1, 2)), junk(2), bare(3, printed=(2, 2))])
        assert "junk_run" not in gap(rows[2])["pair"]
        assert rows[1]["segment_kind"] == "main" and rows[1]["document_seq"] == 1
        assert rows[1]["label"] == "Continue (Doc 1)"

    def test_a_run_of_two_junk_pages_is_its_own_segment(self):
        rows = assign([bare(1, signed=True), junk(2), junk(3), bare(4, title_hit=True)])
        # Documents and junk groups share the numbering (document_seq).
        assert [r["label"] for r in rows[1:3]] == ["New (Junk 2)", "Last (Junk 2)"]
        assert [r["document_seq"] for r in rows] == [1, 2, 2, 3]
        assert gap(rows[1])["pair"]["junk_run"] == pytest.approx(3 * 0.96)
        assert rows[3]["segment_kind"] == "main"

    def test_a_run_of_three_junk_pages_is_decisive(self):
        rows = assign([bare(1), junk(2), junk(3), junk(4), bare(5)])
        assert rows[1]["decided_by"] == "decisive" and rows[4]["decided_by"] == "decisive"
        assert {r["segment_kind"] for r in rows[1:4]} == {"junk"}

    def test_a_duplicate_takes_its_originals_document(self):
        rows = assign([bare(1, printed=(1, 2)), bare(2, printed=(2, 2)),
                       bare(3, kind="duplicate", duplicate_of=2)])
        assert rows[2]["segment_seq"] == rows[1]["segment_seq"] == 1


class TestTags:
    def test_new_continue_last_standalone(self):
        rows = assign([bare(1, printed=(1, 3)), bare(2, printed=(2, 3)), bare(3, printed=(3, 3)),
                       bare(4, dos_from="2024-02-01", dos_trust=5, title_hit=True, signed=True, signed_in_tail=True),
                       bare(5, dos_from="2024-03-01", dos_trust=5)])
        assert [r["tag"] for r in rows] == ["new", "continue", "last", "standalone", "standalone"]
        assert [r["label"] for r in rows[:3]] == ["New (Doc 1)", "Continue (Doc 1)", "Last (Doc 1)"]
        assert rows[3]["break_before"] == rows[2]["break_after"]

    def test_a_low_confidence_side_adds_a_question_mark(self):
        rows = assign([bare(1), bare(2)])
        assert rows[0]["label"] == "New? (Doc 1)" and rows[1]["label"] == "Last? (Doc 1)"


class TestFinalPass:
    def test_a_document_with_two_labelled_dates_is_split(self):
        rows = assign([bare(1, dos_from="2024-01-01", dos_trust=3, printed=(1, 3)),
                       bare(2, dos_from="2024-02-01", dos_trust=5, printed=(2, 3)),
                       bare(3, dos_from="2024-03-01", dos_trust=5, printed=(3, 3))])
        assert rows[2]["relation"] == "new_document" and rows[2]["decided_by"] == "final_pass"
        assert "split" in gap(rows[2])["final_pass"]

    def test_a_weak_break_between_same_dated_documents_is_merged(self):
        rows = assign([bare(1, dos_from="2024-01-01", dos_trust=2),
                       bare(2, dos_from="2024-01-01", dos_trust=2, title_hit=True)])
        assert gap(rows[1])["score"] == 1
        assert rows[1]["relation"] == "continue" and gap(rows[1])["final_pass"] == "merged: same_date"

    def test_same_type_untitled_neighbours_are_merged(self):
        rows = assign([bare(1, names=("Lab",), page_type="Lab", ends=0.6),
                       bare(2, names=("Lab",), page_type="Lab")])
        assert gap(rows[1])["score"] == 1
        assert rows[1]["relation"] == "continue" and "same_type_untitled" in gap(rows[1])["final_pass"]

    def test_a_decisive_break_is_never_merged(self):
        rows = assign([bare(1, dos_from="2024-01-01", dos_trust=5), bare(2, dos_from="2024-02-01", dos_trust=5)])
        assert rows[1]["relation"] == "new_document" and not gap(rows[1])["final_pass"]


class TestProgressNoteRunsOn:
    """A document opening on a Progress Note keeps going until a strong date or a signature."""

    def test_it_absorbs_the_pages_after_it(self):
        rows = assign([bare(1, page_type="Progress Note", dos_from="2024-01-01", dos_trust=5, ends=0.6),
                       bare(2, page_type="Lab Report", page_confidence=0.9, title_hit=True),
                       bare(3, page_type="Questionnaire", page_confidence=0.9, title_hit=True)])
        assert [r["document_seq"] for r in rows] == [1, 1, 1]
        assert gap(rows[1])["final_pass"] == "merged: progress_note_runs_on"

    def test_a_signature_ends_it(self):
        rows = assign([bare(1, page_type="Progress Note", signed=True, signed_in_tail=True, ends=0.6),
                       bare(2, page_type="Lab Report", page_confidence=0.9, title_hit=True)])
        assert [r["document_seq"] for r in rows] == [1, 2]

    def test_a_new_labelled_date_ends_it(self):
        rows = assign([bare(1, page_type="Progress Note", dos_from="2024-01-01", dos_trust=5),
                       bare(2, page_type="Lab Report", page_confidence=0.9, title_hit=True, ends=0.6),
                       bare(3, page_type="Progress Note", dos_from="2024-02-01", dos_trust=5)])
        assert [r["document_seq"] for r in rows] == [1, 1, 2]

    def test_a_junk_group_ends_it(self):
        rows = assign([bare(1, page_type="Progress Note"), junk(2), junk(3), junk(4), bare(5)])
        assert rows[4]["document_seq"] != rows[0]["document_seq"]


class TestConflicts:
    def _pages(self):
        visits = frozenset({"fin 100234"})
        return [page(1, signed=True, signed_in_tail=True, ends=0.6, visit_ids=visits),
                page(2, visit_ids=visits)]

    def test_pattern_a_is_recorded_and_flagged_not_applied(self):
        rows = assign(self._pages())
        before = gap(rows[1])
        assert before["recorded"]["conflict"] == "A" and rows[1]["review_required"]
        assert before["decided_by"] == "sum"

    def test_pattern_a_breaks_when_switched_on(self):
        rules = dataclasses.replace(RULES, conflict_apply=True)
        rows = assign(self._pages(), rules)
        assert rows[1]["decided_by"] == "conflict_a" and rows[1]["relation"] == "new_document"


class TestPagination:
    def test_only_the_header_and_footer_are_searched(self):
        body = page(1, "see page 2 of 4 of the attached report", head="x", foot="y")
        assert pagination(body, RULES) is None
        footer = page(1, "body", foot="printed 10/30/2025 pg 8 of 17")
        assert pagination(footer, RULES) == (8, 17)

    def test_page_1_confirms_the_start(self):
        rows = assign([bare(1, printed=(1, 2)), bare(2, printed=(2, 2))])
        assert rows[0]["start_confirmed"] is True and rows[1]["page_no"] == 2
        assert rows[1]["link_strength"] == "strong"


class TestStaging:
    """The stage reads the extraction's staging first (extraction.json)."""

    def _page(self):
        from stages.lib.extraction import staging

        data = {"version": staging.SCHEMA_VERSION, "pages": {"1.jpg": {"page_number": 1, "fields": {
            "dos": [{"accepted": True, "tier": "weak", "dos_from": "03/14/2024", "value": "03/14/2024"},
                    {"accepted": True, "tier": "service", "dos_from": "2024-03-14", "value": "03/14/2024"}],
            "page_no": [{"accepted": True, "selected": True, "page_no": "2", "page_total": "5"}],
            "electronic_signature": [{"accepted": True, "region": "footer"}],
            "member_id": [{"accepted": True, "key": "FIN", "value": "100234"},
                          {"accepted": True, "key": "MRN", "value": "555"}],
            "heading_heron": [{"accepted": True, "kind": "section", "text": "Assessment"},
                              {"accepted": False, "kind": "section", "text": "Plan"}],
        }}}}
        return staging.Staged(data).page("1.jpg")

    def test_every_fact_comes_from_the_staging(self):
        from stages.lib.continuity import stage

        staged = self._page()
        assert stage._staged_trust(staged, "2024-03-14") == 5  # the best tier that read the date
        assert stage._staged_trust(staged, "2024-05-01") is None
        assert stage._staged_printed(staged, 20) == (2, 5)
        assert stage._staged_signature(staged) == (True, True)
        assert stage._staged_visits(staged) == frozenset({"100234"})  # FIN, not the MRN
        assert [h["text"] for h in stage._staged_sections(staged)] == ["Assessment"]


class TestFinalValues:
    def test_continue_pages_take_the_first_pages_dos_and_type(self):
        rows = assign([page(1, dos_from="2024-03-14", dos_trust=5, page_type="Progress Note"),
                       page(2, page_type="Lab Report", page_confidence=0.5), page(3)])
        assert [r["final_dos_from"] for r in rows] == ["2024-03-14"] * 3
        assert [r["final_page_type"] for r in rows] == ["Progress Note"] * 3

    def test_a_stand_alone_non_visit_document_takes_the_default_dos(self):
        rows = assign([page(1, dos_from="2024-03-14", dos_trust=5, page_type="Patient Demographics"),
                       page(2, page_type="Patient Demographics", page_confidence=0.9)])
        assert [r["final_dos_from"] for r in rows] == ["2222-02-02"] * 2
        assert "is not a visit" in rows[0]["evidence"]

    def test_a_non_visit_page_inside_a_visit_takes_the_visit_dos(self):
        rows = assign([page(1, dos_from="2024-03-14", dos_trust=5, page_type="Progress Note"),
                       page(2, page_type="Problem List", page_confidence=0.6)])
        assert [r["final_dos_from"] for r in rows] == ["2024-03-14"] * 2

    def test_the_sub_type_decides_too(self):
        from stages.lib.continuity.engine import document_finals

        finals = document_finals([{"page_type": "Clinical Summary", "page_subtype": "Problem List",
                                   "dos_from": "2024-01-01"}])
        assert finals["dos_from"] == "2222-02-02" and finals["dos_default"]

    def test_dos_is_the_first_dated_pages(self):
        from stages.lib.continuity.engine import document_finals

        finals = document_finals([
            {"page_type": "Progress Note", "dos_from": ""},
            {"page_type": "Progress Note", "dos_from": "2024-03-05", "dos_to": "2024-03-06"},
            {"page_type": "Progress Note", "dos_from": "2024-04-01"},
        ])
        assert (finals["dos_from"], finals["dos_to"]) == ("2024-03-05", "2024-03-06")

    def test_dos_is_the_first_pages_when_it_is_dated(self):
        from stages.lib.continuity.engine import document_finals

        finals = document_finals([
            {"page_type": "Progress Note", "dos_from": "2024-01-01"},
            {"page_type": "Progress Note", "dos_from": "2024-03-05"},
        ])
        assert (finals["dos_from"], finals["dos_to"]) == ("2024-01-01", "2024-01-01")

    def test_encounter_type_is_the_first_pages(self):
        from stages.lib.continuity.engine import document_finals

        finals = document_finals([
            {"page_type": "Progress Note", "dos_from": "2024-01-01", "encounter_type": "Inpatient"},
            {"page_type": "Progress Note", "dos_from": "", "encounter_type": "Outpatient (F2F)"},
        ])
        assert finals["encounter_type"] == "Inpatient"


class TestLayout:
    def test_final2_lines_are_placed_by_their_polygons(self):
        lines = lines_from_final2({"pagesMeta": [{"height": 1000.0, "lines": [
            {"content": "Footer  Line", "polygon": [0, 950, 10, 950, 10, 970, 0, 970]},
            {"content": "Banner", "polygon": [0, 20, 10, 20, 10, 40, 0, 40]},
        ]}]})
        assert [l.text for l in lines] == ["banner", "footer line"]
        assert lines[0].top == 0.02 and lines[1].bottom == 0.97

    def test_final1_words_are_grouped_into_lines_left_to_right(self):
        word = lambda text, x, y: {"content": text, "polygon": [x, y, x + 5, y, x + 5, y + 10, x, y + 10]}
        lines = lines_from_final1({"height": 1000.0, "words": [
            word("world", 50, 101), word("hello", 0, 100), word("next", 0, 300),
        ]})
        assert [l.text for l in lines] == ["hello world", "next"]

    def test_text_alone_falls_back_to_first_and_last_lines(self):
        p = PageInput(page_id=1, lines=text_lines("Page 3 of 9\nbody\nbody\nbody"))
        assert pagination(p, RULES) == (3, 9)


class TestStage:
    """Continuity then imaging_final, end to end on the in-memory store:
    grouping from the Final2 JSON, the first page's values on every page."""

    def test_run_writes_rows_and_csv(self, tmp_path, monkeypatch):
        import csv
        import json

        import config
        from db import (
            connect,
            disable_skip_db_write,
            enable_skip_db_write,
            init_page_stages,
            list_pages,
            upsert_chart,
            upsert_dos,
            upsert_ocr_result,
            upsert_page_classification,
            upsert_pages,
        )
        from stages.lib.continuity import stage as continuity_stage
        from stages.lib.encounter import stage as encounter_stage
        from stages.lib.extraction import stage as extraction_stage
        from stages.lib.extraction import staging
        from stages.lib.imaging_final import stage as final_stage

        import db

        monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
        # Another test may have imported the stage while db.connect was faked.
        monkeypatch.setattr(continuity_stage, "connect", db.connect)
        monkeypatch.setattr(final_stage, "connect", db.connect)
        monkeypatch.setattr(encounter_stage, "connect", db.connect)
        monkeypatch.setattr(
            extraction_stage, "ensure_staging",
            lambda *_: staging.Staged({"version": staging.SCHEMA_VERSION, "pages": {}}),
        )
        store = enable_skip_db_write(reset=True)
        try:
            def line(text, top):
                return {"content": text, "polygon": [0, top, 9, top, 9, top + 20, 0, top + 20]}

            pages_json = [
                {"pageNumber": n, "fileName": f"{n}.jpg", "content": f"note {n}",
                 "pagesMeta": [{"height": 1000.0, "lines": [
                     line("Progress Note", 30), line(f"body of page {n}", 800),
                     line(f"Page {n} of 2", 960)]}],
                 "section_headers": []}
                for n in (1, 2)
            ]
            ocr = tmp_path / "chart_a" / "ocr"
            ocr.mkdir(parents=True)
            (ocr / "chart_a_final2.json").write_text(
                json.dumps({"pages": pages_json}), encoding="utf-8"
            )

            with connect() as conn:
                chart = upsert_chart(conn, chart_name="chart_a", source="local")
                cid = int(chart["id"])
                upsert_pages(conn, cid, [{"page_name": "1.jpg", "page_number": 1},
                                         {"page_name": "2.jpg", "page_number": 2}])
                pages = list_pages(conn, cid)
                init_page_stages(conn, cid)
                for p in pages:
                    upsert_ocr_result(conn, chart_id=cid, page_id=p["id"],
                                      ocr_type="azuredocintel", raw_text=f"note {p['page_number']}")
                first, second = pages[0]["id"], pages[1]["id"]
                # Page 1: a Discharge Summary title. Page 2: untitled, reads
                # like a Progress Note, printed "Page 2 of 2".
                upsert_page_classification(conn, chart_id=cid, page_id=first,
                                           page_type="Discharge Summary",
                                           page_subtype="Discharge Summary",
                                           classification_category="discharge_summary",
                                           keyword_title_hit=True,
                                           axes={"l1": "Discharge Summary", "title_l1": "Discharge Summary",
                                                 "specialty": "Cardiology"})
                upsert_page_classification(conn, chart_id=cid, page_id=second,
                                           page_type="Progress Note",
                                           page_subtype="Progress Note",
                                           classification_category="codeable",
                                           keyword_title_hit=False,
                                           axes={"l1": "Progress Note", "specialty": "Neurology"})
                upsert_dos(conn, chart_id=cid, page_id=first,
                           date_of_service_from="2024-03-14", date_of_service_to="2024-03-14",
                           date_of_service_from_doclevel=None, date_of_service_to_doclevel=None,
                           confidence=0.9)

            result = continuity_stage.run(cid, force=True)
            assert result["documents"] == 1
            # Every score is staged beside extraction.json.
            scores = json.loads(Path(result["continuity_json"]).read_text(encoding="utf-8"))
            # continuity.json: per page the gap before / after in words, and the result.
            second_page = scores["pages"]["2.jpg"]
            assert second_page["prev_to_this"]["decision"] == "Same document"
            assert second_page["prev_to_this"]["score"] <= -5
            assert any("page numbers run on" in why for why in second_page["prev_to_this"]["because"])
            assert scores["pages"]["1.jpg"]["prev_to_this"] == "first page"
            assert second_page["final"]["document"] == 1
            assert scores["documents"][0]["pages"] == ["1.jpg", "2.jpg"]
            # Every signal of every gap is in continuity_detail.json.
            detail = json.loads(Path(result["continuity_json"]).with_name("continuity_detail.json").read_text(encoding="utf-8"))
            assert detail["pages"][1]["break_before"] <= -5 and detail["pages"][1]["gap_before"]["pair"]
            grouping = {r["page_id"]: r for r in store.continuity.values()}
            # Undated, printed "Page 2 of 2" after "Page 1 of 2": page numbers run on (-5,
            # decisive), a joined gap.
            assert grouping[second]["decided_by"] == "decisive"
            assert grouping[second]["tag"] == "last" and grouping[first]["tag"] == "new"
            assert grouping[second]["break_before"] <= -5
            # The 0-1 confidence goes to page_continuity_results beside score.
            assert grouping[first]["confidence"] == 1.0
            assert 0.9 <= grouping[second]["confidence"] <= 1.0
            with open(result["continuity_csv"], newline="", encoding="utf-8") as handle:
                written = list(csv.DictReader(handle))
            assert written[1]["dos_from"] == ""
            assert written[1]["layout_source"] == "final2"
            assert "final_dos_from" not in written[1]

            # The encounter grouping is staged at the end of continuity.json: one
            # encounter (the document), one decision for both pages.
            encounter = encounter_stage.run(cid, force=True)
            assert encounter["encounters_json"] == result["continuity_json"]
            staged = json.loads(Path(result["continuity_json"]).read_text(encoding="utf-8"))
            assert staged["pages"] and staged["documents"]
            (only,) = staged["encounters"]
            assert only["pages"] == ["1.jpg", "2.jpg"] and only["document"] == 1
            assert only["page_family"] == "Discharge Summary"
            # The first page (a Discharge Summary) has evidence of its own: it decides.
            assert only["encounter_type"] == "inpatient" and only["decided_by"] == "first_page"

            final = final_stage.run(cid, force=True)
            rows = {r["page_id"]: r for r in store.imaging_final.values()}
            assert grouping[second]["link_strength"] == "strong"
            assert grouping[second]["start_confirmed"] is True
            # Continue decided -> the first page's page type and DOS.
            assert rows[second]["page_type"] == "Discharge Summary"
            # A Progress Note has no place under Discharge Summary: Others, never L1 / L1.
            assert rows[second]["page_subtype"] == "Others"
            # The encounter's first specialty, carried to every page.
            assert rows[first]["specialty"] == rows[second]["specialty"] == "Cardiology"
            assert rows[second]["page_type_source"] == "continuation"
            assert rows[second]["continuation_rule"] == "agreed_first_page"
            assert rows[second]["codability"] == "Discharge"
            assert rows[second]["dos_from"] == "2024-03-14"
            assert rows[second]["dos_source"] == "document"
            assert rows[first]["page_type_source"] == "page"
            assert rows[second]["document_seq"] == 1
            with open(final["final_csv"], newline="", encoding="utf-8") as handle:
                final_rows = list(csv.DictReader(handle))
            assert final_rows[1]["dos_from"] == "2024-03-14"
            assert final_rows[1]["classification_category"] == "discharge_summary"

            # A duplicate of page 2 (skipped after blank/junk) takes page 2's
            # Finals and keeps its own blank/junk flag.
            from db import upsert_blank_junk

            with connect() as conn:
                upsert_pages(conn, cid, [{"page_name": "3.jpg", "page_number": 3}])
                third = next(p["id"] for p in list_pages(conn, cid) if p["page_number"] == 3)
                init_page_stages(conn, cid)
                upsert_blank_junk(conn, chart_id=cid, page_id=third, blank_junk_flag="duplicate",
                                  pass_no=2, ocr_source="final2", duplicate_of_page_id=second,
                                  confidence=1.0, is_final=True)
            final_stage.run(cid, force=True)
            rows = {r["page_id"]: r for r in store.imaging_final.values()}
            assert rows[third]["blank_junk_flag"] == "duplicate"
            assert rows[third]["duplicate_of_page_id"] == second
            assert rows[third]["page_type"] == "Discharge Summary"
            assert rows[third]["page_type_source"] == "duplicate"
            assert rows[third]["dos_from"] == "2024-03-14"
            assert rows[third]["dos_source"] == "duplicate"
            assert rows[third]["document_seq"] == 1
            assert rows[second]["duplicate_of_page_id"] is None
        finally:
            disable_skip_db_write()





class TestSchema:
    def test_every_decided_by_the_engine_writes_is_allowed_by_the_schema(self):
        """The engine's reasons must pass page_continuity_results' CHECK, or the stage fails
        the whole chart on insert."""
        import re

        schema = (REPO / "schema/v1.sql").read_text(encoding="utf-8")
        check = re.search(r"decided_by\s+VARCHAR\(20\)[^;]*?CHECK \(decided_by IN \((.*?)\)\)", schema, re.S)
        allowed = set(re.findall(r"'([a-z_]+)'", check.group(1)))
        assert set(DECIDED_BY) <= allowed, sorted(set(DECIDED_BY) - allowed)
        assert all(len(value) <= 20 for value in DECIDED_BY)


def test_imaging_final_gives_junk_pages_no_document_values():
    """Junk pages share the document numbering but keep their own Final values."""
    from stages.lib.imaging_final.stage import _documents

    pages = [{"id": 1}, {"id": 2}, {"id": 3}]
    continuity = {1: {"document_seq": 1, "seq": 1}, 2: {"document_seq": 1, "seq": 2}, 3: {"document_seq": 2, "seq": 1}}
    assert _documents(pages, continuity, frozenset({2, 3})) == {1: [1]}


class TestEncounterSignals:
    """2026-10-23 (user): visit headings open an encounter; Demographics stands alone."""

    def test_a_visit_heading_at_the_top_pushes_toward_a_break(self):
        rows = assign([bare(1), bare(2, "Chief Complaint: cough for two weeks")])
        before = gap(rows[1])
        assert before["opening"]["visit_heading"] == 3
        assert before["evidence"]["visit_heading"].lower().startswith("chief complaint")

    def test_a_visit_heading_at_the_top_always_breaks(self):
        # Same labelled DOS (-5, decisive join) would otherwise keep them together.
        note = dict(dos_from="2024-03-14", dos_trust=5)
        rows = assign([bare(1, **note), bare(2, "HPI: cough for two weeks", **note)])
        assert gap(rows[1])["decision"] == "break"
        assert rows[0]["document_seq"] != rows[1]["document_seq"]

    def test_a_visit_heading_on_a_page_that_runs_on_by_number_still_joins(self):
        rows = assign([bare(1, printed=(1, 2)), bare(2, "Reason for Appointment: follow up", printed=(2, 2))])
        assert rows[0]["document_seq"] == rows[1]["document_seq"]

    def test_a_visual_page_joins_the_page_before_it(self):
        demo = dict(page_type="Demographics", names=("Patient Demographics", "Demographics"))
        rows = assign([bare(1, "Chief Complaint: cough"), bare(2, "", visual=True, **demo)])
        assert gap(rows[1])["evidence"]["visual_join"]
        assert rows[0]["document_seq"] == rows[1]["document_seq"]

    def test_a_visit_heading_low_on_the_page_does_not_count(self):
        body = "\n".join(["assessment stable"] * 6 + ["Reason for Visit: follow up"])
        rows = assign([bare(1), bare(2, body)])
        assert "visit_heading" not in gap(rows[1])["opening"]

    def test_demographics_never_joins_a_progress_note(self):
        # Page numbers running on (-5, decisive join) would join these two.
        demo = bare(1, page_type="Demographics", names=("Patient Demographics", "Demographics"),
                    printed=(1, 2), dos_from="2024-03-14", dos_trust=5)
        note = bare(2, page_type="Progress Note", names=("Progress Note",),
                    printed=(2, 2), dos_from="2024-03-14", dos_trust=5)
        rows = assign([demo, note])
        assert gap(rows[1])["decision"] == "break"
        assert gap(rows[1])["evidence"]["demographics_boundary"]
        assert rows[0]["document_seq"] != rows[1]["document_seq"]
        # Standing alone, the Demographics document takes the default DOS.
        assert rows[0]["final_dos_from"] == "2222-02-02"
        assert rows[1]["final_dos_from"] == "2024-03-14"

    def test_consecutive_demographics_pages_stay_together(self):
        demo = dict(page_type="Demographics", names=("Patient Demographics", "Demographics"))
        rows = assign([bare(1, printed=(1, 2), **demo), bare(2, printed=(2, 2), **demo)])
        assert rows[0]["document_seq"] == rows[1]["document_seq"]
