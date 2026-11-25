"""RAM per stage, on demand: ``MEMORY_PROFILE=true`` in .env (or the container environment).

Off (the default) it does nothing: no thread, no file, no log line. On, every chart the
worker / API runs records, for each stage of the chain and for the chart as a whole:

* the process's RSS before, at its peak and after the stage (sampled every
  ``MEMORY_PROFILE_INTERVAL_MS``, default 50), so spike = peak - before and
  retained = after - before;
* the RSS of child processes (tesseract and the like) at their peak;
* the kernel's own high-water mark for the stage (Linux VmHWM, reset at the start of the
  stage), which catches a spike shorter than one sample -- only when the stage ran alone;
* the container's memory (cgroup) at its peak, and its limit;
* seconds, and how many stages were running in this process at the time (``concurrent``:
  with BATCH_WORKERS > 1 several charts share one process and its RSS, so for clean
  per-stage numbers run with BATCH_WORKERS=1).

Where it goes:

* one log line per stage (``memory <stage>: before ... peak ... after ... spike ...``), so
  it shows in ``docker compose logs`` and on AKS stdout;
* ``<LOG_DIR>/memory_stages.<date>.csv`` (one row per stage per chart) and
  ``<LOG_DIR>/memory_timeline.<date>.csv`` (one row per second: RSS, children, container,
  the stages running) when ``LOG_TO_FILE`` is on. Dated names, so the 7-day log purge
  removes them like any other log.

Linux reads /proc and /sys/fs/cgroup (no extra package). Elsewhere it uses psutil when
installed, else records what ``resource`` gives (the process peak only).
"""
from __future__ import annotations

import csv
import logging
import os
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MB = 1024 * 1024
_TRUE = {"1", "true", "yes", "on"}

STAGE_COLUMNS = (
    "time", "host", "pid", "chart", "pages", "stage", "seconds", "rss_before_mb", "rss_peak_mb",
    "rss_after_mb", "spike_mb", "retained_mb", "hwm_mb", "children_peak_mb", "total_peak_mb",
    "container_before_mb", "container_peak_mb", "container_limit_mb", "concurrent", "error",
)
TIMELINE_COLUMNS = ("time", "host", "pid", "rss_mb", "children_mb", "container_mb", "running")


def enabled() -> bool:
    return (os.environ.get("MEMORY_PROFILE") or "").strip().lower() in _TRUE


def _interval() -> float:
    try:
        return max(5, int(os.environ.get("MEMORY_PROFILE_INTERVAL_MS") or 50)) / 1000
    except ValueError:
        return 0.05


def _mb(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value / MB, 1)


# --------------------------------------------------------------------------- readers

_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_PROC = Path("/proc/self/statm").exists()

try:  # optional, only used off Linux
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None


def _statm_rss(pid: str = "self") -> Optional[int]:
    try:
        with open(f"/proc/{pid}/statm", encoding="ascii") as fh:
            return int(fh.read().split()[1]) * _PAGE
    except (OSError, ValueError, IndexError):
        return None


def _child_pids() -> list[str]:
    """Every descendant of this process (/proc/<pid>/task/<tid>/children, recursively)."""
    found: list[str] = []
    todo = [str(os.getpid())]
    while todo:
        pid = todo.pop()
        try:
            tasks = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tasks:
            try:
                with open(f"/proc/{pid}/task/{tid}/children", encoding="ascii") as fh:
                    kids = fh.read().split()
            except OSError:
                continue
            found.extend(kids)
            todo.extend(kids)
    return found


def process_rss() -> Optional[int]:
    if _PROC:
        return _statm_rss()
    if psutil is not None:
        try:
            return psutil.Process().memory_info().rss
        except Exception:  # noqa: BLE001
            return None
    return None


def children_rss() -> int:
    if _PROC:
        return sum(_statm_rss(pid) or 0 for pid in _child_pids())
    if psutil is not None:
        try:
            return sum(c.memory_info().rss for c in psutil.Process().children(recursive=True))
        except Exception:  # noqa: BLE001
            return 0
    return 0


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path, encoding="ascii") as fh:
            raw = fh.read().strip()
        return None if raw == "max" else int(raw)
    except (OSError, ValueError):
        return None


def container_bytes() -> Optional[int]:
    """The container's memory (cgroup v2, else v1), or None outside one."""
    value = _read_int("/sys/fs/cgroup/memory.current")
    return value if value is not None else _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")


def container_limit() -> Optional[int]:
    value = _read_int("/sys/fs/cgroup/memory.max")
    if value is None:
        value = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    return None if value is None or value >= 1 << 60 else value


def _reset_hwm() -> bool:
    try:
        with open("/proc/self/clear_refs", "w", encoding="ascii") as fh:
            fh.write("5")
        return True
    except OSError:
        return False


def _read_hwm() -> Optional[int]:
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    if not _PROC:
        try:
            import resource
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return peak if sys.platform == "darwin" else peak * 1024
        except Exception:  # noqa: BLE001
            return None
    return None


# --------------------------------------------------------------------------- windows

class Window:
    """One stage (or the whole chart) being measured."""

    def __init__(self, chart: str, stage: str, pages: Any) -> None:
        self.chart, self.stage, self.pages = chart, stage, pages
        self.started = time.monotonic()
        self.rss_before = process_rss()
        self.container_before = container_bytes()
        self.rss_peak = self.rss_before or 0
        self.children_peak = 0
        self.total_peak = self.rss_peak
        self.container_peak = self.container_before or 0
        self.concurrent = 1
        self.alone = True
        self.hwm_reset = False


_LOCK = threading.Lock()
_OPEN: dict[int, Window] = {}
_SAMPLER: Optional[threading.Thread] = None


def _sample_once() -> dict[str, Optional[int]]:
    rss = process_rss()
    kids = children_rss()
    cg = container_bytes()
    with _LOCK:
        windows = list(_OPEN.values())
        for w in windows:
            w.concurrent = max(w.concurrent, len(windows))
            if rss is not None:
                w.rss_peak = max(w.rss_peak, rss)
                w.total_peak = max(w.total_peak, rss + kids)
            w.children_peak = max(w.children_peak, kids)
            if cg is not None:
                w.container_peak = max(w.container_peak, cg)
    return {"rss": rss, "kids": kids, "cg": cg}


def _sampler_loop() -> None:
    interval = _interval()
    next_line = 0.0
    while True:
        values = _sample_once()
        now = time.monotonic()
        if now >= next_line:
            next_line = now + 1.0
            with _LOCK:
                running = ";".join(sorted(f"{w.chart}/{w.stage}" for w in _OPEN.values()
                                          if w.stage != "chart"))
            if running:  # idle seconds are not worth a line
                _append("memory_timeline", TIMELINE_COLUMNS, {
                    "rss_mb": _mb(values["rss"]), "children_mb": _mb(values["kids"]),
                    "container_mb": _mb(values["cg"]), "running": running,
                })
        time.sleep(interval)


def _ensure_sampler() -> None:
    global _SAMPLER
    with _LOCK:
        if _SAMPLER is None or not _SAMPLER.is_alive():
            _SAMPLER = threading.Thread(target=_sampler_loop, name="memory-profile", daemon=True)
            _SAMPLER.start()


def begin(chart: str, stage: str, *, pages: Any = None) -> Optional[Window]:
    """Start measuring; None (and nothing done) when MEMORY_PROFILE is off. Never raises."""
    if not enabled():
        return None
    try:
        _ensure_sampler()
        window = Window(chart, stage, pages)
        with _LOCK:
            if stage != "chart":
                # The kernel high-water mark is process-wide: it counts for a stage only while
                # no other stage runs in this process.
                running = [w for w in _OPEN.values() if w.stage != "chart"]
                for w in running:
                    w.alone = False
                window.alone = not running
                if window.alone:
                    window.hwm_reset = _reset_hwm()
            _OPEN[id(window)] = window
        return window
    except Exception as exc:  # noqa: BLE001 -- measuring must never break a run
        logger.debug("memory profile: begin failed: %s", exc)
        return None


def end(window: Optional[Window], *, error: str = "") -> Optional[dict[str, Any]]:
    """Stop measuring: log the line, append the CSV row, return the row. Never raises."""
    if window is None:
        return None
    try:
        _sample_once()
        with _LOCK:
            _OPEN.pop(id(window), None)
        after = process_rss()
        hwm = _read_hwm() if (window.hwm_reset and window.alone) else None
        peak = max(window.rss_peak, hwm or 0, after or 0)
        before = window.rss_before
        row = {
            "chart": window.chart, "pages": window.pages if window.pages is not None else "",
            "stage": window.stage, "seconds": round(time.monotonic() - window.started, 2),
            "rss_before_mb": _mb(before), "rss_peak_mb": _mb(peak), "rss_after_mb": _mb(after),
            "spike_mb": _mb(peak - before) if before is not None else None,
            "retained_mb": _mb(after - before) if before is not None and after is not None else None,
            "hwm_mb": _mb(hwm), "children_peak_mb": _mb(window.children_peak),
            "total_peak_mb": _mb(max(window.total_peak, peak)),
            "container_before_mb": _mb(window.container_before),
            "container_peak_mb": _mb(window.container_peak or None),
            "container_limit_mb": _mb(container_limit()),
            "concurrent": window.concurrent, "error": (error or "")[:200],
        }
        logger.info(
            "memory %s: before %s MB, peak %s, after %s, spike %s, retained %s, children %s, "
            "container peak %s MB, %ss%s",
            window.stage, row["rss_before_mb"], row["rss_peak_mb"], row["rss_after_mb"],
            row["spike_mb"], row["retained_mb"], row["children_peak_mb"],
            row["container_peak_mb"], row["seconds"],
            "" if window.concurrent <= 1 else f" ({window.concurrent} running in this process)",
        )
        _append("memory_stages", STAGE_COLUMNS, row)
        return row
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory profile: end failed: %s", exc)
        return None


# --------------------------------------------------------------------------- files

_FILE_LOCK = threading.Lock()


def _to_file() -> bool:
    return (os.environ.get("LOG_TO_FILE") or "true").strip().lower() in _TRUE


def _append(stem: str, columns: tuple[str, ...], row: dict[str, Any]) -> None:
    if not _to_file():
        return
    try:
        from common.logging_setup import resolve_log_dir

        folder = resolve_log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{stem}.{date.today().isoformat()}.csv"
        full = {"time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "host": os.environ.get("HOSTNAME") or "", "pid": os.getpid(), **row}
        with _FILE_LOCK:
            new = not path.exists()
            with path.open("a", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
                if new:
                    writer.writeheader()
                writer.writerow(full)
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory profile: could not write %s: %s", stem, exc)
