#!/usr/bin/env python3
"""RAM per stage for one or more charts, run through the real stage chain in this process.

What it does
------------
Runs each chart exactly as a queue worker does (``apps.worker.chart_run.execute_run`` with a
``RunRequest`` built from the same fields POST /api/charts/run takes), after wrapping, at
runtime and without editing any pipeline file:

* every callable in ``apps.worker.runner.STAGE_CHAIN`` (the 15 stages, stage_name + pass_no),
* the pieces around them: intake (``runner.run_download`` / ``download_blob.import_local_folder``),
  page hydration (``download_blob.ensure_chart_images``) and the write-out
  (``chart_run.write_after_run``).

A background thread samples memory every ``--interval`` seconds (default 0.05): RSS of this
process, RSS of its child processes, thread count, and the container's cgroup memory when
readable. Per stage it records RSS before / after / peak, delta (after - before), spike
(peak - before), the kernel's own high-water mark for the stage (Linux, VmHWM reset through
/proc/self/clear_refs, so even a spike shorter than one sample is caught), wall seconds, gc
collections, and optionally tracemalloc's top allocators.

THIS RUNS THE CHART FOR REAL: it writes that chart's rows in the database (Postgres or Cosmos,
whatever DB_BACKEND says), replacing the previous run's results, exactly as a worker run would.
With --output-path it also writes the chart out to Blob. Without --skip-ocr (or with --skip-ocr
on a chart that has no OCR to reuse) stage 5 sends pages to Azure Document Intelligence, which
is billed per page; --no-docintel turns stage 5 off for this process instead.

Outputs in --out:  <run-id>_stages.csv   one row per stage call (and per intake / write-out)
                   <run-id>_samples.csv  every sample, with the chart and stage running then
                   <run-id>_summary.json the arguments, environment, per-chart totals, cold vs warm

    python stage_memory.py --chart CHART_SMALL --input-path Raw_Input/Run1/Batch1/DEID_PNGs --skip-ocr
    python stage_memory.py --self-test          # fake stages, no pipeline / DB: checks the tooling

See README.md in this folder for the Docker / VM commands.
"""
from __future__ import annotations

import argparse
import csv
import functools
import gc
import json
import os
import platform
import socket
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
# `run_scenarios.sh setup` installs psutil (and matplotlib) here, so the image needs no rebuild.
# Appended, so whatever the image already has wins.
if (HERE / ".pydeps").is_dir():
    sys.path.append(str(HERE / ".pydeps"))

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - the message is the point
    sys.stderr.write(
        "stage_memory.py needs psutil, which is not in core-pipeline's requirements.\n"
        "  inside the container / venv:   pip install psutil\n"
        "  or once, from core-pipeline/:  ../utilities/memory_profile/run_scenarios.sh setup\n"
        "     (installs it into utilities/memory_profile/.pydeps, which this script picks up)\n"
    )
    sys.exit(2)

MB = 1024 * 1024

# Environment worth keeping with the numbers. Values only for these names; nothing secret.
ENV_KEYS = (
    "DB_BACKEND", "STAGE_WORKERS", "BATCH_WORKERS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS", "TORCH_NUM_THREADS", "AZURE_DI_MAX_CONCURRENT", "BLANK_JUNK_LLM",
    "MEMBER_NER_ENABLED", "MEMBER_NER_MODEL_ID", "CONTINUITY_MODE", "SEQUENCING_MODE",
    "SKIP_OCR", "RUN_MODE", "DB_POOL_MAX", "PYTHONMALLOC", "MALLOC_ARENA_MAX",
)

STAGE_FIELDS = (
    "run_id", "chart", "iteration", "chart_seq", "kind", "stage_key", "stage_name", "pass_no",
    "started_at", "wall_s", "rss_before_mb", "rss_after_mb", "delta_mb", "peak_rss_mb", "spike_mb",
    "hwm_mb", "children_peak_mb", "total_peak_mb", "cgroup_before_mb", "cgroup_after_mb",
    "cgroup_peak_mb", "cgroup_ws_peak_mb", "threads_max", "samples", "gc_collections", "tracemalloc_peak_mb", "error",
)
SAMPLE_FIELDS = (
    "run_id", "ts", "t_s", "chart", "stage_key", "rss_mb", "children_rss_mb", "total_rss_mb",
    "cgroup_mb", "cgroup_ws_mb", "threads", "children",
)


# --------------------------------------------------------------------------- memory readers

def _read_int(path: str) -> Optional[int]:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read().strip()
        return None if raw in ("", "max") else int(raw)
    except (OSError, ValueError):
        return None


class Cgroup:
    """This container's memory as the kernel accounts it (v2 first, then v1). None off Linux."""

    V2 = "/sys/fs/cgroup/memory.current"
    V1 = "/sys/fs/cgroup/memory/memory.usage_in_bytes"

    def __init__(self) -> None:
        self.current_path = next((p for p in (self.V2, self.V1) if os.path.exists(p)), None)
        self.version = 2 if self.current_path == self.V2 else (1 if self.current_path else None)

    def current(self) -> Optional[int]:
        return _read_int(self.current_path) if self.current_path else None

    def working_set(self, current: Optional[int]) -> Optional[int]:
        """current - inactive_file: what kubectl top / the kubelet call the working set."""
        if current is None or not self.version:
            return None
        stat = "/sys/fs/cgroup/memory.stat" if self.version == 2 else "/sys/fs/cgroup/memory/memory.stat"
        key = "inactive_file" if self.version == 2 else "total_inactive_file"
        try:
            with open(stat, encoding="utf-8") as fh:
                for line in fh:
                    name, _, value = line.partition(" ")
                    if name == key:
                        return max(0, current - int(value))
        except (OSError, ValueError):
            pass
        return current

    def peak(self) -> Optional[int]:
        if self.version == 2:
            return _read_int("/sys/fs/cgroup/memory.peak")  # kernel 5.19+
        if self.version == 1:
            return _read_int("/sys/fs/cgroup/memory/memory.max_usage_in_bytes")
        return None

    def limit(self) -> Optional[int]:
        if self.version == 2:
            return _read_int("/sys/fs/cgroup/memory.max")
        if self.version == 1:
            value = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
            return None if value and value > 1 << 60 else value
        return None


class HighWater:
    """The kernel's per-process RSS high-water mark (VmHWM), resettable on Linux >= 4.0."""

    def __init__(self) -> None:
        self.supported = self.reset()

    @staticmethod
    def reset() -> bool:
        try:
            with open("/proc/self/clear_refs", "w", encoding="utf-8") as fh:
                fh.write("5")
            return True
        except OSError:
            return False

    @staticmethod
    def read() -> Optional[int]:
        try:
            with open("/proc/self/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmHWM:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        return None


def _mb(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value / MB, 1)


# --------------------------------------------------------------------------- sampler

class Sampler(threading.Thread):
    """Samples memory every `interval` s; tracks the peak of the current window."""

    CHILD_REFRESH_S = 1.0

    def __init__(self, run_id: str, interval: float, samples_path: Path) -> None:
        super().__init__(name="memprof-sampler", daemon=True)
        self.run_id = run_id
        self.interval = interval
        self.proc = psutil.Process()
        self.cgroup = Cgroup()
        self.t0 = time.monotonic()
        self.chart = ""
        self.label = "startup"
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._children: list[Any] = []
        self._children_at = 0.0
        self._fh = open(samples_path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=SAMPLE_FIELDS)
        self._writer.writeheader()
        self._last_flush = time.monotonic()
        self.reset_window()

    # -- one sample
    def _child_list(self) -> list[Any]:
        now = time.monotonic()
        if now - self._children_at >= self.CHILD_REFRESH_S:
            try:
                self._children = self.proc.children(recursive=True)
            except psutil.Error:
                self._children = []
            self._children_at = now
        return self._children

    def sample(self) -> dict[str, Any]:
        rss = self.proc.memory_info().rss
        child_rss = 0
        alive = 0
        for child in self._child_list():
            try:
                child_rss += child.memory_info().rss
                alive += 1
            except psutil.Error:
                pass
        try:
            threads = self.proc.num_threads()
        except psutil.Error:
            threads = 0
        cg = self.cgroup.current()
        ws = self.cgroup.working_set(cg)
        with self._lock:
            w = self.window
            w["n"] += 1
            w["rss_peak"] = max(w["rss_peak"], rss)
            w["child_peak"] = max(w["child_peak"], child_rss)
            w["total_peak"] = max(w["total_peak"], rss + child_rss)
            w["threads_max"] = max(w["threads_max"], threads)
            if cg is not None:
                w["cg_peak"] = max(w["cg_peak"] or 0, cg)
            if ws is not None:
                w["ws_peak"] = max(w["ws_peak"] or 0, ws)
            chart, label = self.chart, self.label
        row = {
            "run_id": self.run_id,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "t_s": round(time.monotonic() - self.t0, 3),
            "chart": chart, "stage_key": label,
            "rss_mb": _mb(rss), "children_rss_mb": _mb(child_rss),
            "total_rss_mb": _mb(rss + child_rss), "cgroup_mb": _mb(cg), "cgroup_ws_mb": _mb(ws),
            "threads": threads, "children": alive,
        }
        with self._lock:
            self._writer.writerow(row)
            # Flushed often: an OOM kill (SIGKILL) must still leave the curve up to the kill.
            if time.monotonic() - self._last_flush >= 1.0:
                self._fh.flush()
                self._last_flush = time.monotonic()
        return {"rss": rss, "child_rss": child_rss, "cgroup": cg, "threads": threads}

    def run(self) -> None:
        while not self._halt.wait(self.interval):
            try:
                self.sample()
            except Exception:  # noqa: BLE001 - the sampler must never die mid-chart
                pass

    # -- windows (one per stage)
    def reset_window(self) -> None:
        with self._lock:
            self.window = {"n": 0, "rss_peak": 0, "child_peak": 0, "total_peak": 0,
                           "threads_max": 0, "cg_peak": None, "ws_peak": None}

    def take_window(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.window)

    def set_label(self, chart: Optional[str] = None, label: Optional[str] = None) -> None:
        with self._lock:
            if chart is not None:
                self.chart = chart
            if label is not None:
                self.label = label

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=5)
        with self._lock:
            self._fh.flush()
            self._fh.close()


# --------------------------------------------------------------------------- the profiler

class Profiler:
    """Wraps stage callables; one CSV row per call."""

    def __init__(self, run_id: str, out_dir: Path, interval: float, use_tracemalloc: bool,
                 trace_top: int = 15) -> None:
        self.run_id = run_id
        self.out_dir = out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        self.sampler = Sampler(run_id, interval, out_dir / f"{run_id}_samples.csv")
        self.hwm = HighWater()
        self.use_tracemalloc = use_tracemalloc
        self.trace_top = trace_top
        self.records: list[dict[str, Any]] = []
        self.trace_tops: dict[str, list[dict[str, Any]]] = {}
        self.chart = ""
        self.iteration = 0
        self.chart_seq = 0
        self._depth = 0
        self._fh = open(out_dir / f"{run_id}_stages.csv", "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=STAGE_FIELDS)
        self._writer.writeheader()
        self._fh.flush()
        if use_tracemalloc:
            import tracemalloc

            tracemalloc.start(10)

    def start(self) -> None:
        self.sampler.start()

    def close(self) -> None:
        self.sampler.stop()
        self._fh.close()

    def set_chart(self, chart: str, iteration: int, chart_seq: int) -> None:
        self.chart, self.iteration, self.chart_seq = chart, iteration, chart_seq
        self.sampler.set_label(chart=chart, label="between")

    # -- the measurement around one call
    def measure(self, kind: str, stage_name: str, pass_no: Optional[int], fn: Callable[..., Any],
                *args: Any, **kwargs: Any) -> Any:
        if self._depth:  # nested (e.g. a stage that calls another wrapped piece): no second row
            return fn(*args, **kwargs)
        key = f"{stage_name}:{pass_no}" if pass_no is not None else stage_name
        tm = None
        before_snap = None
        if self.use_tracemalloc:
            import tracemalloc as tm

            tm.reset_peak()
            before_snap = tm.take_snapshot()
        gc_before = [s["collections"] for s in gc.get_stats()]
        hwm_ok = self.hwm.reset()
        self.sampler.reset_window()
        self.sampler.set_label(label=key)
        before = self.sampler.sample()
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        t0 = time.monotonic()
        error = ""
        self._depth += 1
        try:
            return fn(*args, **kwargs)
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            self._depth -= 1
            wall = time.monotonic() - t0
            after = self.sampler.sample()
            win = self.sampler.take_window()
            self.sampler.set_label(label="between")
            hwm = self.hwm.read() if hwm_ok else None
            gc_after = [s["collections"] for s in gc.get_stats()]
            peak = max(win["rss_peak"], hwm or 0)
            cg_peak = win["cg_peak"]
            row = {
                "run_id": self.run_id, "chart": self.chart, "iteration": self.iteration,
                "chart_seq": self.chart_seq, "kind": kind, "stage_key": key,
                "stage_name": stage_name, "pass_no": "" if pass_no is None else pass_no,
                "started_at": started_at, "wall_s": round(wall, 2),
                "rss_before_mb": _mb(before["rss"]), "rss_after_mb": _mb(after["rss"]),
                "delta_mb": _mb(after["rss"] - before["rss"]),
                "peak_rss_mb": _mb(peak), "spike_mb": _mb(peak - before["rss"]),
                "hwm_mb": _mb(hwm), "children_peak_mb": _mb(win["child_peak"]),
                "total_peak_mb": _mb(max(win["total_peak"], peak)),
                "cgroup_before_mb": _mb(before["cgroup"]), "cgroup_after_mb": _mb(after["cgroup"]),
                "cgroup_peak_mb": _mb(cg_peak), "cgroup_ws_peak_mb": _mb(win["ws_peak"]), "threads_max": win["threads_max"],
                "samples": win["n"],
                "gc_collections": "/".join(str(a - b) for a, b in zip(gc_after, gc_before)),
                "tracemalloc_peak_mb": "", "error": error,
            }
            if tm is not None and before_snap is not None:
                row["tracemalloc_peak_mb"] = _mb(tm.get_traced_memory()[1])
                self._trace_top(f"{self.chart}#{self.iteration}:{key}", before_snap, tm.take_snapshot())
            self.records.append(row)
            self._writer.writerow(row)
            self._fh.flush()

    def _trace_top(self, label: str, before: Any, after: Any) -> None:
        stats = after.compare_to(before, "lineno")[: self.trace_top]
        self.trace_tops[label] = [
            {"where": str(s.traceback[0]) if s.traceback else "?",
             "size_diff_mb": round(s.size_diff / MB, 2), "size_mb": round(s.size / MB, 2),
             "count_diff": s.count_diff}
            for s in stats
        ]

    def wrap(self, kind: str, stage_name: str, pass_no: Optional[int],
             fn: Callable[..., Any]) -> Callable[..., Any]:
        if getattr(fn, "__memprof__", False):
            return fn

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return self.measure(kind, stage_name, pass_no, fn, *args, **kwargs)

        wrapper.__memprof__ = True  # type: ignore[attr-defined]
        return wrapper

    # -- hooks into the pipeline
    def wrap_stage_chain(self, runner: Any) -> list[str]:
        """Replace each STAGE_CHAIN entry in place. runner.run_pipeline_for_chart iterates the
        module-level list (or a slice of it) at call time, so the wrapped callables are what
        it calls; the list object stays the same for anything holding a reference to it."""
        chain = runner.STAGE_CHAIN
        for i, (name, pass_no, fn) in enumerate(list(chain)):
            chain[i] = (name, pass_no, self.wrap("stage", name, pass_no, fn))
        return [f"{n}:{p}" for n, p, _ in chain]

    def wrap_attr(self, module: Any, attr: str, label: str) -> bool:
        fn = getattr(module, attr, None)
        if not callable(fn):
            return False
        setattr(module, attr, self.wrap("io", label, None, fn))
        return True


# --------------------------------------------------------------------------- pipeline setup

def find_core_pipeline(explicit: Optional[str]) -> Path:
    candidates = [explicit, os.environ.get("CORE_PIPELINE_DIR"),
                  str(HERE.parents[1] / "core-pipeline"), "/app/core-pipeline", os.getcwd()]
    for raw in candidates:
        if raw and (Path(raw) / "apps" / "worker" / "runner.py").is_file():
            return Path(raw).resolve()
    sys.exit("core-pipeline not found: pass --core-pipeline DIR (the folder holding apps/worker/runner.py)")


def prepare_environment(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    """What queue_worker.main does before running a chart, minus the Service Bus."""
    os.chdir(root)  # .env paths (models/...) are relative to core-pipeline/, as in the image
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env", override=True)  # as the worker does; absent in the image
    except ImportError:
        pass
    # The user's standing rule: no LLM in these runs, whatever .env says.
    os.environ["BLANK_JUNK_LLM"] = "false"
    if args.no_docintel:
        os.environ.pop("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", None)
        os.environ.pop("AZURE_DOCUMENT_INTELLIGENCE_KEY", None)
    for item in args.env or []:
        name, _, value = item.partition("=")
        os.environ[name.strip()] = value

    import logging

    from common.logging_setup import configure_logging, set_process_tag, set_worker_name

    set_process_tag(f"memprof-{os.getpid()}")
    configure_logging(logging.WARNING if args.quiet else logging.INFO)
    set_worker_name("memprof")
    resources: dict[str, Any] = {}
    try:  # phase 8: models / canon from Blob, as the worker does before loading anything
        from common.resources import ensure_resources

        resources = ensure_resources() or {}
    except Exception as exc:  # noqa: BLE001
        resources = {"error": str(exc)[:300]}
    return {"resources_done": bool(resources.get("done")), "resources_error": resources.get("error")}


def hook_pipeline(prof: Profiler) -> dict[str, Any]:
    from apps.worker import chart_run, runner
    from apps.worker.stages.utilities import download_blob

    hooked = {"stages": prof.wrap_stage_chain(runner), "io": []}
    for module, attr, label in (
        (runner, "run_download", "intake_blob"),            # runner.ingest_and_run, blob input
        (download_blob, "import_local_folder", "intake_local"),  # local input (imported per call)
        (download_blob, "ensure_chart_images", "ensure_images"),  # run_pipeline_for_chart, first step
        (chart_run, "write_after_run", "write_out"),         # only with --output-path
    ):
        if prof.wrap_attr(module, attr, label):
            hooked["io"].append(label)
    return hooked


def capture_page_counts(store: dict[str, Any]) -> None:
    """Keep each chart's page count from run_pipeline_for_chart's result (execute_run drops it)."""
    from apps.worker import runner

    original = runner.run_pipeline_for_chart
    if getattr(original, "__memprof__", False):
        return

    @functools.wraps(original)
    def wrapper(chart_id: int, **kwargs: Any) -> Any:
        result = original(chart_id, **kwargs)
        try:
            store[result.get("chart_name")] = {
                "chart_id": chart_id,
                "pages": (result.get("images") or {}).get("page_count"),
                "status": result.get("status"),
                "skipped_stages": result.get("skipped_stages"),
                "stage_errors": result.get("stage_errors"),
            }
        except Exception:  # noqa: BLE001
            pass
        return result

    wrapper.__memprof__ = True  # type: ignore[attr-defined]
    runner.run_pipeline_for_chart = wrapper


def build_payload(args: argparse.Namespace, chart: str) -> Any:
    from apps.orchestrator_api.schemas import RunRequest

    body: dict[str, Any] = {
        "input_type": args.input_type,
        "input_path": args.input_path,
        "chart_name": chart,
        "skip_ocr": bool(args.skip_ocr),
    }
    if args.input_type == "blob":
        body["container_name"] = args.container or os.environ.get("AZURE_STORAGE_CONTAINER") or "imaging-pipeline"
    if args.output_path:
        body["output_path"] = args.output_path
    if args.only:
        body["only"] = args.only
    if args.run_through:
        body["run_through"] = args.run_through
    if args.redownload_pages:
        body["skip_page_download"] = False
    return RunRequest.model_validate(body)


def malloc_trim() -> bool:
    try:
        import ctypes

        return bool(ctypes.CDLL("libc.so.6").malloc_trim(0))
    except Exception:  # noqa: BLE001 - not glibc
        return False


# --------------------------------------------------------------------------- reporting

def chart_totals(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[tuple, dict[str, Any]] = {}
    for r in records:
        k = (r["chart"], r["iteration"], r["chart_seq"])
        t = out.setdefault(k, {"chart": r["chart"], "iteration": r["iteration"], "chart_seq": r["chart_seq"],
                               "wall_s": 0.0, "peak_rss_mb": 0.0, "max_spike_mb": 0.0,
                               "max_spike_stage": "", "rss_start_mb": r["rss_before_mb"]})
        t["wall_s"] = round(t["wall_s"] + (r["wall_s"] or 0), 1)
        t["peak_rss_mb"] = max(t["peak_rss_mb"], r["peak_rss_mb"] or 0)
        if (r["spike_mb"] or 0) > t["max_spike_mb"]:
            t["max_spike_mb"], t["max_spike_stage"] = r["spike_mb"], r["stage_key"]
        t["rss_end_mb"] = r["rss_after_mb"]
    return list(out.values())


def cold_vs_warm(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per chart that ran more than once: each stage's first run vs its later runs."""
    rows = []
    by_chart: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for r in records:
        by_chart.setdefault(r["chart"], {}).setdefault(r["stage_key"], []).append(r)
    for chart, stages in by_chart.items():
        for key, runs in stages.items():
            if len(runs) < 2:
                continue
            first, later = runs[0], runs[1:]
            rows.append({
                "chart": chart, "stage_key": key,
                "cold_spike_mb": first["spike_mb"], "warm_spike_mb": max(x["spike_mb"] or 0 for x in later),
                "cold_delta_mb": first["delta_mb"], "warm_delta_mb": max(x["delta_mb"] or 0 for x in later),
                "cold_wall_s": first["wall_s"], "warm_wall_s": round(sum(x["wall_s"] for x in later) / len(later), 2),
            })
    return rows


def print_table(records: list[dict[str, Any]], charts: list[dict[str, Any]]) -> None:
    head = f"{'chart':<24} {'it':>2} {'stage':<18} {'wall_s':>8} {'before':>8} {'peak':>8} {'after':>8} {'delta':>8} {'spike':>8} {'hwm':>8} {'cgroup':>8}"
    print("\nRAM per stage (MB; spike = peak - before, delta = after - before; cgroup = container peak)")
    print(head)
    print("-" * len(head))
    for r in records:
        print(f"{r['chart'][:24]:<24} {r['iteration']:>2} {r['stage_key'][:18]:<18} {r['wall_s']:>8} "
              f"{r['rss_before_mb']:>8} {r['peak_rss_mb']:>8} {r['rss_after_mb']:>8} {r['delta_mb']:>8} "
              f"{r['spike_mb']:>8} {str(r['hwm_mb'] or '-'):>8} {str(r['cgroup_peak_mb'] or '-'):>8}"
              + (f"  ERROR {r['error'][:60]}" if r["error"] else ""))
    if charts:
        print("\nPer chart run")
        for c in charts:
            print(f"  {c['chart']} (iteration {c['iteration']}, #{c['chart_seq']} in this process): "
                  f"{c.get('pages') or '?'} pages, {c['wall_s']} s, RSS {c['rss_start_mb']} -> {c['rss_end_mb']} MB, "
                  f"peak {c['peak_rss_mb']} MB, largest spike {c['max_spike_mb']} MB in {c['max_spike_stage']}"
                  + (f", after gc {c['rss_after_gc_mb']} MB" if c.get("rss_after_gc_mb") is not None else ""))


# --------------------------------------------------------------------------- self-test

def self_test(args: argparse.Namespace, run_id: str) -> int:
    """Fake 15-stage chain (some allocate and free, one keeps, one raises, one uses a child
    process). Checks the hook and the files without the pipeline or a database."""
    import subprocess
    import types

    prof = Profiler(run_id, Path(args.out), args.interval, args.tracemalloc)
    prof.start()
    kept: list[bytearray] = []

    def make(name: str, pass_no: int) -> Callable[..., Any]:
        def fn(chart_id: int, *, force: bool = True) -> dict[str, Any]:
            if name == "ocr_final1":   # a spike that is freed again
                buf = bytearray(200 * MB)
                for i in range(0, len(buf), 4096):
                    buf[i] = 1
                time.sleep(0.3)
                del buf
            elif name == "member_verify":  # memory that stays (a model load)
                buf = bytearray(80 * MB)
                for i in range(0, len(buf), 4096):
                    buf[i] = 1
                kept.append(buf)
                time.sleep(0.2)
            elif name == "dos_extract":
                raise RuntimeError("fake stage failure")
            elif name == "page_sequencing":  # a child process holding ~60 MB
                subprocess.run([sys.executable, "-c",
                                "import time; b = bytearray(60*1024*1024); b[::4096] = b'1'*len(b[::4096]); time.sleep(1.5)"],
                               check=True)
            else:
                time.sleep(0.06)
            return {"chart_id": chart_id, "force": force}
        return fn

    names = [("ocr_quality", 1), ("ocr_prelim", 1), ("blank_junk", 1), ("ocr_final1", 1), ("ocr_final2", 1),
             ("section_headers", 1), ("kv_extract", 1), ("blank_junk", 2), ("member_verify", 1),
             ("page_subtype", 1), ("dos_extract", 1), ("continuity", 1), ("encounter_type", 1),
             ("page_sequencing", 1), ("imaging_final", 1)]
    fake_runner = types.SimpleNamespace(STAGE_CHAIN=[(n, p, make(n, p)) for n, p in names])
    original_list = fake_runner.STAGE_CHAIN
    prof.wrap_stage_chain(fake_runner)
    prof.wrap_stage_chain(fake_runner)  # twice: must not double-wrap

    def fake_run(chart_id: int) -> None:  # the runner's loop, reduced: it catches stage errors
        for _name, _pass, fn in fake_runner.STAGE_CHAIN:
            try:
                fn(chart_id, force=True)
            except RuntimeError:
                pass

    for iteration in (1, 2):
        prof.set_chart("FAKE_CHART", iteration, iteration)
        fake_run(1)
    prof.close()

    problems = []
    if fake_runner.STAGE_CHAIN is not original_list:
        problems.append("STAGE_CHAIN list object was replaced, not patched in place")
    if len(prof.records) != 30:
        problems.append(f"expected 30 stage rows, got {len(prof.records)}")
    by = {(r["iteration"], r["stage_key"]): r for r in prof.records}
    spike = by[(1, "ocr_final1:1")]["spike_mb"]
    if not spike or spike < 150:
        problems.append(f"ocr_final1 spike {spike} MB, expected >= 150")
    # The delta checks are Linux only: there freed large blocks leave RSS at once (mmap) and kept
    # ones stay; macOS keeps freed memory a while and compresses kept memory under pressure.
    if sys.platform.startswith("linux") and abs(by[(1, "ocr_final1:1")]["delta_mb"]) > 60:
        problems.append(f"ocr_final1 delta {by[(1, 'ocr_final1:1')]['delta_mb']} MB, expected ~0 (freed)")
    if sys.platform.startswith("linux") and (by[(1, "member_verify:1")]["delta_mb"] or 0) < 60:
        problems.append(f"member_verify delta {by[(1, 'member_verify:1')]['delta_mb']} MB, expected ~80 (kept)")
    if "RuntimeError" not in by[(1, "dos_extract:1")]["error"]:
        problems.append("dos_extract error not recorded")
    if (by[(1, "page_sequencing:1")]["children_peak_mb"] or 0) < 30:
        problems.append(f"child process not seen: children_peak {by[(1, 'page_sequencing:1')]['children_peak_mb']} MB")
    for suffix in ("_stages.csv", "_samples.csv"):
        path = Path(args.out) / f"{run_id}{suffix}"
        if not path.is_file() or path.stat().st_size < 100:
            problems.append(f"{path.name} missing or empty")
    print_table(prof.records, chart_totals(prof.records))
    print(f"\nkernel high-water mark per stage: {'yes' if prof.hwm.supported else 'no (not Linux)'}; "
          f"cgroup: {'v%s' % prof.sampler.cgroup.version if prof.sampler.cgroup.version else 'not readable'}")
    if problems:
        print("\nSELF-TEST FAILED:\n  " + "\n  ".join(problems))
        return 1
    print(f"\nself-test ok: {len(prof.records)} stage rows -> {args.out}/{run_id}_*.csv")
    return 0


# --------------------------------------------------------------------------- main

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--chart", action="append", default=[], help="chart folder name (repeatable)")
    p.add_argument("--charts-file", help="one chart name per line; blank lines and # comments ignored")
    p.add_argument("--input-type", choices=("blob", "local"), default="blob")
    p.add_argument("--input-path", help="blob prefix (or server folder) holding the chart folders, as the API's input_path")
    p.add_argument("--container", help="blob container (default: AZURE_STORAGE_CONTAINER, else imaging-pipeline)")
    p.add_argument("--output-path", help="write the chart out here afterwards, as the API's output_path (default: no write)")
    p.add_argument("--skip-ocr", action="store_true", help="reuse OCR (no stage-5 bill when OCR exists)")
    p.add_argument("--no-docintel", action="store_true",
                   help="unset the Document Intelligence endpoint for this process: stage 5 sends nothing (and is not billed)")
    p.add_argument("--only", action="append", help="run only this stage (repeatable), e.g. dos_extract or blank_junk:2")
    p.add_argument("--run-through", help="stop after this stage, e.g. ocr_final2")
    p.add_argument("--redownload-pages", action="store_true", help="skip_page_download=false: fetch the pages again")
    p.add_argument("--repeat", type=int, default=1, help="run the chart list N times in this process (cold, then warm)")
    p.add_argument("--settle", type=float, default=2.0, help="seconds idle between charts (default 2)")
    p.add_argument("--gc-between", action="store_true", help="gc.collect() (and malloc_trim on glibc) after each chart; RSS recorded after")
    p.add_argument("--interval", type=float, default=0.05, help="sample interval in seconds (default 0.05)")
    p.add_argument("--tracemalloc", action="store_true", help="top Python allocators per stage (slow; Python heap only)")
    p.add_argument("--out", default=str(HERE / "results"), help="output folder (default: results/ next to this script)")
    p.add_argument("--run-id", help="file prefix (default: <host>-<time>-<pid>)")
    p.add_argument("--core-pipeline", help="core-pipeline folder (default: found automatically)")
    p.add_argument("--env", action="append", help="NAME=VALUE set before the pipeline loads (repeatable), e.g. OMP_NUM_THREADS=2")
    p.add_argument("--quiet", action="store_true", help="pipeline logs at WARNING")
    p.add_argument("--self-test", action="store_true", help="fake stages only: checks psutil, the hook and the files")
    args = p.parse_args(argv)
    if args.charts_file:
        for line in Path(args.charts_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                args.chart.append(line)
    if not args.self_test:
        if not args.chart:
            p.error("give --chart NAME (repeatable) or --charts-file")
        if not args.input_path:
            p.error("--input-path is required (the folder / blob prefix holding the chart folders)")
    if args.repeat < 1:
        p.error("--repeat must be >= 1")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    run_id = args.run_id or f"{socket.gethostname()}-{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}"
    Path(args.out).mkdir(parents=True, exist_ok=True)
    if args.self_test:
        return self_test(args, run_id)

    root = find_core_pipeline(args.core_pipeline)
    prof = Profiler(run_id, Path(args.out), args.interval, args.tracemalloc)
    prof.start()
    cgroup = prof.sampler.cgroup
    proc = psutil.Process()
    summary: dict[str, Any] = {
        "run_id": run_id, "host": socket.gethostname(), "pid": os.getpid(),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0], "platform": platform.platform(),
        "cpu_count": os.cpu_count(), "host_memory_mb": _mb(psutil.virtual_memory().total),
        "cgroup_version": cgroup.version, "cgroup_limit_mb": _mb(cgroup.limit()),
        "kernel_hwm_per_stage": prof.hwm.supported,
        "args": {k: v for k, v in vars(args).items() if k != "env"},
        "env_overrides": [e.split("=", 1)[0] for e in (args.env or [])],
    }
    exit_code = 0
    try:
        rss0 = proc.memory_info().rss
        summary["startup"] = prof.measure("startup", "prepare_environment", None, prepare_environment, root, args)
        summary["env"] = {k: os.environ.get(k) for k in ENV_KEYS if os.environ.get(k) is not None}
        # The stage modules and their import-time state (canons, some models) load here.
        prof.measure("startup", "import_pipeline", None, __import__, "apps.worker.runner")
        hooked = hook_pipeline(prof)
        pages: dict[str, Any] = {}
        capture_page_counts(pages)
        summary["hooked"] = hooked
        summary["rss_python_start_mb"] = _mb(rss0)
        summary["rss_after_imports_mb"] = _mb(proc.memory_info().rss)

        from apps.worker import run_stats
        from apps.worker.chart_run import execute_run

        trigger = (run_stats.new_trigger("memprof"), "memprof")
        charts_out: list[dict[str, Any]] = []
        seq = 0
        for iteration in range(1, args.repeat + 1):
            for chart in args.chart:
                seq += 1
                prof.set_chart(chart, iteration, seq)
                rss_start = proc.memory_info().rss
                info: dict[str, Any] = {"chart": chart, "iteration": iteration, "chart_seq": seq,
                                        "rss_start_mb": _mb(rss_start)}
                t0 = time.monotonic()
                try:
                    payload = build_payload(args, chart)
                    run_stats.in_trigger(trigger, execute_run, payload)
                    info["status"] = (pages.get(chart) or {}).get("status") or "ran"
                except Exception as exc:  # noqa: BLE001 - next chart still runs
                    info["status"] = "failed"
                    info["error"] = f"{type(exc).__name__}: {exc}"[:500]
                    traceback.print_exc()
                    exit_code = 1
                info["wall_s"] = round(time.monotonic() - t0, 1)
                info["rss_end_mb"] = _mb(proc.memory_info().rss)
                info.update({k: v for k, v in (pages.get(chart) or {}).items() if k in ("pages", "chart_id", "stage_errors", "skipped_stages")})
                if args.gc_between:
                    gc.collect()
                    info["malloc_trim"] = malloc_trim()
                    info["rss_after_gc_mb"] = _mb(proc.memory_info().rss)
                charts_out.append(info)
                prof.sampler.set_label(label="between")
                time.sleep(max(0.0, args.settle))
        summary["charts"] = charts_out
    except KeyboardInterrupt:
        summary["interrupted"] = True
        exit_code = 130
    except Exception as exc:  # noqa: BLE001
        summary["error"] = f"{type(exc).__name__}: {exc}"[:500]
        traceback.print_exc()
        exit_code = 1
    finally:
        prof.close()
        stage_rows = [r for r in prof.records if r["kind"] in ("stage", "io")]
        totals = chart_totals(stage_rows)
        for t in totals:
            for c in summary.get("charts", []):
                if (c["chart"], c["iteration"], c["chart_seq"]) == (t["chart"], t["iteration"], t["chart_seq"]):
                    t.update({k: c.get(k) for k in ("pages", "status", "rss_after_gc_mb")})
        summary["chart_totals"] = totals
        summary["cold_vs_warm"] = cold_vs_warm([r for r in stage_rows if r["kind"] == "stage"])
        summary["process_peak_rss_mb"] = max((r["peak_rss_mb"] or 0 for r in prof.records), default=None)
        summary["cgroup_peak_mb"] = _mb(cgroup.peak())  # whole container, since it started
        summary["ended"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if prof.trace_tops:
            summary["tracemalloc_top"] = prof.trace_tops
        summary["stages"] = prof.records
        (Path(args.out) / f"{run_id}_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print_table(prof.records, totals)
        print(f"\nfiles: {args.out}/{run_id}_stages.csv, _samples.csv, _summary.json")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
