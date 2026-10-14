import pandas as pd


# =============================================================
# KEY COLUMNS
#
# These columns establish which Ground Truth row corresponds
# to which System Generated row. They are NEVER compared as
# content and must not influence correctness in any way.
# =============================================================

KEY_COLUMNS = [
    "PDF ID",
    "Page Number",
]


def normalize_key(pdf_id, page_number):
    """
    Normalize a (PDF ID, Page Number) pair for reliable
    equality comparison.
    """

    if pd.isna(pdf_id):
        pdf_id_key = ""
    else:
        pdf_id_key = str(pdf_id).strip()

    if pd.isna(page_number):
        page_key = ""
    else:
        try:
            page_key = str(int(float(page_number)))
        except (ValueError, TypeError):
            page_key = str(page_number).strip()

    return (pdf_id_key, page_key)


def _build_key_map(df, label):
    """
    Map each normalized (PDF ID, Page Number) key to the
    single row index that produced it.

    Raises if a key is duplicated within the same file, since
    a 1:1 correspondence cannot be established unambiguously.
    """

    key_map = {}

    for index, row in df.iterrows():

        key = normalize_key(
            row.get("PDF ID"),
            row.get("Page Number"),
        )

        if key in key_map:

            raise ValueError(
                f"Duplicate PDF ID/Page Number combination "
                f"found in {label} file: "
                f"PDF ID={key[0]!r}, Page Number={key[1]!r}. "
                f"Each PDF ID/Page Number pair must be unique "
                f"within a file so records can be matched "
                f"unambiguously."
            )

        key_map[key] = index

    return key_map


def match_records(ground_df, system_df):
    """
    Establish which Ground Truth rows correspond to which
    System Generated rows, using an exact match on
    (PDF ID, Page Number).

    This function determines correspondence ONLY. It must
    never be used, directly or indirectly, to decide whether
    the matched records agree with each other.

    Returns:
        matched_pairs: list of (gt_index, system_index)
        missing_gt_indices: list of GT indices with no match
        extra_system_indices: list of System indices with no match
    """

    ground_keys = _build_key_map(
        ground_df,
        "Ground Truth",
    )

    system_keys = _build_key_map(
        system_df,
        "System Generated",
    )

    matched_pairs = []
    missing_gt_indices = []

    for key, gt_index in ground_keys.items():

        system_index = system_keys.get(key)

        if system_index is None:
            missing_gt_indices.append(gt_index)
        else:
            matched_pairs.append((gt_index, system_index))

    matched_system_indices = {
        system_index
        for _, system_index in matched_pairs
    }

    extra_system_indices = [
        system_index
        for key, system_index in system_keys.items()
        if system_index not in matched_system_indices
    ]

    return matched_pairs, missing_gt_indices, extra_system_indices
