from .file_reader import read_tabular_file
from .header_normalizer import normalize_headers
from .record_matcher import KEY_COLUMNS, match_records
from .field_comparator import (
    ACTIVE_COMPARISON_FIELDS,
    ALWAYS_EXEMPT_FIELDS,
    ALWAYS_EXEMPT_REASON,
    compare_fields,
    determine_status,
    build_reason,
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
        return pd.read_csv(file_object)

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
        reason = build_reason(status, field_results)

        row = build_associated_result(
            gt_row.get("PDF ID", ""),
            gt_row.get("Page Number", ""),
            field_results,
            status,
            reason,
            fields_to_compare,
        )
        # review-ui highlights these cells. write_report picks its columns
        # explicitly, so the workbook is unchanged.
        row["Mismatched"] = [
            field_name
            for field_name, field_result in field_results.items()
            if not field_result["match"]
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

    result_rows, missing_count, extra_count = compare_frames(
        ground, system, fields_to_compare
    )

    # ---------------------------------------------------------
    # Summary
    # ---------------------------------------------------------

    summary_df = build_summary(len(ground), len(system), result_rows)

    correct_count = sum(
        1 for row in result_rows if row["Status"] == "CORRECT"
    )
    error_count = sum(
        1 for row in result_rows if row["Status"] == "ERROR"
    )

    return {
        "ground_count": len(ground),
        "system_count": len(system),
        "correct_count": correct_count,
        "error_count": error_count,
        "missing_count": missing_count,
        "extra_count": extra_count,
        "result_rows": result_rows,
        "summary_df": summary_df,
        "active_comparison_fields": fields_to_compare,
        "exempted_columns": exempted_columns,
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
