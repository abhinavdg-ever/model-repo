"""core-pipeline/outputs/ is scratch space (2026-11-07, user).

Chart working folders live under ``DATA_ROOT`` (default ``core-pipeline/outputs/charts``).
They are not deleted after every run yet: re-running only some stages
(``only: ["dos_extract"]``) still reads the staging files and OCR a chart folder holds.
Until those move into the database, a chart folder nobody has run for
``OUTPUTS_RETENTION_DAYS`` days (default 7) is deleted. The check runs on the first trigger
of each day, with the log purge (orchestrator/run_stats.new_trigger).

Safety: only a ``DATA_ROOT`` inside ``core-pipeline/outputs/`` is ever purged. A
``DATA_ROOT`` pointed elsewhere (an old .env still naming ``review-ui/data/folders``) is
left alone unless ``OUTPUTS_PURGE_ANYWHERE=true``.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from common.config import CORE_ROOT, DATA_ROOT

logger = logging.getLogger(__name__)

LAST_RUN_FILE = ".last_run"
PURGE_MARK = ".last_purge"
_LOCK = threading.Lock()


def retention_days() -> int:
    try:
        return max(1, int(os.environ.get("OUTPUTS_RETENTION_DAYS") or "7"))
    except ValueError:
        return 7


def mark_chart_used(chart_name: str, root: Path | None = None) -> None:
    """Stamp the chart folder at the start of a run, so the purge counts days since the last
    run rather than since a file happened to change."""
    folder = (root or DATA_ROOT) / chart_name
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / LAST_RUN_FILE).write_text(datetime.now().astimezone().isoformat() + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("could not stamp %s: %s", folder, exc)


def _last_used(folder: Path) -> date:
    stamp = folder / LAST_RUN_FILE
    paths = [stamp] if stamp.is_file() else [folder, *(p for p in folder.iterdir() if p.is_dir())]
    return date.fromtimestamp(max(p.stat().st_mtime for p in paths))


def _purge_allowed(root: Path) -> bool:
    if (os.environ.get("OUTPUTS_PURGE_ANYWHERE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    try:
        root.resolve().relative_to((CORE_ROOT / "outputs").resolve())
        return True
    except ValueError:
        return False


def purge_old_charts(days: int | None = None, *, root: Path | None = None,
                     today: date | None = None, force: bool = False) -> list[Path]:
    """Delete chart folders under ``root`` (DATA_ROOT) not run for ``days`` days. Once a day
    (``root/.last_purge``) unless ``force``. Never raises."""
    root = root or DATA_ROOT
    days = retention_days() if days is None else days
    today = today or date.today()
    cutoff = today - timedelta(days=max(1, days))
    removed: list[Path] = []
    with _LOCK:
        if not root.is_dir():
            return removed
        if not _purge_allowed(root):
            logger.info("outputs purge skipped: DATA_ROOT %s is outside core-pipeline/outputs", root)
            return removed
        mark = root / PURGE_MARK
        try:
            if not force and mark.read_text(encoding="utf-8").strip() == today.isoformat():
                return removed
        except OSError:
            pass
        for folder in root.iterdir():
            if not folder.is_dir() or folder.name.startswith((".", "_")):
                continue
            try:
                if _last_used(folder) < cutoff:
                    shutil.rmtree(folder)
                    removed.append(folder)
            except OSError as exc:
                logger.warning("outputs purge: %s not removed: %s", folder.name, exc)
        try:
            mark.write_text(today.isoformat() + "\n", encoding="utf-8")
        except OSError:
            pass
    if removed:
        logger.info("Outputs purge: removed %d chart folder(s) not run for %d days", len(removed), days)
    return removed
