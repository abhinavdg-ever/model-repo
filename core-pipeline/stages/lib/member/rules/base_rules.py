from __future__ import annotations


def combine_evidences(
    *,
    name_ok: bool,
    dob_ok: bool,
    id_ok: bool,
    initial_only: bool = False,
) -> str:
    if not name_ok:
        return "Reject"
    if initial_only:
        return "Accept" if dob_ok and id_ok else "Reject"
    return "Accept" if dob_ok or id_ok else "Reject"


def is_present(value: str | None) -> bool:
    text = (value or "").strip()
    return bool(text) and text.upper() != "N/A"
