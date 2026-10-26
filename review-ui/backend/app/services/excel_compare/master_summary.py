from collections import defaultdict
from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Rect
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    KeepTogether,
    CondPageBreak,
)


# =============================================================
# MASTER SUMMARY
#
# A print-ready QA overview of one comparison run, read entirely
# from the dict compare.compare_files() returns -- nothing is
# recomputed with a different formula, so it always reconciles
# with the Excel report built from the same result.
#
# Ordered by what a reviewer acts on: headline numbers, which
# charts and fields are worst, the recurring disagreements behind
# them. The per-field evidence is in the Excel report, not here.
# =============================================================

REPORT_TITLE = "Manual Review — Master Summary"

ACCENT_COLOR = colors.HexColor("#1f2937")
MUTED_COLOR = colors.HexColor("#6b7280")
HEADER_BG = colors.HexColor("#1f2937")
ROW_ALT_BG = colors.HexColor("#f3f4f6")
BORDER_COLOR = colors.HexColor("#d1d5db")
TRACK_COLOR = colors.HexColor("#e5e7eb")
GOOD_COLOR = colors.HexColor("#047857")
FAIR_COLOR = colors.HexColor("#b45309")
POOR_COLOR = colors.HexColor("#b91c1c")
NEUTRAL_COLOR = colors.HexColor("#374151")

# Accuracy at or above GOOD reads green, at or above FAIR amber,
# anything lower red.
GOOD_AT = 95.0
FAIR_AT = 80.0

CONTENT_WIDTH = LETTER[0] - 1.2 * inch

# Bounds that keep a large run's PDF quick to build and readable. The
# Excel report always has every row; the PDF says when it stops short.
MAX_PATTERN_CHARTS = 6
MAX_PATTERNS = 60

NOT_APPLICABLE = "N/A"
BLANK = "<i>(blank)</i>"


def _styles():

    styles = getSampleStyleSheet()

    def add(name, **kwargs):
        styles.add(ParagraphStyle(name=name, **kwargs))

    add("ReportTitle", fontName="Helvetica-Bold", fontSize=20, leading=24,
        textColor=ACCENT_COLOR, spaceAfter=4)
    add("ReportSubtitle", fontName="Helvetica", fontSize=9.5, leading=13,
        textColor=MUTED_COLOR, spaceAfter=14)
    add("SectionHeading", fontName="Helvetica-Bold", fontSize=13, leading=16,
        textColor=ACCENT_COLOR, spaceBefore=16, spaceAfter=6)
    add("BodyNote", fontName="Helvetica", fontSize=8.5, leading=11.5,
        textColor=MUTED_COLOR, spaceAfter=6)
    add("WarningNote", fontName="Helvetica-Bold", fontSize=9.5, leading=13,
        textColor=FAIR_COLOR, spaceAfter=10)
    add("CellText", fontName="Helvetica", fontSize=8.5, leading=11,
        textColor=colors.black)
    add("CellRight", parent=styles["CellText"], alignment=2)
    add("HeaderCellText", fontName="Helvetica-Bold", fontSize=8.5, leading=11,
        textColor=colors.white)
    add("TileValue", fontName="Helvetica-Bold", fontSize=20, leading=24,
        textColor=ACCENT_COLOR)
    add("TileLabel", fontName="Helvetica", fontSize=8, leading=10,
        textColor=MUTED_COLOR)
    add("CountValue", fontName="Helvetica-Bold", fontSize=13, leading=16,
        textColor=ACCENT_COLOR)

    return styles


# =============================================================
# FORMATTING HELPERS
# =============================================================

def _text(value):
    """
    A cell value as Paragraph markup. Values are escaped -- a "<"
    or "&" in a value would otherwise break the build -- and a
    blank cell reads "(blank)", so it is never confused with a
    value that literally is "-".
    """

    text = "" if value is None else str(value)

    return escape(text) if text.strip() else BLANK


def _pct_text(value):
    """A percentage that may be None (nothing to divide by) as "N/A", not "0%"."""

    if value is None:
        return NOT_APPLICABLE

    return f"{value:.2f}%"


def _rate_color(value):

    if value is None:
        return NEUTRAL_COLOR

    if value >= GOOD_AT:
        return GOOD_COLOR

    if value >= FAIR_AT:
        return FAIR_COLOR

    return POOR_COLOR


def _page_key(page):

    try:
        return (0, int(float(page)), "")
    except (TypeError, ValueError):
        return (1, 0, str(page))


def _page_ranges(pages):
    """[1, 2, 3, 5, 9, 10] -> "1-3, 5, 9-10" (en dash), numeric order."""

    numbers = sorted({_page_key(page) for page in pages})
    parts = []
    run_start = run_end = None

    def close_run():
        if run_start is None:
            return
        parts.append(
            str(run_start) if run_start == run_end else f"{run_start}–{run_end}"
        )

    for kind, number, text in numbers:
        if kind == 1:
            continue
        if run_end is not None and number == run_end + 1:
            run_end = number
            continue
        close_run()
        run_start = run_end = number

    close_run()
    parts += [text for kind, _, text in numbers if kind == 1]

    return ", ".join(parts)


def _pages_by_chart(entries, single_chart=False, max_charts=None):
    """[(pdf_id, page), ...] -> "70084804: 1-6, 71; 70085129: 3" in first-seen
    chart order; just "1-6, 71" when the whole run is one chart. Past
    `max_charts`, the rest is counted ("... and 12 more charts") so one cell
    never outgrows a page."""

    by_chart = defaultdict(list)

    for pdf_id, page in entries:
        by_chart[str(pdf_id)].append(page)

    if single_chart and len(by_chart) == 1:
        (pages,) = by_chart.values()
        return _page_ranges(pages)

    charts = list(by_chart.items())
    shown = charts if max_charts is None else charts[:max_charts]
    text = "; ".join(f"{chart}: {_page_ranges(pages)}" for chart, pages in shown)

    if len(charts) > len(shown):
        text += f" … and {len(charts) - len(shown)} more charts"

    return text


# =============================================================
# TABLE HELPERS
# =============================================================

def _table(rows, col_widths, styles, right_cols=(), header=True):
    """
    A consistently styled table: dark header row, light zebra
    striping, thin grid lines. Every cell is a Paragraph (already
    markup) so long values wrap within their column; a Flowable
    (e.g. a bar) is placed as-is.
    """

    data = []

    for row_index, row in enumerate(rows):
        is_header = header and row_index == 0
        cells = []
        for col_index, cell in enumerate(row):
            if not isinstance(cell, str):
                cells.append(cell)
                continue
            if is_header:
                style = styles["HeaderCellText"]
            elif col_index in right_cols:
                style = styles["CellRight"]
            else:
                style = styles["CellText"]
            cells.append(Paragraph(cell, style))
        data.append(cells)

    table = Table(data, colWidths=col_widths, repeatRows=1 if header else 0)

    commands = [
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]

    if header:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), HEADER_BG))

    first_body_row = 1 if header else 0

    for row_index in range(first_body_row, len(data)):
        if (row_index - first_body_row) % 2 == 1:
            commands.append(("BACKGROUND", (0, row_index), (-1, row_index), ROW_ALT_BG))

    table.setStyle(TableStyle(commands))

    return table


def _bar(percentage, width=1.3 * inch, height=7):
    """A horizontal accuracy bar: grey track, filled to `percentage`."""

    drawing = Drawing(width, height)
    drawing.add(Rect(0, 0, width, height, fillColor=TRACK_COLOR, strokeColor=None))

    if percentage:
        drawing.add(Rect(
            0, 0, width * min(percentage, 100.0) / 100.0, height,
            fillColor=_rate_color(percentage), strokeColor=None,
        ))

    return drawing


def _tiles(items, styles, value_style="TileValue"):
    """
    A row of headline tiles: [(label, value_text, color), ...],
    each a big number over its label with a coloured left rule.
    """

    width = CONTENT_WIDTH / len(items)

    cells = [
        [
            Paragraph(
                f'<font color="{color.hexval().replace("0x", "#")}">{escape(value)}</font>',
                styles[value_style],
            ),
            Paragraph(escape(label), styles["TileLabel"]),
        ]
        for label, value, color in items
    ]

    table = Table([cells], colWidths=[width] * len(items))

    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, 0), (-1, -1), ROW_ALT_BG),
    ]

    for col_index, (_, _, color) in enumerate(items):
        commands.append(("LINEBEFORE", (col_index, 0), (col_index, 0), 3, color))

    table.setStyle(TableStyle(commands))

    return table


# A table this short moves to the next page whole, with its heading,
# rather than splitting; a longer one starts where it is and repeats
# its header row on each page.
KEEP_WHOLE_ROWS = 20


def _section(elements, styles, heading, note, body, empty_message):
    """
    One report section: heading + note, then the body, or a plain
    "nothing here" line when there's no body. A heading is never
    stranded at the foot of a page.
    """

    block = [Paragraph(heading, styles["SectionHeading"])]

    if note:
        block.append(Paragraph(note, styles["BodyNote"]))

    if body is None:
        block.append(Paragraph(empty_message, styles["BodyNote"]))
        elements.append(KeepTogether(block))
        return

    if isinstance(body, Table) and len(body._cellvalues) <= KEEP_WHOLE_ROWS:
        elements.append(KeepTogether(block + [body]))
        return

    # Room for the heading, note and the first rows of a long table, or
    # it starts on the next page.
    elements.append(CondPageBreak(1.6 * inch))
    elements.extend(block)
    elements.append(body)


class _NumberedCanvas(pdf_canvas.Canvas):
    """Footer on every page: report title, then "Page N of M" (needs every page first)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pages = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont("Helvetica", 7.5)
            self.setFillColor(MUTED_COLOR)
            self.drawString(0.6 * inch, 0.4 * inch, REPORT_TITLE)
            self.drawRightString(
                LETTER[0] - 0.6 * inch, 0.4 * inch, f"Page {self._pageNumber} of {total}"
            )
            super().showPage()
        super().save()


# =============================================================
# DERIVED VIEWS
#
# Regroupings of compare_files()'s rows and evidence -- counts
# only, no new rule for what is correct.
# =============================================================

def _chart_rows(result_rows):
    """
    One entry per PDF ID: its ground-truth pages and how they came
    out. Worst exact-match rate first, so the charts to look at
    lead the table.
    """

    charts = {}

    for row in result_rows:
        pdf_id = str(row.get("PDF ID", ""))
        chart = charts.setdefault(pdf_id, defaultdict(int))
        chart[row["Status"]] += 1

    entries = []

    for pdf_id, counts in charts.items():
        ground_pages = sum(counts[s] for s in ("CORRECT", "ERROR", "MISSING", "UNVALIDATED", "EXCLUDED"))
        rate = round(counts["CORRECT"] / ground_pages * 100, 2) if ground_pages else None
        entries.append({
            "pdf_id": pdf_id,
            "ground_pages": ground_pages,
            "correct": counts["CORRECT"],
            "error": counts["ERROR"],
            "missing": counts["MISSING"],
            "extra": counts["EXTRA"],
            "unvalidated": counts["UNVALIDATED"],
            "rate": rate,
        })

    return sorted(
        entries,
        key=lambda e: (e["rate"] is None, e["rate"] if e["rate"] is not None else 0, e["pdf_id"]),
    )


def _mismatch_patterns(mismatch_evidence):
    """
    Identical disagreements grouped: one entry per (field, ground
    truth value, algo value) with how often and on which pages.
    Most frequent first -- a vocabulary gap ("ED Note" vs
    "Progress note") shows as one line, not forty.
    """

    groups = {}

    for entry in mismatch_evidence:
        key = (
            entry["field"],
            str(entry["ground_truth_value"]).strip(),
            str(entry["system_value"]).strip(),
        )
        groups.setdefault(key, []).append((entry["pdf_id"], entry["page_number"]))

    return sorted(
        (
            {"field": field, "gt": gt, "algo": algo, "count": len(pages), "pages": pages}
            for (field, gt, algo), pages in groups.items()
        ),
        key=lambda p: (-p["count"], p["field"], p["gt"], p["algo"]),
    )


# =============================================================
# BUILD
# =============================================================

def build_master_summary_pdf(
    comparison_result,
    ground_label="Ground Truth",
    system_label="Algo Generated",
):
    """
    Build the Master Summary PDF for a comparison run:
      Headline tiles, then
      1. Overall Comparison Summary -- every count and rate, with
         its denominator spelled out.
      2. Chart-wise Summary -- per PDF ID, worst first.
      3. Field-wise Accuracy -- every compared field, worst first.
      4. Top Mismatch Patterns -- identical disagreements grouped.
      5. Missing / Extra Pages -- as page ranges per chart.
      6. Columns Not Compared -- exempted, and headers nothing reads.

    Returns the PDF as raw bytes.
    """

    styles = _styles()
    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        topMargin=0.6 * inch,
        bottomMargin=0.7 * inch,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        title="Master Summary",
        author="Manual Review",
    )

    r = comparison_result
    elements = []

    # ---------------------------------------------------------
    # Header and headline tiles
    # ---------------------------------------------------------

    elements.append(Paragraph(REPORT_TITLE, styles["ReportTitle"]))

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    elements.append(Paragraph(
        f"Generated {generated_at} &nbsp;|&nbsp; <b>{escape(ground_label)}</b> "
        f"vs <b>{escape(system_label)}</b>",
        styles["ReportSubtitle"],
    ))

    ground_count = r["ground_count"]
    matched_count = r["matched_count"]
    charts_compared = r.get("total_unique_charts_compared")

    elements.append(_tiles([
        ("Exact match rate (all fields agree)", _pct_text(r["exact_match_rate"]),
         _rate_color(r["exact_match_rate"])),
        ("Discrepancy rate (error or missing)", _pct_text(r["overall_discrepancy_rate"]),
         NEUTRAL_COLOR),
        ("Pages matched of ground truth", f"{matched_count} / {ground_count}", NEUTRAL_COLOR),
        ("Charts compared", "-" if charts_compared is None else str(charts_compared),
         NEUTRAL_COLOR),
    ], styles))

    elements.append(Spacer(1, 6))

    elements.append(_tiles([
        ("Correct", str(r["correct_count"]), GOOD_COLOR),
        ("Errors", str(r["error_count"]), POOR_COLOR),
        ("Missing", str(r["missing_count"]), FAIR_COLOR),
        ("Extra", str(r["extra_count"]), NEUTRAL_COLOR),
        ("Unvalidated", str(r.get("unvalidated_count", 0)), MUTED_COLOR),
        ("Excluded", str(r.get("excluded_count", 0)), MUTED_COLOR),
    ], styles, value_style="CountValue"))

    unvalidated_count = r.get("unvalidated_count", 0)

    if unvalidated_count:
        elements.append(Spacer(1, 8))
        elements.append(Paragraph(
            f"WARNING: {unvalidated_count} matched page(s) are UNVALIDATED: PDF ID + "
            f"Page Number matched, but no field could be compared for them. This is "
            f"not evidence that their fields agree -- do not read UNVALIDATED as CORRECT.",
            styles["WarningNote"],
        ))

    fields = list(r.get("active_comparison_fields", []))
    elements.append(Spacer(1, 6))
    elements.append(Paragraph(
        f"<b>Compared ({len(fields)}):</b> "
        + (escape(", ".join(fields)) if fields else "none"),
        styles["BodyNote"],
    ))

    # ---------------------------------------------------------
    # 1. Overall Comparison Summary
    # ---------------------------------------------------------

    def count_row(metric, value, basis):
        return [escape(metric), escape(str(value)), escape(basis)]

    overview = [
        ["Metric", "Value", "Denominator / Basis"],
        count_row("Ground Truth pages", ground_count, "Rows in the ground truth"),
        count_row("Algo Generated pages", r["system_count"],
                  "Algo rows for pages the ground truth lists"),
        count_row("Matched", matched_count, "Pages in both (CORRECT + ERROR + UNVALIDATED + EXCLUDED)"),
        count_row("CORRECT", r["correct_count"], "Matched; every compared field agreed"),
        count_row("ERROR", r["error_count"], "Matched; one or more fields disagreed"),
        count_row("UNVALIDATED", unvalidated_count,
                  "Matched with no field to compare -- not evidence of agreement"),
        count_row("EXCLUDED (Blank/Blank)", r.get("excluded_blank_count", 0),
                  "Matched; both sides call the page Blank -- not evaluated"),
        count_row("EXCLUDED (Junk/Junk)", r.get("excluded_junk_count", 0),
                  "Matched; both sides call the page Junk -- not evaluated"),
        count_row("Evaluated Matched Records", r.get("evaluated_count", matched_count),
                  "Matched minus EXCLUDED -- the denominator of the field-wise accuracy"),
        count_row("Evaluated Accuracy Rate", _pct_text(r.get("evaluated_accuracy_rate")),
                  "CORRECT / Evaluated Matched Records (N/A when nothing was compared)"),
        count_row("MISSING", r["missing_count"], "Ground truth pages with no algo row"),
        count_row("EXTRA", r["extra_count"],
                  "Algo pages with no ground truth row (not in the discrepancy rate)"),
        count_row("Exact Match Rate", _pct_text(r["exact_match_rate"]),
                  "CORRECT / Ground Truth pages (N/A when nothing was compared)"),
        count_row("Record Key Match Rate", _pct_text(r["overall_match_percentage"]),
                  "Matched / Ground Truth pages -- linkage by PDF ID + Page Number only, "
                  "not field accuracy"),
        count_row("Overall Discrepancy Rate", _pct_text(r["overall_discrepancy_rate"]),
                  "(ERROR + MISSING) / Ground Truth pages"),
        count_row("Extra Rate", _pct_text(r["extra_rate"]), "EXTRA / Algo Generated pages"),
        count_row("Pages with a discrepancy", r["unique_discrepancy_count"],
                  "ERROR + MISSING + EXTRA (disjoint, no double count)"),
    ]

    for metric, key, basis in (
        ("Ground Truth charts", "total_unique_ground_charts", "Distinct PDF IDs in the ground truth"),
        ("Algo Generated charts", "total_unique_system_charts", "Distinct PDF IDs in the algo output"),
        ("Charts compared", "total_unique_charts_compared", "Distinct PDF IDs in either"),
    ):
        if r.get(key) is not None:
            overview.append(count_row(metric, r[key], basis))

    _section(
        elements, styles,
        "1. Overall Comparison Summary",
        None,
        _table(overview, [1.9 * inch, 0.9 * inch, CONTENT_WIDTH - 2.8 * inch], styles,
               right_cols=(1,)),
        "",
    )

    # ---------------------------------------------------------
    # 2. Chart-wise Summary
    # ---------------------------------------------------------

    charts = _chart_rows(r.get("result_rows", []))
    chart_table = None

    if charts:
        rows = [["PDF ID", "GT pages", "Correct", "Errors", "Missing", "Extra",
                 "Exact match", ""]]
        for chart in charts:
            rows.append([
                escape(chart["pdf_id"]),
                str(chart["ground_pages"]),
                str(chart["correct"]),
                str(chart["error"]),
                str(chart["missing"]),
                str(chart["extra"]),
                _pct_text(chart["rate"]),
                _bar(chart["rate"] or 0, width=1.1 * inch),
            ])
        chart_table = _table(
            rows,
            [1.55 * inch, 0.7 * inch, 0.65 * inch, 0.6 * inch, 0.65 * inch, 0.55 * inch,
             0.85 * inch, CONTENT_WIDTH - 5.55 * inch],
            styles,
            right_cols=(1, 2, 3, 4, 5, 6),
        )

    _section(
        elements, styles,
        "2. Chart-wise Summary",
        "One row per chart (PDF ID), lowest exact-match rate first. Exact match = "
        "CORRECT / the chart's ground truth pages.",
        chart_table,
        "No charts in this run.",
    )

    # ---------------------------------------------------------
    # 3. Field-wise Accuracy
    # ---------------------------------------------------------

    field_error_summary = r.get("field_error_summary", [])
    field_table = None

    if field_error_summary:
        rows = [["Field", "Mismatches", "Accuracy", ""]]
        for entry in field_error_summary:
            accuracy = (
                round(100.0 - entry["error_percentage"], 2) if r.get("evaluated_count", matched_count) else None
            )
            rows.append([
                escape(entry["field"]),
                str(entry["error_count"]),
                _pct_text(accuracy),
                _bar(accuracy or 0, width=2.2 * inch),
            ])
        field_table = _table(
            rows,
            [2.1 * inch, 0.95 * inch, 0.95 * inch, CONTENT_WIDTH - 4.0 * inch],
            styles,
            right_cols=(1, 2),
        )

    _section(
        elements, styles,
        "3. Field-wise Accuracy",
        "Every compared field, most mismatches first. Accuracy = evaluated pages that "
        "agreed on the field / Evaluated Matched Records (Blank/Blank and Junk/Junk "
        "pages are not evaluated). A page with several mismatched fields "
        "counts once per field, so mismatches can add up to more than ERROR.",
        field_table,
        "No fields were compared this run.",
    )

    # ---------------------------------------------------------
    # 4. Top Mismatch Patterns
    # ---------------------------------------------------------

    patterns = _mismatch_patterns(r.get("mismatch_evidence", []))
    pattern_table = None
    pattern_note = (
        "Identical disagreements grouped: each line is one field where the ground truth "
        "said one thing and the algo another, with how many pages and which. Fixing a "
        "line at the top fixes every page it lists. Dates read MM/DD/YYYY on both sides. "
        "A \"-\" is a display placeholder for a blank cell; it does not imply a value."
    )

    if len(patterns) > MAX_PATTERNS:
        pattern_note += (
            f" The {MAX_PATTERNS} most frequent of {len(patterns)} patterns are shown; "
            f"the Excel report has every mismatch."
        )

    if patterns:
        rows = [["Field", "Ground Truth", "Algo Generated", "Pages", "Where"]]
        for pattern in patterns[:MAX_PATTERNS]:
            rows.append([
                escape(pattern["field"]),
                _text(pattern["gt"]),
                _text(pattern["algo"]),
                str(pattern["count"]),
                escape(_pages_by_chart(
                    pattern["pages"],
                    single_chart=charts_compared == 1,
                    max_charts=MAX_PATTERN_CHARTS,
                )),
            ])
        pattern_table = _table(
            rows,
            [1.35 * inch, 1.5 * inch, 1.5 * inch, 0.55 * inch, CONTENT_WIDTH - 4.9 * inch],
            styles,
            right_cols=(3,),
        )

    _section(
        elements, styles,
        "4. Top Mismatch Patterns",
        pattern_note,
        pattern_table,
        "No field-level mismatches.",
    )

    # ---------------------------------------------------------
    # 5. Missing / Extra Pages
    # ---------------------------------------------------------

    result_rows = r.get("result_rows", [])
    gap_rows = [["PDF ID", "Missing pages (no algo row)", "Extra pages (no ground truth)"]]
    gaps = defaultdict(lambda: {"MISSING": [], "EXTRA": []})

    for row in result_rows:
        if row.get("Status") in ("MISSING", "EXTRA"):
            gaps[str(row.get("PDF ID", ""))][row["Status"]].append(row.get("Page Number", ""))

    for pdf_id, pages in gaps.items():
        gap_rows.append([
            escape(pdf_id),
            escape(f"{_page_ranges(pages['MISSING'])} ({len(pages['MISSING'])})")
            if pages["MISSING"] else "-",
            escape(f"{_page_ranges(pages['EXTRA'])} ({len(pages['EXTRA'])})")
            if pages["EXTRA"] else "-",
        ])

    _section(
        elements, styles,
        "5. Missing / Extra Pages",
        "Pages present on one side only, as page ranges per chart (count in brackets).",
        _table(gap_rows, [1.5 * inch, (CONTENT_WIDTH - 1.5 * inch) / 2,
                          (CONTENT_WIDTH - 1.5 * inch) / 2], styles)
        if len(gap_rows) > 1 else None,
        "Every page is on both sides.",
    )

    # ---------------------------------------------------------
    # 6. Columns Not Compared
    # ---------------------------------------------------------

    not_compared = [["Column", "Why"]]

    by_reason = defaultdict(list)
    for entry in r.get("exempted_columns", []):
        by_reason[entry["reason"]].append(entry["column"])
    for reason, columns in by_reason.items():
        not_compared.append([escape(", ".join(columns)), escape(reason)])

    unrecognized = r.get("unrecognized_columns", {})
    for side, label in (("ground", "Ground Truth"), ("system", "Algo Generated")):
        columns = unrecognized.get(side) or []
        if columns:
            not_compared.append([
                escape(", ".join(columns)),
                escape(f"Header in the {label} file that no compared column reads "
                       f"(check the spelling if it should be compared)."),
            ])

    _section(
        elements, styles,
        "6. Columns Not Compared",
        "Exempted columns, and headers left out because nothing reads them.",
        _table(not_compared, [2.6 * inch, CONTENT_WIDTH - 2.6 * inch], styles)
        if len(not_compared) > 1 else None,
        "Every column was compared.",
    )

    doc.build(elements, canvasmaker=_NumberedCanvas)

    return buffer.getvalue()
