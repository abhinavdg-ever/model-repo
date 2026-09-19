"""Write a finished chart back out — the reverse of run's intake step.

``run`` pulls a chart folder in from blob or local disk into the workspace at
data/folders/<chart>. This takes the same folder back out to a destination that
is, again, either blob or local disk. A chart handed to run from a blob prefix
and then written back to one makes a round trip with the outputs added.

What gets written is the whole chart folder — ``pages/``, ``corrected-pages/``,
``ocr/`` and ``imaging/`` — so the destination is self-contained: the scans as
received, the rotation-corrected copies of the ones that needed it, the OCR
text and the per-stage CSVs, readable without the database.

Nothing here consults the database. The workspace on disk is what the pipeline
produced, and writing it out should not depend on a chart_list row still saying
so.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Optional

from config import chart_dir

logger = logging.getLogger(__name__)

# The chart workspace layout, in the order a reader would want them.
CHART_SUBDIRS = ("pages", "corrected-pages", "ocr", "imaging")

# What a write sends. The default omits `pages/` — the originals came FROM the
# source you are usually writing back to, so re-sending them doubles the
# storage and the transfer for bytes already there. `corrected-pages/` is still
# sent, because those the pipeline produced and the source does not have.
SKIP_ORIG_PAGES = "skip_orig_pages"
ALL_FILES = "all_files"
WRITE_MODES = (SKIP_ORIG_PAGES, ALL_FILES)


def subdirs_for(write_mode: str) -> tuple[str, ...]:
    if write_mode == ALL_FILES:
        return CHART_SUBDIRS
    if write_mode != SKIP_ORIG_PAGES:
        raise ValueError(
            f"unknown write_mode {write_mode!r} — use one of {', '.join(WRITE_MODES)}"
        )
    return tuple(d for d in CHART_SUBDIRS if d != "pages")


def _files_to_write(root: Path, write_mode: str = SKIP_ORIG_PAGES) -> list[Path]:
    """Every file under the chart folder, excluding junk that is not content.

    macOS AppleDouble stubs (``._1.jpg``) are resource forks, not files anyone
    wants at the destination — the same exclusion intake applies on the way in.
    """
    out: list[Path] = []
    for sub in subdirs_for(write_mode):
        folder = root / sub
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            if path.name.startswith("._") or path.name == ".DS_Store":
                continue
            out.append(path)
    return out


def write_chart(
    chart_name: str,
    *,
    local_path: Optional[str] = None,
    blob_container: Optional[str] = None,
    blob_path: Optional[str] = None,
    overwrite: bool = False,
    write_mode: str = SKIP_ORIG_PAGES,
) -> dict[str, Any]:
    """Copy data/folders/<chart_name> to a local directory or a blob prefix.

    Exactly one destination: ``local_path``, or ``blob_container`` +
    ``blob_path``. The chart's own name is appended to it, so writing two
    charts to one destination does not merge them.

    ``overwrite`` guards the destination. Without it, a destination that
    already holds files is an error rather than a silent merge of two runs.
    """
    if bool(local_path) == bool(blob_path):
        raise ValueError(
            "Provide exactly one destination: local_path, or blob_container + blob_path"
        )

    root = chart_dir(chart_name)
    if not root.is_dir():
        raise RuntimeError(f"No chart workspace at {root} — has '{chart_name}' been run?")

    files = _files_to_write(root, write_mode)
    if not files:
        sent = ", ".join(subdirs_for(write_mode))
        raise RuntimeError(
            f"{root} holds nothing to write under {sent}"
            + (
                " — the chart may have produced no output yet, or every page "
                "was already upright so corrected-pages/ is empty; "
                "write_mode=all_files would send the originals"
                if write_mode == SKIP_ORIG_PAGES
                else ""
            )
        )

    if local_path:
        return _write_local(chart_name, root, files, Path(local_path), overwrite, write_mode)
    return _write_blob(chart_name, root, files, blob_container, blob_path, overwrite, write_mode)


def _write_local(
    chart_name: str,
    root: Path,
    files: list[Path],
    dest_root: Path,
    overwrite: bool,
    write_mode: str = SKIP_ORIG_PAGES,
) -> dict[str, Any]:
    dest = (dest_root.expanduser().resolve()) / chart_name
    if dest == root.resolve():
        raise RuntimeError(
            f"Destination is the chart workspace itself ({dest}) — writing it "
            "onto itself would do nothing and risk truncating the source"
        )

    existing = [p for p in dest.rglob("*") if p.is_file()] if dest.is_dir() else []
    if existing and not overwrite:
        raise RuntimeError(
            f"{dest} already holds {len(existing)} file(s). "
            "Pass overwrite=true to replace them."
        )

    written = 0
    total_bytes = 0
    for path in files:
        target = dest / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        written += 1
        total_bytes += path.stat().st_size

    logger.info(
        "Wrote chart %s: %d file(s), %.1f MB -> %s",
        chart_name, written, total_bytes / 1_048_576, dest,
    )
    return {
        "chart_name": chart_name,
        "mode": "local",
        "destination": str(dest),
        "write_mode": write_mode,
        "files_written": written,
        "bytes_written": total_bytes,
    }


def _write_blob(
    chart_name: str,
    root: Path,
    files: list[Path],
    container: Optional[str],
    blob_path: Optional[str],
    overwrite: bool,
    write_mode: str = SKIP_ORIG_PAGES,
) -> dict[str, Any]:
    from azure_retry import call_with_retry
    from config import AZURE_RETRY_ATTEMPTS, AZURE_RETRY_BASE_DELAY, AZURE_RETRY_MAX_DELAY
    from db.blob_store import (
        ensure_blob_ready,
        get_container_client,
        normalize_prefix,
        upload_blob,
    )

    ensure_blob_ready(container)
    prefix = f"{normalize_prefix(blob_path)}{chart_name}/"
    client = get_container_client(container)

    if not overwrite:
        # One listing, not one existence check per file: a 400-page chart would
        # otherwise cost 400 round trips before writing anything.
        def _peek():
            return next(iter(client.list_blobs(name_starts_with=prefix)), None)

        clash = call_with_retry(
            _peek,
            attempts=AZURE_RETRY_ATTEMPTS,
            base_delay=AZURE_RETRY_BASE_DELAY,
            max_delay=AZURE_RETRY_MAX_DELAY,
            label=f"blob.list:{container}/{prefix}",
        )
        if clash is not None:
            raise RuntimeError(
                f"{container}/{prefix} already holds blobs (e.g. {clash.name}). "
                "Pass overwrite=true to replace them."
            )

    written = 0
    total_bytes = 0
    for path in files:
        name = f"{prefix}{path.relative_to(root).as_posix()}"
        with path.open("rb") as handle:
            upload_blob(container or "", name, handle, overwrite=True)
        written += 1
        total_bytes += path.stat().st_size

    logger.info(
        "Wrote chart %s: %d file(s), %.1f MB -> %s/%s",
        chart_name, written, total_bytes / 1_048_576, container, prefix,
    )
    return {
        "chart_name": chart_name,
        "mode": "blob",
        "destination": f"{container}/{prefix}",
        "write_mode": write_mode,
        "files_written": written,
        "bytes_written": total_bytes,
    }
