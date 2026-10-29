from .file_reader import read_csv_any_encoding, read_tabular_file
from .header_normalizer import normalize_headers, unrecognized_columns
from .record_matcher import KEY_COLUMNS, match_records, unique_pdf_ids
from .field_comparator import (
    ACTIVE_COMPARISON_FIELDS,
    ALWAYS_EXEMPT_FIELDS,
    ALWAYS_EXEMPT_REASON,
    compare_fields,
    determine_status,
    build_reason,
    mutual_blank_or_junk,
)

from .result_builder import (
    build_associated_result,
    build_missing_result,
    build_extra_result,
    build_summary,
    write_report,
)


# =============================================================
# REQUIRED COLUMNS
#
# Only the matching key is strictly required. Comparison fields
# are never a hard requirement any more -- a field missing from
# either file is exempted from comparison (see
# _resolve_comparison_fields) rather than failing validation.
# =============================================================

REQUIRED_COLUMNS = list(KEY_COLUMNS)


NOT_PRESENT_IN_BOTH_REASON = "Not present in both files."


def _safe_rate(numerator, denominator):
    """
    Percentage, or None when the denominator is zero (empty
    input) so callers can render "N/A" instead of a misleading
    0% for a metric that isn't actually applicable.
    """

    if not denominator:
        return None

    return round(numerator / denominator * 100, 2)


# =============================================================
# FILE READER
# =============================================================

def read_uploaded_file(file_object):
    """
    Read a Streamlit UploadedFile safely.

    The existing read_tabular_file() expects a filesystem
    path, so uploaded files are read directly from memory
    according to their extension.
    """

    import pandas as pd

    filename = getattr(file_object, "name", "")

    if not filename:
        raise ValueError("Uploaded file has no filename.")

    extension = filename.lower().split(".")[-1]

    if hasattr(file_object, "seek"):
        file_object.seek(0)

    if extension in {"xlsx", "xls"}:
        return pd.read_excel(file_object)

    if extension == "csv":
        return read_csv_any_encoding(file_object)

    raise ValueError(f"Unsupported file type: .{extension}")


def _read_file(file_object):

    if hasattr(file_object, "read"):
        df = read_uploaded_file(file_object)
    else:
        df = read_tabular_file(file_object)

    # Header normalization happens here, internally, on the
    # in-memory DataFrame only -- the uploaded/source file on
    # disk is never modified.
    return normalize_headers(df)


# =============================================================
# VALIDATION
# =============================================================

def _validate_columns(df, label):

    missing = [
        column
        for column in REQUIRED_COLUMNS
        if column not in df.columns
    ]

    if missing:
        raise ValueError(
            f"{label} file is missing required column(s): "
            f"{', '.join(missing)}."
        )


# =============================================================
# EXEMPTED COLUMNS
# =============================================================

def _resolve_comparison_fields(ground, system):
    """
    Decide which ACTIVE_COMPARISON_FIELDS are actually compared
    this run, and build the full exempted-columns notice.

    A field is exempted for one of two reasons:
      - it's always exempt by design (ALWAYS_EXEMPT_FIELDS), or
      - it isn't present in both the Ground Truth and System
        files for this run.

    Returns (fields_to_compare, exempted_columns).
    """

    fields_to_compare = [
        field_name
        for field_name in ACTIVE_COMPARISON_FIELDS
        if field_name in ground.columns and field_name in system.columns
    ]

    exempted_columns = [
        {"column": field_name, "reason": NOT_PRESENT_IN_BOTH_REASON}
        for field_name in ACTIVE_COMPARISON_FIELDS
        if field_name not in fields_to_compare
    ]

    exempted_columns += [
        {"column": field_name, "reason": ALWAYS_EXEMPT_REASON}
        for field_name in ALWAYS_EXEMPT_FIELDS
    ]

    return fields_to_compare, exempted_columns


# =============================================================
# CORE COMPARISON
# =============================================================

def restrict_to_ground(ground, system):
    """
    The Algo Generated rows whose (PDF ID, Page Number) the ground truth
    lists -- a left join on the ground truth. Ground truth is the base: an
    unlabelled algo page or chart is not compared, so it never counts as
    Extra. A labelled page with no algo row still comes out Missing.
    """

    from .record_matcher import normalize_key

    def keys(frame):
        return [
            normalize_key(pdf_id, page_number)
            for pdf_id, page_number in zip(frame["PDF ID"], frame["Page Number"])
        ]

    labelled = set(keys(ground))
    keep = [key in labelled for key in keys(system)]
    return system[keep]


def compare_frames(ground, system, fields_to_compare):
    """
    Match and compare two already-read, already-validated frames.

    Returns (result_rows, missing_count, extra_count). compare_files
    runs this once over whole files; review-ui's streaming compare
    runs it once per PDF ID.
    """

    (
        matched_pairs,
        missing_gt_indices,
        extra_system_indices,
    ) = match_records(ground, system)

    result_rows = []

    for gt_index, system_index in matched_pairs:

        gt_row = ground.loc[gt_index]
        system_row = system.loc[system_index]

        field_results = compare_fields(gt_row, system_row, fields_to_compare)
        status = determine_status(field_results)
        # Both sides blank / junk: every field matches (compare_fields), said so.
        reason = build_reason(
            "BLANK_JUNK" if status == "CORRECT" and mutual_blank_or_junk(gt_row, system_row) else status,
            field_results,
        )

        row = build_associated_result(
            gt_row.get("PDF ID", ""),
            gt_row.get("Page Number", ""),
            field_results,
            status,
            reason,
            fields_to_compare,
        )
        # review-ui highlights these cells, and the master summary lists the
        # evidence. write_report picks its columns explicitly, so the
        # workbook is unchanged.
        row["Mismatched"] = [
            field_name
            for field_name, field_result in field_results.items()
            if not field_result["match"]
        ]
        row["Evidence"] = [
            {
                "field": field_name,
                "ground_truth_value": field_results[field_name]["gt_raw"],
                "system_value": field_results[field_name]["system_raw"],
            }
            for field_name in row["Mismatched"]
        ]
        result_rows.append(row)

    for gt_index in missing_gt_indices:
        result_rows.append(
            build_missing_result(ground.loc[gt_index], fields_to_compare)
        )

    for system_index in extra_system_indices:
        result_rows.append(
            build_extra_result(system.loc[system_index], fields_to_compare)
        )

    return result_rows, len(missing_gt_indices), len(extra_system_indices)


def summarize(
    result_rows,
    ground_count,
    system_count,
    fields_to_compare,
    ground_chart_ids,
    system_chart_ids,
):
    """
    Every total, rate and breakdown of a run, from its result rows.
    compare_files and review-ui's streaming compare both call this,
    so the screen, the workbook and the master summary agree.

    Rates:
      Exact Match Rate       = CORRECT / Total Ground Truth
      Overall Match %        = Matched (CORRECT+ERROR+UNVALIDATED+EXCLUDED) / Total Ground Truth
      Evaluated Accuracy     = CORRECT / Evaluated (CORRECT+ERROR+UNVALIDATED)

    EXCLUDED pages (both sides Blank, or both Junk) are matched but never evaluated:
    the field breakdown's percentages are of the evaluated pages.
      Overall Discrepancy %  = (ERROR + MISSING) / Total Ground Truth
      Extra Rate             = EXTRA / Total Algo Generated

    Exact Match Rate is None ("N/A") when zero fields were
    compared: correct_count is structurally 0 then, and "0%"
    would read as "every record disagreed".

    ERROR + MISSING + EXTRA is an exact count of records affected
    by discrepancies: matching puts every key in exactly one of
    matched / missing / extra. UNVALIDATED is neither a match nor
    a discrepancy, so it is in neither.

    A chart is one distinct PDF ID, however many pages it has.
    """

    def count(status):
        return sum(1 for row in result_rows if row["Status"] == status)

    correct_count = count("CORRECT")
    error_count = count("ERROR")
    missing_count = count("MISSING")
    extra_count = count("EXTRA")
    unvalidated_count = count("UNVALIDATED")
    excluded_count = count("EXCLUDED")
    excluded_blank_count = sum(
        1 for row in result_rows if row["Status"] == "EXCLUDED" and row.get("Excluded As") == "blank"
    )
    evaluated_count = correct_count + error_count + unvalidated_count
    matched_count = evaluated_count + excluded_count

    fields_were_compared = bool(fields_to_compare)

    # ---------------------------------------------------------
    # Per-field error breakdown
    #
    # How many matched records disagreed on each field. A record
    # with 2 mismatched fields counts in 2 entries, so these can
    # add up to more than error_count. Percentages are of matched
    # records: missing/extra records were never compared.
    # ---------------------------------------------------------

    field_error_counts = {field_name: 0 for field_name in fields_to_compare}
    mismatch_evidence = []

    for row in result_rows:
        for entry in row.get("Evidence", []):
            field_error_counts[entry["field"]] = field_error_counts.get(entry["field"], 0) + 1
            mismatch_evidence.append({
                "pdf_id": row.get("PDF ID", ""),
                "page_number": row.get("Page Number", ""),
                **entry,
            })

    field_error_summary = sorted(
        (
            {
                "field": field_name,
                "error_count": errors,
                "error_percentage": (
                    round(errors / evaluated_count * 100, 2) if evaluated_count else 0.0
                ),
            }
            for field_name, errors in field_error_counts.items()
        ),
        key=lambda entry: (-entry["error_count"], entry["field"]),
    )

    total_unique_ground_charts = len(ground_chart_ids)
    total_unique_system_charts = len(system_chart_ids)
    total_unique_charts_compared = len(set(ground_chart_ids) | set(system_chart_ids))

    summary_df = build_summary(
        ground_count, system_count, result_rows,
        fields_were_compared=fields_were_compared,
        total_unique_ground_charts=total_unique_ground_charts,
        total_unique_system_charts=total_unique_system_charts,
        total_unique_charts_compared=total_unique_charts_compared,
    )

    return {
        "ground_count": ground_count,
        "system_count": system_count,
        "matched_count": matched_count,
        "correct_count": correct_count,
        "error_count": error_count,
        "missing_count": missing_count,
        "extra_count": extra_count,
        "unvalidated_count": unvalidated_count,
        "excluded_count": excluded_count,
        "excluded_blank_count": excluded_blank_count,
        "excluded_junk_count": excluded_count - excluded_blank_count,
        "evaluated_count": evaluated_count,
        "evaluated_accuracy_rate": (
            _safe_rate(correct_count, evaluated_count) if fields_were_compared and evaluated_count else None
        ),
        "exact_match_rate": (
            _safe_rate(correct_count, ground_count) if fields_were_compared else None
        ),
        "overall_match_percentage": _safe_rate(matched_count, ground_count),
        "overall_discrepancy_rate": _safe_rate(error_count + missing_count, ground_count),
        "extra_rate": _safe_rate(extra_count, system_count),
        "unique_discrepancy_count": error_count + missing_count + extra_count,
        "field_error_summary": field_error_summary,
        "mismatch_evidence": mismatch_evidence,
        "fields_were_compared": fields_were_compared,
        "total_unique_ground_charts": total_unique_ground_charts,
        "total_unique_system_charts": total_unique_system_charts,
        "total_unique_charts_compared": total_unique_charts_compared,
        "summary_df": summary_df,
    }


def compare_files(ground_file, system_file):
    """
    Compare Ground Truth and Algo Generated files on the
    active QA checklist fields.

    Records are matched by an exact (PDF ID, Page Number) key
    -- this determines correspondence only. Once matched, the
    active comparison fields are compared independently to
    decide correctness. A single file pair may contain rows for
    multiple PDF IDs; all are matched and compared in one pass.

    Fields that are always exempt by design, or that aren't
    present in both files, are excluded from comparison and
    from the report entirely; see "exempted_columns" in the
    returned dict for exactly which fields and why.

    Accepts filesystem paths or Streamlit UploadedFile objects.
    """

    ground = _read_file(ground_file)
    system = _read_file(system_file)

    _validate_columns(ground, "Ground Truth")
    _validate_columns(system, "Algo Generated")

    fields_to_compare, exempted_columns = _resolve_comparison_fields(
        ground, system
    )

    system = restrict_to_ground(ground, system)

    result_rows, _, _ = compare_frames(ground, system, fields_to_compare)

    return {
        **summarize(
            result_rows,
            len(ground),
            len(system),
            fields_to_compare,
            unique_pdf_ids(ground),
            unique_pdf_ids(system),
        ),
        "result_rows": result_rows,
        "active_comparison_fields": fields_to_compare,
        "exempted_columns": exempted_columns,
        "unrecognized_columns": {
            "ground": unrecognized_columns(ground),
            "system": unrecognized_columns(system),
        },
    }


def save_report(comparison_result, output_path):
    """
    Save comparison result to an Excel file.
    """

    return write_report(
        output_path,
        comparison_result["result_rows"],
        comparison_result["summary_df"],
        comparison_result["active_comparison_fields"],
        comparison_result["exempted_columns"],
    )
