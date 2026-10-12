"""Document continuity: grouping pages into documents."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from stages.lib.continuity.engine import (  # noqa: E402
    Line,
    PageInput,
    assign,
    body_end,
    load_rules,
    pagination,
    same_type_probability,
    text_lines,
)
from stages.lib.continuity.layout import lines_from_final1, lines_from_final2  # noqa: E402

BANNER = "patient: jane doe dob 01/02/1950 mrn 123456"
FOOTER = "community clinic 100 main street springfield"


def page(n, body="body", *, head=BANNER, foot=FOOTER, page_type="", confidence=None, dos="",
         dos_to="", signed=False, skipped=False, printed=None, title_hit=False, breaks=False,
         ends=0.85):
    """A located page: head line at the top, body ending ``ends`` down the page, foot at the bottom."""
    lines = []
    if head:
        lines.append(Line(head, 0.03, 0.05))
    for i, text in enumerate(body.splitlines()):
        lines.append(Line(text.lower(), 0.30 + i * 0.02, 0.31 + i * 0.02))
    lines.append(Line("last line of the body", ends - 0.01, ends))
    if foot:
        lines.append(Line(foot, 0.95, 0.97))
    return PageInput(
        page_id=n, page_name=f"{n}.jpg", page_number=n,
        text="\n".join(l.text for l in lines), lines=lines,
        page_type=page_type, page_confidence=confidence, title_hit=title_hit,
        dos_from=dos, dos_to=dos_to or dos,
        signed=signed, skipped=skipped, printed=printed, breaks=breaks,
    )


def bare(n, body="body", **kw):
    return page(n, body, head="", foot="", **kw)


RULES = load_rules()


def docs(rows):
    return [r["document_seq"] for r in rows]


class TestSameDos:
    def test_pages_with_the_same_dos_are_one_document(self):
        rows = assign([page(1, dos="2024-03-14"), page(2, dos="2024-03-14"), page(3, dos="2024-03-14")])
        assert docs(rows) == [1, 1, 1]
        assert rows[1]["decided_by"] == "same_dos"
        assert rows[2]["link_strength"] == "strong"

    def test_a_different_dos_starts_a_new_document(self):
        rows = assign([page(1, dos="2024-03-14"), page(2, dos="2024-05-01")])
        assert docs(rows) == [1, 2]
        assert rows[1]["decided_by"] == "new_dos"

    def test_same_dos_clubs_even_after_a_signature(self):
        rows = assign([page(1, dos="2024-03-14", signed=True), page(2, dos="2024-03-14")])
        assert docs(rows) == [1, 1]

    def test_from_and_to_must_both_match(self):
        rows = assign([page(1, dos="2024-03-01", dos_to="2024-03-05"), page(2, dos="2024-03-01")])
        assert docs(rows) == [1, 2]

    def test_a_dated_page_after_an_undated_document_starts_a_new_one(self):
        rows = assign([page(1), page(2, dos="2024-03-14")])
        assert docs(rows) == [1, 2]


class TestContinuePages:
    def test_an_undated_page_continues_the_open_document(self):
        rows = assign([page(1, dos="2024-03-14"), page(2), page(3)])
        assert docs(rows) == [1, 1, 1]
        assert rows[1]["decided_by"] == "continue_page"
        assert rows[2]["link_strength"] == "weak"

    def test_a_signature_on_the_previous_page_ends_the_document(self):
        rows = assign([page(1, dos="2024-03-14", signed=True), page(2)])
        assert docs(rows) == [1, 2]
        assert rows[1]["decided_by"] == "signature"

    def test_a_sure_page_type_change_ends_the_document(self):
        rows = assign([page(1, dos="2024-03-14", page_type="Progress Note"),
                       page(2, page_type="Laboratory Data", confidence=0.9)])
        assert docs(rows) == [1, 2]
        assert rows[1]["decided_by"] == "page_type"

    def test_an_unsure_page_type_change_continues(self):
        rows = assign([page(1, dos="2024-03-14", page_type="Progress Note"),
                       page(2, page_type="Laboratory Data", confidence=0.6)])
        assert docs(rows) == [1, 1]

    def test_exactly_10_percent_is_sure_enough(self):
        rows = assign([page(1, dos="2024-03-14", page_type="Progress Note"),
                       page(2, page_type="Laboratory Data", confidence=0.90)])
        assert rows[1]["decided_by"] == "page_type"

    def test_a_type_change_also_needs_70_percent_on_the_new_type(self):
        """Same type at 5% confidence: 5% it is the same, but the classifier is not sure."""
        from dataclasses import replace

        rules = replace(RULES, max_same_type_probability=0.5)
        rows = assign([page(1, dos="2024-03-14", page_type="Lab"),
                       page(2, page_type="Lab", confidence=0.4)], rules)
        assert rows[1]["decided_by"] == "continue_page"

    def test_printed_page_numbers_never_split(self):
        rows = assign([page(1, dos="2024-03-14", printed=(2, 3)), page(2, printed=(1, 2))])
        assert docs(rows) == [1, 1]

    def test_the_last_printed_page_ends_the_document(self):
        rows = assign([page(1, dos="2024-03-14", printed=(2, 2)), page(2)])
        assert docs(rows) == [1, 2]
        assert rows[1]["decided_by"] == "page_number_end"

    def test_the_last_printed_page_does_not_split_the_same_dos(self):
        rows = assign([page(1, dos="2024-03-14", printed=(1, 1)), page(2, dos="2024-03-14")])
        assert docs(rows) == [1, 1]

    def test_consecutive_page_numbers_confirm_a_continue_page(self):
        """N -> N+1 of M continues even past a signature or a sure type change."""
        rows = assign([page(1, dos="2024-03-14", printed=(1, 2), signed=True, page_type="Progress Note"),
                       page(2, printed=(2, 2), page_type="Laboratory Data", confidence=0.95)])
        assert docs(rows) == [1, 1]
        assert (rows[1]["decided_by"], rows[1]["link_strength"]) == ("pagination", "strong")

    def test_a_different_dos_still_splits_a_paginated_printout(self):
        rows = assign([page(1, dos="2024-03-14", printed=(1, 2)),
                       page(2, dos="2024-06-03", printed=(2, 2))])
        assert docs(rows) == [1, 2]

    def test_a_title_for_another_page_type_starts_a_new_document(self):
        rows = assign([page(1, dos="2024-03-14", page_type="Progress Note"),
                       page(2, page_type="Operative Report", title_hit=True)])
        assert rows[1]["decided_by"] == "first_page_marker"

    def test_a_title_for_the_same_page_type_continues(self):
        rows = assign([page(1, dos="2024-03-14", page_type="Progress Note"),
                       page(2, page_type="Progress Note", title_hit=True)])
        assert docs(rows) == [1, 1]

    def test_same_type_probability(self):
        a = page(1, page_type="Progress Note")
        assert same_type_probability(a, page(2, page_type="Progress Note", confidence=0.7)) == 0.7
        assert round(same_type_probability(a, page(2, page_type="Lab", confidence=0.7)), 2) == 0.3
        assert same_type_probability(a, page(2, page_type="Lab")) is None

    def test_text_ending_mid_page_ends_the_document(self):
        rows = assign([page(1, dos="2024-03-14", ends=0.45), page(2)])
        assert docs(rows) == [1, 2]
        assert rows[1]["decided_by"] == "page_end"

    def test_the_footer_does_not_count_as_text(self):
        p = page(1, ends=0.45)
        assert body_end(p, RULES) == 0.45

    def test_without_line_positions_there_is_no_page_end_signal(self):
        p = PageInput(page_id=1, lines=text_lines("short\ntext"))
        assert body_end(p, RULES) is None


class TestFinalValues:
    def test_continue_pages_take_the_first_pages_dos_and_type(self):
        rows = assign([page(1, dos="2024-03-14", page_type="Progress Note"),
                       page(2, page_type="Laboratory Data", confidence=0.5), page(3)])
        assert [r["final_dos_from"] for r in rows] == ["2024-03-14"] * 3
        assert [r["final_page_type"] for r in rows] == ["Progress Note"] * 3

    def test_a_stand_alone_non_visit_document_takes_the_default_dos(self):
        rows = assign([page(1, dos="2024-03-14", page_type="Patient Demographics"),
                       page(2, page_type="Patient Demographics", confidence=0.9)])
        assert [r["final_dos_from"] for r in rows] == ["2222-02-02"] * 2
        assert "is not a visit" in rows[0]["evidence"]

    def test_a_non_visit_page_inside_a_visit_takes_the_visit_dos(self):
        rows = assign([page(1, dos="2024-03-14", page_type="Progress Note"),
                       page(2, page_type="Problem List", confidence=0.6)])
        assert [r["final_dos_from"] for r in rows] == ["2024-03-14"] * 2

    def test_the_sub_type_decides_too(self):
        from stages.lib.continuity.engine import document_finals

        finals = document_finals([{"page_type": "Clinical Summary", "page_subtype": "Problem List",
                                   "dos_from": "2024-01-01"}])
        assert finals["dos_from"] == "2222-02-02" and finals["dos_default"]

    def test_encounter_type_is_the_first_pages(self):
        from stages.lib.continuity.engine import document_finals

        finals = document_finals([
            {"page_type": "Progress Note", "dos_from": "2024-01-01", "encounter_type": "Inpatient"},
            {"page_type": "Progress Note", "dos_from": "", "encounter_type": "Outpatient (F2F)"},
        ])
        assert finals["encounter_type"] == "Inpatient"


class TestBreakers:
    def test_a_blank_or_junk_page_in_between_breaks_the_document(self):
        rows = assign([page(1, dos="2024-01-01"), bare(2, skipped=True, breaks=True), page(3)])
        assert rows[1]["document_seq"] is None
        assert (rows[2]["document_seq"], rows[2]["decided_by"]) == (2, "blank_junk")

    def test_a_duplicate_in_between_does_not_break(self):
        rows = assign([page(1, dos="2024-01-01"), bare(2, skipped=True), page(3)])
        assert rows[1]["decided_by"] == "blank_junk"
        assert rows[2]["relation"] == "continue"
        assert [rows[0]["position"], rows[2]["position"]] == ["first", "last"]

    def test_labels_name_the_position_and_the_document(self):
        rows = assign([page(1, dos="2024-01-01"), page(2), page(3),
                       bare(4, skipped=True, breaks=True), page(5)])
        assert [r["label"] for r in rows] == [
            "First (Doc 1)", "Continue (Doc 1)", "Last (Doc 1)", "", "Single (Doc 2)"]


class TestPagination:
    """Printed page numbers are recorded and confirm a start; they do not group."""

    def test_only_the_header_and_footer_are_searched(self):
        body = page(1, "see page 2 of 4 of the attached report", head="x", foot="y")
        assert pagination(body, RULES) is None
        footer = page(1, "body", foot="printed 10/30/2025 pg 8 of 17")
        assert pagination(footer, RULES) == (8, 17)

    def test_a_date_is_not_a_page_number(self):
        assert pagination(page(1, foot="seen 26/24"), RULES) is None

    def test_page_1_confirms_the_start(self):
        rows = assign([page(1, printed=(1, 2)), page(2, printed=(2, 2))])
        assert rows[0]["start_confirmed"] is True
        assert rows[1]["page_no"] == 2


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
        from stages.lib.extraction import stage as extraction_stage
        from stages.lib.extraction import staging
        from stages.lib.imaging_final import stage as final_stage

        import db

        monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
        # Another test may have imported the stage while db.connect was faked.
        monkeypatch.setattr(continuity_stage, "connect", db.connect)
        monkeypatch.setattr(final_stage, "connect", db.connect)
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
                                           keyword_title_hit=True)
                upsert_page_classification(conn, chart_id=cid, page_id=second,
                                           page_type="Progress Note",
                                           page_subtype="Progress Note",
                                           classification_category="codeable",
                                           keyword_title_hit=False)
                upsert_dos(conn, chart_id=cid, page_id=first,
                           date_of_service_from="2024-03-14", date_of_service_to="2024-03-14",
                           date_of_service_from_doclevel=None, date_of_service_to_doclevel=None,
                           confidence=0.9)

            result = continuity_stage.run(cid, force=True)
            assert result["documents"] == 1
            grouping = {r["page_id"]: r for r in store.continuity.values()}
            # Undated, printed "Page 2 of 2" after "Page 1 of 2": a confirmed continue page.
            assert grouping[second]["decided_by"] == "pagination"
            with open(result["continuity_csv"], newline="", encoding="utf-8") as handle:
                written = list(csv.DictReader(handle))
            assert written[1]["dos_from"] == ""
            assert written[1]["layout_source"] == "final2"
            assert "final_dos_from" not in written[1]

            final = final_stage.run(cid, force=True)
            rows = {r["page_id"]: r for r in store.imaging_final.values()}
            assert grouping[second]["link_strength"] == "strong"
            assert grouping[second]["start_confirmed"] is True
            # Continue decided -> the first page's page type and DOS.
            assert rows[second]["page_type"] == "Discharge Summary"
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



