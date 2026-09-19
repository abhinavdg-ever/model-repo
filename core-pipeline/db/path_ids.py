"""Infer chart_list.run_id / batch_id from path segments when not passed.

Paths commonly look like::

    Raw_Input/Run1/Batch1/DEID_PNGs
    Batch1/Run1
    run_2/batch_3

Segment forms accepted (case-insensitive): ``Run1``, ``run_1``, ``R1``,
``Batch1``, ``batch_1``, ``B1``. Values are normalized to ``R{n}`` / ``B{n}``
so they match manifest files like ``metadata_R1_B1.csv``.
"""
from __future__ import annotations

import re
from typing import Optional

_RUN_SEG = re.compile(r"(?i)^(?:run[_-]?|r)(\d+)$")
_BATCH_SEG = re.compile(r"(?i)^(?:batch[_-]?|b)(\d+)$")


def infer_run_batch_from_path(*path_parts: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return ``(run_id, batch_id)`` from any path fragments, either order."""
    segments: list[str] = []
    for part in path_parts:
        if not part:
            continue
        for seg in re.split(r"[/\\]+", str(part).strip("/\\")):
            if seg:
                segments.append(seg)

    run_id: Optional[str] = None
    batch_id: Optional[str] = None
    for seg in segments:
        m = _RUN_SEG.fullmatch(seg)
        if m:
            run_id = f"R{int(m.group(1))}"
            continue
        m = _BATCH_SEG.fullmatch(seg)
        if m:
            batch_id = f"B{int(m.group(1))}"
    return run_id, batch_id


def resolve_run_batch(
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    *path_parts: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Explicit values win; otherwise infer from path parts."""
    explicit_run = (run_id or "").strip() or None
    explicit_batch = (batch_id or "").strip() or None
    inferred_run, inferred_batch = infer_run_batch_from_path(*path_parts)
    return explicit_run or inferred_run, explicit_batch or inferred_batch
