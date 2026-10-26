from pathlib import Path

import pandas as pd

from .field_comparator import ACTIVE_COMPARISON_FIELDS, DATE_FIELDS, IDENTIFIER_FIELDS, NAME_FIELDS
from .normalizer import clean_identifier, display_date, display_name


# =============================================================
# RESULT ROW BUILDERS
# =============================================================

def _raw_value(row, field_name):

    value = row.get(field_name, "")

    if pd.isna(value):
        return ""

    return str(value).strip()


def _field_value(row, field_name):
    """A field shown on its own (Missing / Extra): dates read MM/DD/YYYY, IDs
    without a float's ".0", names as display_name()."""

    value = _raw_value(row, field_name)

    if field_name in DATE_FIELDS:
        return display_date(value)

    if field_name in IDENTIFIER_FIELDS:
        return clean_identifier(value)

    if field_name in NAME_FIELDS:
        return display_name(value)

    return value


def build_associated_result(
    pdf_id,
    page_number,
    field_results,
    status,
    reason,
    fields=None,
):
    """
    Build a compact human-readable result row for a matched
    Ground Truth / Algo Generated pair.

    `fields` is the subset of ACTIVE_COMPARISON_FIELDS actually
    compared for this run (fields exempted -- either always, or
    because they weren't present in both files -- are left out
    of the row entirely).

    Matching (PDF ID + Page Number) only determines which rows
    correspond. Status here comes exclusively from
    field_comparator.determine_status, which independently
    compares the actual field values.
    """

    if fields is None:
        fields = ACTIVE_COMPARISON_FIELDS

    row = {
        "PDF ID": pdf_id,
        "Page Number": page_number,
    }

    for field_name in fields:
        row[field_name] = field_results[field_name]["display"]

    row["Status"] = status
    row["Reason"] = reason

    return row


def build_missing_result(gt_row, fields=None):
    """
    Build a result row for a Ground Truth record that has no
    corresponding Algo Generated record.
    """

    if fields is None:
        fields = ACTIVE_COMPARISON_FIELDS

    pdf_id = _raw_value(gt_row, "PDF ID")
    page_number = _raw_value(gt_row, "Page Number")

    row = {
        "PDF ID": pdf_id,
        "Page Number": page_number,
    }

    for field_name in fields:
        row[field_name] = _field_value(gt_row, field_name)

    row["Status"] = "MISSING"
    row["Reason"] = (
        f"No corresponding Algo Generated record found for "
        f"PDF ID {pdf_id}, Page Number {page_number}."
    )

    return row


EXCLUDED_REASON = (
    "Excluded from evaluation: Ground Truth and Algo Generated both call the page {kind}."
)


def build_excluded_result(gt_row, kind, fields=None):
    """
    A matched pair both sides call Blank (or both call Junk): status EXCLUDED, its
    fields not compared -- shown as the ground truth has them.
    """

    if fields is None:
        fields = ACTIVE_COMPARISON_FIELDS

    row = {
        "PDF ID": _raw_value(gt_row, "PDF ID"),
        "Page Number": _raw_value(gt_row, "Page Number"),
    }

    for field_name in fields:
        row[field_name] = _field_value(gt_row, field_name)

    row["Status"] = "EXCLUDED"
    row["Reason"] = EXCLUDED_REASON.format(kind=kind.capitalize())
    row["Excluded As"] = kind

    return row


def build_extra_result(system_row, fields=None):
    """
    Build a result row for a Algo Generated record that has
    no corresponding Ground Truth record.
    """

    if fields is None:
        fields = ACTIVE_COMPARISON_FIELDS

    pdf_id = _raw_value(system_row, "PDF ID")
    page_number = _raw_value(system_row, "Page Number")

    row = {
        "PDF ID": pdf_id,
        "Page Number": page_number,
    }

    for field_name in fields:
        row[field_name] = _field_value(system_row, field_name)

    row["Status"] = "EXTRA"
    row["Reason"] = (
        f"No corresponding Ground Truth record found for "
        f"PDF ID {pdf_id}, Page Number {page_number}."
    )

    return row


# =============================================================
# SUMMARY
# =============================================================

def build_summary(
    ground_count, system_count, result_rows,
    fields_were_compared=True,
    total_unique_ground_charts=None,
    total_unique_system_charts=None,
    total_unique_charts_compared=None,
):
    """
    Build compact summary metrics across all compared records.

    `fields_were_compared=False` means zero comparison fields
    were available this run -- every matched record is
    "UNVALIDATED" rather than CORRECT/ERROR, so Correct Records /
    Match Percentage are reported as "N/A" rather than a
    misleading 0%.

    The three `total_unique_*_charts` args are optional (None
    omits those rows), so a caller that doesn't count charts gets
    the same rows as before.
    """

    df = pd.DataFrame(result_rows)

    if df.empty:
        df = pd.DataFrame(columns=["Status"])

    correct_count = int((df["Status"] == "CORRECT").sum())
    error_count = int((df["Status"] == "ERROR").sum())
    missing_count = int((df["Status"] == "MISSING").sum())
    extra_count = int((df["Status"] == "EXTRA").sum())
    unvalidated_count = int((df["Status"] == "UNVALIDATED").sum())
    excluded = df[df["Status"] == "EXCLUDED"]
    excluded_kind = excluded["Excluded As"] if "Excluded As" in excluded.columns else pd.Series(dtype=str)
    excluded_blank_count = int((excluded_kind == "blank").sum())
    excluded_junk_count = int((excluded_kind == "junk").sum())
    # Matched pages actually evaluated: Blank/Blank and Junk/Junk pages are matched but
    # never compared.
    evaluated_count = correct_count + error_count + unvalidated_count

    # ---------------------------------------------------------
    # Match Percentage
    #
    # Correct records / all Ground Truth records. Missing
    # records therefore count as not successfully matched.
    # ---------------------------------------------------------

    if ground_count:
        match_percentage = (correct_count / ground_count) * 100
    else:
        match_percentage = 0.0

    match_percentage_display = (
        "N/A" if not fields_were_compared else round(match_percentage, 2)
    )

    if ground_count:
        unvalidated_percentage = round(unvalidated_count / ground_count * 100, 2)
    else:
        unvalidated_percentage = 0.0

    # ---------------------------------------------------------
    # Overall Disparity/Error Percentage
    #
    # Ground Truth records with either ERROR or MISSING.
    # ---------------------------------------------------------

    disparity_count = error_count + missing_count

    if ground_count:
        disparity_percentage = (disparity_count / ground_count) * 100
    else:
        disparity_percentage = 0.0

    # ---------------------------------------------------------
    # Extra Percentage
    # ---------------------------------------------------------

    if system_count:
        extra_percentage = (extra_count / system_count) * 100
    else:
        extra_percentage = 0.0

    summary_rows = [
        {
            "Metric": "Total Ground Truth Records",
            "Count": ground_count,
            "Percentage": 100.0 if ground_count else 0.0,
        },
        {
            "Metric": "Total Algo Records",
            "Count": system_count,
            "Percentage": 100.0 if system_count else 0.0,
        },
        {
            "Metric": "Correct Records",
            "Count": correct_count,
            "Percentage": match_percentage_display,
        },
        {
            "Metric": "Errors",
            "Count": error_count,
            "Percentage": round(
                error_count / ground_count * 100, 2
            ) if ground_count else 0.0,
        },
        {
            "Metric": "Missing Records",
            "Count": missing_count,
            "Percentage": round(
                missing_count / ground_count * 100, 2
            ) if ground_count else 0.0,
        },
        {
            "Metric": "Extra Records",
            "Count": extra_count,
            "Percentage": round(extra_percentage, 2),
        },
        {
            "Metric": "Unvalidated Records",
            "Count": unvalidated_count,
            "Percentage": unvalidated_percentage,
        },
        {
            "Metric": "Excluded (Blank/Blank)",
            "Count": excluded_blank_count,
            "Percentage": round(excluded_blank_count / ground_count * 100, 2) if ground_count else 0.0,
        },
        {
            "Metric": "Excluded (Junk/Junk)",
            "Count": excluded_junk_count,
            "Percentage": round(excluded_junk_count / ground_count * 100, 2) if ground_count else 0.0,
        },
        {
            "Metric": "Evaluated Matched Records",
            "Count": evaluated_count,
            "Percentage": round(evaluated_count / ground_count * 100, 2) if ground_count else 0.0,
        },
        {
            "Metric": "Evaluated Accuracy Rate",
            "Count": correct_count,
            "Percentage": (
                "N/A" if not fields_were_compared or not evaluated_count
                else round(correct_count / evaluated_count * 100, 2)
            ),
        },
        {
            "Metric": "Match Percentage",
            "Count": correct_count,
            "Percentage": match_percentage_display,
        },
        {
            "Metric": "Overall Disparity Rate",
            "Count": disparity_count,
            "Percentage": round(disparity_percentage, 2),
        },
    ]

    # ---------------------------------------------------------
    # Unique Charts
    #
    # A chart is identified by PDF ID alone, independent of Page
    # Number -- not a percentage-based metric, so "Percentage" is
    # a plain "-" placeholder for these rows.
    # ---------------------------------------------------------

    if total_unique_ground_charts is not None:
        summary_rows.append({
            "Metric": "Total Unique Ground Truth Charts",
            "Count": total_unique_ground_charts,
            "Percentage": "-",
        })

    if total_unique_system_charts is not None:
        summary_rows.append({
            "Metric": "Total Unique Algo Charts",
            "Count": total_unique_system_charts,
            "Percentage": "-",
        })

    if total_unique_charts_compared is not None:
        summary_rows.append({
            "Metric": "Total Unique Charts Compared",
            "Count": total_unique_charts_compared,
            "Percentage": "-",
        })

    return pd.DataFrame(summary_rows)


# =============================================================
# EXEMPTED COLUMNS
#
# Columns recognized by the schema but not compared this run --
# either always exempt by design (subjective fields) or
# exempted because they weren't present in both input files.
# =============================================================

def build_exempted_columns_df(exempted_columns):
    """
    Build a small reference table of exempted columns and why
    each one was left out of the comparison.

    `exempted_columns` is a list of {"column": str, "reason": str}.
    """

    if not exempted_columns:
        return pd.DataFrame(columns=["Column", "Reason"])

    return pd.DataFrame(
        [
            {"Column": entry["column"], "Reason": entry["reason"]}
            for entry in exempted_columns
        ]
    )


# =============================================================
EXCLUDED_RECORD_COLUMNS = ["PDF ID", "Page Number", "Excluded As", "Exclusion Reason"]


def build_excluded_records_df(result_rows):
    """The pages excluded from evaluation (Blank/Blank, Junk/Junk), for their own sheet."""

    rows = [
        {
            "PDF ID": row.get("PDF ID", ""),
            "Page Number": row.get("Page Number", ""),
            "Excluded As": str(row.get("Excluded As", "")).capitalize(),
            "Exclusion Reason": row.get("Reason", ""),
        }
        for row in result_rows
        if row.get("Status") == "EXCLUDED"
    ]

    return pd.DataFrame(rows, columns=EXCLUDED_RECORD_COLUMNS)


# EXCEL WRITER
# =============================================================

def write_report(
    output_path,
    result_rows,
    summary_df,
    fields=None,
    exempted_columns=None,
):
    """
    Write the final compact Excel report.
    """

    if fields is None:
        fields = ACTIVE_COMPARISON_FIELDS

    if isinstance(output_path, (str, Path)):
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------
    # Explicit column order
    # ---------------------------------------------------------

    result_columns = (
        ["PDF ID", "Page Number"]
        + fields
        + ["Status", "Reason"]
    )

    results_df = pd.DataFrame(result_rows)

    for column in result_columns:
        if column not in results_df.columns:
            results_df[column] = ""

    results_df = results_df[result_columns]

    exempted_df = build_exempted_columns_df(exempted_columns)
    excluded_df = build_excluded_records_df(result_rows)

    # ---------------------------------------------------------
    # Write workbook
    # ---------------------------------------------------------

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:

        results_df.to_excel(
            writer,
            sheet_name="Comparison Results",
            index=False,
        )

        summary_df.to_excel(
            writer,
            sheet_name="Summary",
            index=False,
        )

        exempted_df.to_excel(
            writer,
            sheet_name="Exempted Columns",
            index=False,
        )

        excluded_df.to_excel(
            writer,
            sheet_name="Excluded Records",
            index=False,
        )

        workbook = writer.book

        # -----------------------------------------------------
        # Formatting
        # -----------------------------------------------------

        for worksheet in workbook.worksheets:

            worksheet.freeze_panes = "A2"

            worksheet.auto_filter.ref = worksheet.dimensions

            # Header formatting
            for cell in worksheet[1]:
                cell.font = cell.font.copy(bold=True)

            # Column widths
            for column_cells in worksheet.columns:

                max_length = 0
                column_letter = column_cells[0].column_letter

                for cell in column_cells:

                    if cell.value is None:
                        continue

                    max_length = max(max_length, len(str(cell.value)))

                worksheet.column_dimensions[column_letter].width = min(
                    max_length + 2, 60
                )

    return output_path
