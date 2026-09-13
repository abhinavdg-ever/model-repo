from __future__ import annotations

import re


def extract_member_id(ocr_text: str, member_id: str) -> str:
    """Return MemberID if the exact value appears on the page, else N/A."""
    value = (member_id or "").strip()
    if not value or value.upper() == "N/A":
        return "N/A"
    pattern = r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])"
    if re.search(pattern, ocr_text or "", flags=re.IGNORECASE):
        return value
    return "N/A"
