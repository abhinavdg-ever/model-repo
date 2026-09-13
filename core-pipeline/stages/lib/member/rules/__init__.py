"""Verification rules ported from Reference/V1 Code/Member_Verification/Rules."""
from .base_rules import combine_evidences, is_present
from .name_2_words_rules import verify_two_word_name
from .name_3_words_rules import verify_three_word_name
from .what_if_rules import (
    ACCEPT,
    MAX_WRONG_PAGES,
    PAGE_NOT_VERIFIED,
    PAGE_VERIFIED,
    PAGE_WRONG_MEMBER,
    REJECT,
    WRONG_PAGE_RATIO,
    apply_what_if,
    count_wrong_member,
    page_status,
    reject_threshold,
)
from .wrong_member_rules import wrong_member_on_page

__all__ = [
    "ACCEPT",
    "MAX_WRONG_PAGES",
    "PAGE_NOT_VERIFIED",
    "PAGE_VERIFIED",
    "PAGE_WRONG_MEMBER",
    "REJECT",
    "WRONG_PAGE_RATIO",
    "apply_what_if",
    "combine_evidences",
    "count_wrong_member",
    "is_present",
    "page_status",
    "reject_threshold",
    "verify_three_word_name",
    "verify_two_word_name",
    "wrong_member_on_page",
]
