from __future__ import annotations

import math

PAGE_VERIFIED = "Verified"
PAGE_WRONG_MEMBER = "Wrong_Member"
PAGE_NOT_VERIFIED = "Not_Verified"

ACCEPT = "Accept"
REJECT = "Reject"

MAX_WRONG_PAGES = 5
WRONG_PAGE_RATIO = 0.10


def page_status(*, verified: bool, wrong_member: bool) -> str:
    """Bucket one page from its verification result and name evidence."""
    if verified:
        return PAGE_VERIFIED
    if wrong_member:
        return PAGE_WRONG_MEMBER
    return PAGE_NOT_VERIFIED


def reject_threshold(total_pages: int) -> int:
    """Wrong-member page count that rejects the document.

    5 pages or 10% of the pages, whichever comes first (at least 1 page).
    """
    if total_pages <= 0:
        return 1
    ratio_pages = math.ceil(total_pages * WRONG_PAGE_RATIO)
    return max(1, min(MAX_WRONG_PAGES, ratio_pages))


def count_wrong_member(page_statuses: list[str]) -> int:
    return sum(1 for status in page_statuses if status == PAGE_WRONG_MEMBER)


def apply_what_if(page_statuses: list[str], total_pages: int) -> str:
    """Return the document status (Accept / Reject) for the page buckets."""
    if not page_statuses:
        return REJECT
    total = total_pages if total_pages > 0 else len(page_statuses)
    wrong = count_wrong_member(page_statuses)
    if wrong >= reject_threshold(total):
        return REJECT
    return ACCEPT
