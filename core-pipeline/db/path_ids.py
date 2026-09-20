"""Infer chart_list.run_id / batch_id from path segments when not passed.

Paths commonly look like::

    Raw_Input/Run1/Batch1/DEID_PNGs
    Batch1/Run1
    run_2/batch_3

Segment forms accepted (case-insensitive): ``Run1``, ``run_1``, ``R1``,
``Batch1``, ``batch_1``, ``B1``. Values are normalized to ``R{n}`` / ``B{n}``
so they match manifest files like ``metadata_R1_B1.csv``.

Also derives the Processed/ write destination from a Raw_Input read path —
see :func:`derive_output_path`.
"""
from __future__ import annotations

import re
from typing import Optional

_RUN_SEG = re.compile(r"(?i)^(?:run[_-]?|r)(\d+)$")
_BATCH_SEG = re.compile(r"(?i)^(?:batch[_-]?|b)(\d+)$")
# Leaf folders under Batch that hold chart dirs (not part of the output path).
_DEID_SEG = re.compile(r"(?i)^deid([_-].+)?$")


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


def derive_output_path(
    blob_read_path: Optional[str],
    chart_name: str,
) -> Optional[str]:
    """Map a Raw_Input read prefix + chart folder to the Processed write path.

    Example::

        Raw_Input/Run1/Batch1/DEID_PNGs  +  52743839_44976074
          →  Processed/Run1/Batch1/52743839_44976074

        Raw_Input/Run1/Batch1/DEID_Images + FolderName
          →  Processed/Run1/Batch1/FolderName

    Rules:
      * Leading ``Raw_Input`` → ``Processed`` (case-insensitive).
      * Keep ``Run*`` / ``Batch*`` segments in order.
      * Drop a trailing ``DEID*`` (or similar) leaf under the batch.
      * Append the chart folder name.
    """
    name = (chart_name or "").strip().strip("/\\")
    if not name:
        return None

    raw = (blob_read_path or "").strip().strip("/\\")
    if not raw:
        return f"Processed/{name}"

    segments = [s for s in re.split(r"[/\\]+", raw) if s]
    if not segments:
        return f"Processed/{name}"

    out: list[str] = []
    i = 0
    if segments[0].casefold() == "raw_input":
        out.append("Processed")
        i = 1
    elif segments[0].casefold() == "processed":
        out.append("Processed")
        i = 1

    while i < len(segments):
        seg = segments[i]
        if _DEID_SEG.fullmatch(seg):
            i += 1
            continue
        if i == len(segments) - 1 and seg == name:
            i += 1
            continue
        out.append(seg)
        i += 1

    if not out:
        out = ["Processed"]
    out.append(name)
    return "/".join(out)


def resolve_output_path(
    chart_name: str,
    *,
    write_path: Optional[str] = None,
    read_path: Optional[str] = None,
) -> Optional[str]:
    """Prefer an explicit write path as-is; otherwise derive from the read path.

    ``write_path`` is the caller-supplied destination prefix (or full chart
    path). It is **not** remapped through Raw_Input→Processed — only normalized
    to forward slashes. The chart folder name is appended when missing.
    """
    name = (chart_name or "").strip().strip("/\\")
    if not name:
        return None

    explicit = (write_path or "").strip().strip("/\\").replace("\\", "/")
    if explicit:
        parts = [s for s in explicit.split("/") if s]
        if parts and parts[-1] == name:
            return "/".join(parts)
        parts.append(name)
        return "/".join(parts)

    return derive_output_path(read_path, name)


def output_write_prefix(output_path: Optional[str]) -> Optional[str]:
    """Parent prefix for write_chart (everything before the chart folder)."""
    path = (output_path or "").strip().strip("/\\")
    if not path:
        return None
    parts = [s for s in re.split(r"[/\\]+", path) if s]
    if len(parts) < 2:
        return None
    return "/".join(parts[:-1])
