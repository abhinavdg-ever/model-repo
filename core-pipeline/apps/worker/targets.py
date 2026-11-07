"""Step targets for the /v1 chart APIs (phase 5, docs/specials/PLATFORM_PLAN.md §6).

A target names what a caller wants back (OCR, page types, member verdict, dates). Each one
needs a set of stages, and ``stages_for`` returns them in chain order as ``name:pass``
tokens, the spelling ``only`` takes (``apps.worker.runner.resolve_stage``).

``CHAIN`` is the ``pipeline_stage`` seed in schema/schema.sql (and ``runner.STAGE_NAMES``;
a test holds the three together). Kept here as data so this module imports nothing heavy.
"""
from __future__ import annotations

from typing import Iterable, Optional

CHAIN: tuple[str, ...] = (
    "ocr_quality:1",
    "ocr_prelim:1",
    "blank_junk:1",
    "ocr_final1:1",
    "ocr_final2:1",
    "section_headers:1",
    "kv_extract:1",
    "blank_junk:2",
    "member_verify:1",
    "page_subtype:1",
    "dos_extract:1",
    "continuity:1",
    "encounter_type:1",
    "page_sequencing:1",
    "imaging_final:1",
)

# target -> (targets it builds on, stages of its own)
TARGETS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "ocr": ((), (
        "ocr_quality:1", "ocr_prelim:1", "blank_junk:1",
        "ocr_final1:1", "ocr_final2:1", "section_headers:1",
    )),
    "page-type": (("ocr",), ("kv_extract:1", "blank_junk:2", "page_subtype:1")),
    "member-verify": (("ocr",), ("kv_extract:1", "blank_junk:2", "member_verify:1")),
    "dos": (("page-type",), ("dos_extract:1",)),
    "full": ((), CHAIN),
}


def stages_for(target: str, order: Optional[Iterable[str]] = None) -> list[str]:
    """Every stage ``target`` needs, dependencies included, in chain order.

    ``order`` is the chain as ``name:pass`` tokens (e.g. from ``list_stages``); default
    ``CHAIN``. Raises KeyError naming the known targets.
    """
    if target not in TARGETS:
        raise KeyError(f"unknown target {target!r}; known: {', '.join(TARGETS)}")
    wanted: set[str] = set()
    pending = [target]
    while pending:
        deps, own = TARGETS[pending.pop()]
        wanted.update(own)
        pending.extend(deps)
    chain = list(order) if order is not None else list(CHAIN)
    return [token for token in chain if token in wanted]


def split(token: str) -> tuple[str, int]:
    """``"blank_junk:2"`` -> ``("blank_junk", 2)``."""
    name, _, pass_no = token.partition(":")
    return name, int(pass_no or 1)
