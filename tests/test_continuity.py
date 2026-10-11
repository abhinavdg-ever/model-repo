"""Document continuity: grouping pages into documents and carrying Final values."""
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
    judge,
    load_rules,
    pagination,
    text_lines,
)
from stages.lib.continuity.layout import lines_from_final1, lines_from_final2  # noqa: E402

BANNER = "patient: jane doe dob 01/02/1950 mrn 123456"
FOOTER = "community clinic 100 main street springfield"


def page(n, body="", *, head=BANNER, foot=FOOTER, page_type="", dos="",
         signed=False, headers=(), skipped=False, printed=None, title_hit=False):
    """A located page: head line at the top, body in the middle, foot line at the bottom."""
    lines = []
    if head:
        lines.append(Line(head, 0.03, 0.05))
    for i, text in enumerate(body.splitlines()):
        lines.append(Line(text.lower(), 0.30 + i * 0.02, 0.31 + i * 0.02))
    if foot:
        lines.append(Line(foot, 0.95, 0.97))
    return PageInput(
        page_id=n, page_name=f"{n}.jpg", page_number=n,
        text="\n".join(l.text for l in lines), lines=lines,
        section_headers=[{"text": h, "matched_canonical": h, "top": top} for h, top in headers],
        page_type=page_type, title_hit=title_hit, dos_from=dos, dos_to=dos,
        signed=signed, skipped=skipped, printed=printed,
    )


def bare(n, body="", **kw):
    """A page sharing nothing with its neighbours: no banner, no footer."""
    return page(n, body, head="", foot="", **kw)


RULES = load_rules()


class TestPagination:
    def test_page_numbers_decide_on_their_own(self):
        a, b = page(1, printed=(1, 3)), page(2, printed=(2, 3))
        assert judge(a, b, RULES).relation == "continue"
        assert judge(a, b, RULES).decided_by == "pagination"
        assert judge(b, page(3, printed=(1, 2)), RULES).relation == "new_document"
        assert "resets" in judge(b, page(3, printed=(3, 5)), RULES).evidence

    def test_only_the_header_and_footer_are_searched(self):
        body = page(1, "see page 2 of 4 of the attached report", head="x", foot="y")
        assert pagination(body, RULES) is None
        footer = page(1, "body", foot="printed 10/30/2025 pg 8 of 17")
        assert pagination(footer, RULES) == (8, 17)

    def test_a_date_is_not_a_page_number(self):
        assert pagination(page(1, foot="seen 26/24"), RULES) is None


class TestSignals:
    def test_a_repeated_banner_and_footer_continue(self):
        verdict = judge(page(1, "assessment"), page(2, "plan"), RULES)
        assert verdict.relation == "continue"
        assert "repeated header line" in verdict.evidence

    def test_a_title_and_a_signature_start_a_new_document(self):
        previous = bare(1, "plan", signed=True)
        current = bare(2, "x", headers=[("Office Visit", 0.08)])
        assert judge(previous, current, RULES).relation == "new_document"

    def test_a_shared_emr_banner_outweighs_them_and_is_left_for_review(self):
        """Separate documents from one EMR reprint the same banner (28% of new
        documents in the tuning set), so the banner and a title cancel out."""
        previous = page(1, "plan", signed=True)
        current = page(2, "x", headers=[("Office Visit", 0.08)])
        assert judge(previous, current, RULES).relation == "unknown"

    def test_an_unmatched_header_candidate_is_not_a_title(self):
        current = page(2, "x")
        current.section_headers = [{"text": "Respiratory:", "matched_canonical": "", "top": 0.05}]
        assert "own title" not in judge(page(1), current, RULES).evidence

    def test_no_signal_is_unknown_and_starts_its_own_document_for_review(self):
        rows = assign([bare(1, "alpha"), bare(2, "beta")])
        assert rows[1]["relation"] == "unknown"
        assert rows[1]["review_required"] is True
        assert rows[1]["document_seq"] == 2
        assert rows[1]["final_page_type"] is None or rows[1]["final_page_type"] == ""


class TestProgressNote:
    NOTE = dict(page_type="Progress Note")

    def test_an_open_note_keeps_pages_the_signals_cannot_decide(self):
        rows = assign([bare(1, dos="2024-03-14", **self.NOTE), bare(2), bare(3)])
        assert [r["document_seq"] for r in rows] == [1, 1, 1]
        assert rows[2]["decided_by"] == "progress_note"
        assert rows[2]["final_page_type"] == "Progress Note"
        assert rows[2]["final_dos_from"] == "2024-03-14"
        assert rows[2]["dos_from"] == ""  # Extracted stays the page's own

    def test_the_signature_page_is_the_last_page(self):
        rows = assign([bare(1, dos="2024-03-14", **self.NOTE), bare(2, signed=True), bare(3)])
        assert [r["document_seq"] for r in rows] == [1, 1, 2]
        assert rows[1]["position"] == "last"
        assert rows[2]["relation"] == "new_document"
        assert rows[2]["final_dos_from"] is None

    def test_the_next_encounter_ends_the_note_even_inside_one_printout(self):
        rows = assign([
            page(1, dos="2024-03-28", printed=(1, 4), **self.NOTE),
            page(2, printed=(2, 4)),
            page(3, dos="2024-06-03", printed=(3, 4), headers=[("Office Visit", 0.32)]),
            page(4, printed=(4, 4)),
        ])
        assert [r["document_seq"] for r in rows] == [1, 1, 2, 2]
        assert rows[2]["evidence"].startswith("next encounter: Office Visit on 2024-06-03")
        assert rows[3]["final_dos_from"] == "2024-06-03"

    def test_a_stray_date_without_a_visit_header_does_not_end_the_note(self):
        rows = assign([
            bare(1, dos="2025-01-23", **self.NOTE),
            bare(2, dos="2017-11-14", headers=[("Problem List", 0.7)]),
        ])
        assert [r["document_seq"] for r in rows] == [1, 1]
        assert rows[1]["final_dos_from"] == "2025-01-23"


class TestFinalValues:
    def test_the_first_page_sets_the_final_page_type_and_dos(self):
        rows = assign([
            page(1, page_type="Laboratory Report", dos="2024-10-01"),
            page(2, page_type="Patient Demographics", dos=""),
        ])
        assert rows[1]["final_page_type"] == "Laboratory Report"
        assert rows[1]["final_dos_from"] == "2024-10-01"
        assert rows[1]["page_type"] == "Patient Demographics"

    def test_an_undated_first_page_takes_the_first_dated_page(self):
        rows = assign([page(1, page_type="Lab"), page(2, dos="2024-10-01")])
        assert rows[0]["final_dos_from"] == "2024-10-01"
        assert "Final DOS from page 2" in rows[0]["evidence"]

    def test_blank_and_junk_pages_belong_to_no_document_and_are_passed_over(self):
        rows = assign([
            page(1, dos="2024-01-01", printed=(1, 2)),
            bare(2, skipped=True),
            page(3, printed=(2, 2)),
        ])
        assert rows[1]["document_seq"] is None
        assert rows[1]["decided_by"] == "blank_junk"
        assert rows[2]["relation"] == "continue"
        assert [rows[0]["position"], rows[2]["position"]] == ["first", "last"]


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
    grouping from the Final2 JSON, Final values carried from the first page."""

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
                     line("Progress Note", 30), line(f"body of page {n}", 400),
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
            # Continue decided -> page type replicated -> DOS carried.
            assert rows[second]["page_type"] == "Discharge Summary"
            assert rows[second]["page_type_source"] == "continuation"
            assert rows[second]["continuation_rule"] == "continuation"
            assert rows[second]["codability"] == "Discharge"
            assert rows[second]["dos_from"] == "2024-03-14"
            assert rows[second]["dos_source"] == "document"
            assert rows[first]["page_type_source"] == "page"
            assert rows[second]["document_seq"] == 1
            with open(final["final_csv"], newline="", encoding="utf-8") as handle:
                final_rows = list(csv.DictReader(handle))
            assert final_rows[1]["dos_from"] == "2024-03-14"
            assert final_rows[1]["classification_category"] == "discharge_summary"
        finally:
            disable_skip_db_write()
