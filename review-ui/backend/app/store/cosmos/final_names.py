"""imaging_final in Cosmos uses the ground truth's field names (2026-11-14, user).

Inside a Cosmos page document, ``imaging_final`` carries the ground truth's names where the two
mean the same thing; Postgres keeps its own column names. core-pipeline writes them
(``common/db/cosmos_store.FINAL_TO_COSMOS``, the same table; a test keeps both equal). review-ui
reads a page doc's imaging_final back to the Postgres names (``from_cosmos``), so the rest of the
code is unchanged; a document written before the rename (old names) reads as it is.

Page type and encounter type keep imaging_final's names: the ground truth stores the page type
in ``encounter_type`` and the encounter type in ``encounter_label``.
"""
from __future__ import annotations

from typing import Any

FINAL_TO_COSMOS = {
    "handwritten_area_pct": "handwritten_pct",
    "quality_tag": "quality",
    "orientation_angle": "rotation",
    "tilt_angle": "tilt",
    "specialty": "provider_specialty",
    "codability": "codeable",
    "provider_name": "rendering_provider",
    "signature_present": "provider_signature",
    "seq": "page_sequence",
}
COSMOS_TO_FINAL = {cosmos: pg for pg, cosmos in FINAL_TO_COSMOS.items()}


def from_cosmos(row: Any) -> Any:
    """A page doc's imaging_final with the Postgres names; old documents pass through."""
    if not isinstance(row, dict):
        return row
    return {COSMOS_TO_FINAL.get(k, k) if k not in FINAL_TO_COSMOS else k: v for k, v in row.items()}
