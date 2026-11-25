#!/usr/bin/env python3
"""Summarise stage_memory.py runs: RAM per stage across charts, and a plot per run.

    python3 report.py results/                      # every *_stages.csv below results/
    python3 report.py results/20261125-101500/parallel-3 results/other/x_stages.csv
    python3 report.py results/ --csv summary.csv --no-plots

Prints, per stage (stage_name:pass_no, plus intake / ensure_images / write_out):
runs, max and average spike (peak - before), max peak RSS, average and max delta
(after - before), average wall seconds, and the same split cold (the first chart of its
process) vs warm (every later chart). Then one line per chart run, and per scenario folder
the host / container peaks from run_scenarios.sh's host_memory.csv.

With matplotlib (run_scenarios.sh setup installs it into .pydeps) it writes, next to each
run's CSVs, <run-id>_rss.png: RSS (process, + children, container working set) over time
with the stages as bands; and host_memory.png per scenario folder. Standard library only
otherwise.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

HERE = Path(__file__).resolve().parent
if (HERE / ".pydeps").is_dir():
    sys.path.append(str(HERE / ".pydeps"))


def _f(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in ("", None) else None
    except ValueError:
        return None


def find_stage_csvs(paths: Iterable[str]) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found.extend(sorted(p.rglob("*_stages.csv")))
        elif p.is_file() and p.name.endswith("_stages.csv"):
            found.append(p)
        else:
            print(f"skipped (not a folder or *_stages.csv): {raw}", file=sys.stderr)
    return [p for p in found if not p.name.startswith("._")]


def read_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows = []
    for path in paths:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                row["_file"] = str(path)
                rows.append(row)
    return rows


def stage_order(rows: list[dict[str, Any]]) -> list[str]:
    order: list[str] = []
    for r in rows:
        if r["stage_key"] not in order:
            order.append(r["stage_key"])
    return order


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("kind") in ("stage", "io"):
            groups[r["stage_key"]].append(r)
    out = []
    for key in stage_order([r for r in rows if r.get("kind") in ("stage", "io")]):
        g = groups[key]
        spikes = [x for x in (_f(r["spike_mb"]) for r in g) if x is not None]
        deltas = [x for x in (_f(r["delta_mb"]) for r in g) if x is not None]
        peaks = [x for x in (_f(r["peak_rss_mb"]) for r in g) if x is not None]
        walls = [x for x in (_f(r["wall_s"]) for r in g) if x is not None]
        cold = [_f(r["spike_mb"]) or 0 for r in g if str(r.get("chart_seq")) == "1"]
        warm = [_f(r["spike_mb"]) or 0 for r in g if str(r.get("chart_seq")) not in ("1", "")]
        cgp = [x for x in (_f(r.get("cgroup_ws_peak_mb")) or _f(r.get("cgroup_peak_mb")) for r in g) if x is not None]
        out.append({
            "stage_key": key, "runs": len(g),
            "max_spike_mb": max(spikes, default=None),
            "avg_spike_mb": round(sum(spikes) / len(spikes), 1) if spikes else None,
            "max_peak_rss_mb": max(peaks, default=None),
            "avg_delta_mb": round(sum(deltas) / len(deltas), 1) if deltas else None,
            "max_delta_mb": max(deltas, default=None),
            "avg_wall_s": round(sum(walls) / len(walls), 1) if walls else None,
            "cold_max_spike_mb": max(cold, default=None),
            "warm_max_spike_mb": max(warm, default=None),
            "max_container_mb": max(cgp, default=None),
            "errors": sum(1 for r in g if r.get("error")),
        })
    return out


def chart_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runs: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        if r.get("kind") not in ("stage", "io"):
            continue
        k = (r["run_id"], r["chart"], r["iteration"], r["chart_seq"])
        t = runs.setdefault(k, {"run_id": r["run_id"], "chart": r["chart"], "iteration": r["iteration"],
                                "chart_seq": r["chart_seq"], "wall_s": 0.0, "start_mb": _f(r["rss_before_mb"]),
                                "peak_mb": 0.0, "spike_mb": 0.0, "spike_stage": ""})
        t["wall_s"] += _f(r["wall_s"]) or 0
        t["peak_mb"] = max(t["peak_mb"], _f(r["peak_rss_mb"]) or 0)
        if (_f(r["spike_mb"]) or 0) > t["spike_mb"]:
            t["spike_mb"], t["spike_stage"] = _f(r["spike_mb"]) or 0, r["stage_key"]
        t["end_mb"] = _f(r["rss_after_mb"])
    return list(runs.values())


def fmt(v: Any) -> str:
    return "-" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v))


def print_summary(agg: list[dict[str, Any]], runs: list[dict[str, Any]]) -> None:
    cols = [("stage_key", 18), ("runs", 4), ("max_spike_mb", 9), ("avg_spike_mb", 9), ("max_peak_rss_mb", 9),
            ("avg_delta_mb", 9), ("max_delta_mb", 9), ("avg_wall_s", 8), ("cold_max_spike_mb", 9),
            ("warm_max_spike_mb", 9), ("max_container_mb", 9), ("errors", 6)]
    heads = ["stage", "runs", "spikeMax", "spikeAvg", "peakRSS", "deltaAvg", "deltaMax", "wall_s",
             "coldSpk", "warmSpk", "contPeak", "errors"]
    print("RAM per stage across runs (MB). spike = peak - before; delta = after - before (what stays);")
    print("cold = first chart in its process (includes model loads); contPeak = container working set.\n")
    print(" ".join(h.rjust(w) if i else h.ljust(w) for i, (h, (_, w)) in enumerate(zip(heads, cols))))
    for a in agg:
        print(" ".join(fmt(a[c]).rjust(w) if i else fmt(a[c])[:w].ljust(w) for i, (c, w) in enumerate(cols)))
    if runs:
        print("\nPer chart run")
        for t in runs:
            print(f"  {t['run_id']}  {t['chart']}  it{t['iteration']}  #{t['chart_seq']}: {t['wall_s']:.0f} s, "
                  f"RSS {fmt(t['start_mb'])} -> {fmt(t.get('end_mb'))} MB, peak {t['peak_mb']:.0f} MB, "
                  f"largest spike {t['spike_mb']:.0f} MB in {t['spike_stage']}")


def host_peaks(folders: set[Path]) -> None:
    lines = []
    for folder in sorted(folders):
        path = folder / "host_memory.csv"
        if not path.is_file():
            continue
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            continue
        peak = lambda col: max((_f(r.get(col)) or 0 for r in rows), default=0)  # noqa: E731
        lines.append(f"  {folder.name}: host used {peak('host_used_mb'):.0f} MB, containers {peak('containers_mb'):.0f} MB "
                     f"(working set {peak('containers_ws_mb'):.0f} MB, max {int(peak('containers'))} at once), "
                     f"own cgroup {peak('self_cgroup_mb'):.0f} MB, swap {peak('swap_used_mb'):.0f} MB")
    if lines:
        print("\nWhole machine / containers (run_scenarios.sh host_memory.csv)")
        print("\n".join(lines))


# --------------------------------------------------------------------------- plots

def plot_run(samples_csv: Path, plt: Any) -> Optional[Path]:
    with open(samples_csv, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) < 2:
        return None
    t = [_f(r["t_s"]) or 0 for r in rows]
    fig, ax = plt.subplots(figsize=(14, 5))
    # Stage bands: consecutive samples with the same label.
    start = 0
    shade = False
    for i in range(1, len(rows) + 1):
        if i == len(rows) or rows[i]["stage_key"] != rows[start]["stage_key"] or rows[i]["chart"] != rows[start]["chart"]:
            label = rows[start]["stage_key"]
            if label not in ("between", "startup"):
                ax.axvspan(t[start], t[i - 1], color="#d0d7e2" if shade else "#e8ecf2", lw=0)
                if t[i - 1] - t[start] > (t[-1] - t[0]) / 80:
                    ax.text((t[start] + t[i - 1]) / 2, 1.0, label.replace(":1", ""), rotation=90, fontsize=7,
                            va="top", ha="center", transform=ax.get_xaxis_transform(), color="#334")
                shade = not shade
            start = i
    ax.plot(t, [_f(r["rss_mb"]) for r in rows], color="#1f5fbf", lw=1.2, label="process RSS")
    if any((_f(r["children_rss_mb"]) or 0) > 0 for r in rows):
        ax.plot(t, [_f(r["total_rss_mb"]) for r in rows], color="#c2571a", lw=1, label="process + children")
    if any(_f(r.get("cgroup_ws_mb")) for r in rows):
        ax.plot(t, [_f(r.get("cgroup_ws_mb")) for r in rows], color="#2e8b57", lw=1, ls="--",
                label="container working set")
    ax.set_xlabel("seconds")
    ax.set_ylabel("MB")
    ax.set_title(samples_csv.name.replace("_samples.csv", ""), fontsize=10)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    out = samples_csv.with_name(samples_csv.name.replace("_samples.csv", "_rss.png"))
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def plot_host(folder: Path, plt: Any) -> Optional[Path]:
    path = folder / "host_memory.csv"
    if not path.is_file():
        return None
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) < 2:
        return None
    x = list(range(len(rows)))
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(x, [_f(r["host_used_mb"]) for r in rows], label="host used", color="#555")
    if any(_f(r.get("containers_ws_mb")) for r in rows):
        ax.plot(x, [_f(r["containers_ws_mb"]) for r in rows], label="profiled containers (working set, sum)", color="#2e8b57")
        per: dict[str, list[Optional[float]]] = defaultdict(lambda: [None] * len(rows))
        for i, r in enumerate(rows):
            for item in (r.get("per_container") or "").split(";"):
                if "=" in item:
                    name, _, mb = item.rpartition("=")
                    per[name][i] = _f(mb)
        for name, ys in per.items():
            ax.plot(x, ys, lw=0.8, label=name[:40])
    if any(_f(r.get("self_cgroup_mb")) for r in rows):
        ax.plot(x, [_f(r["self_cgroup_mb"]) for r in rows], label="this container (cgroup)", color="#1f5fbf")
    ax.set_xlabel("samples (HOST_INTERVAL s apart)")
    ax.set_ylabel("MB")
    ax.set_title(f"{folder.name}: host and container memory", fontsize=10)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="upper left", fontsize=7)
    fig.tight_layout()
    out = folder / "host_memory.png"
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Summarise stage_memory.py runs.")
    p.add_argument("paths", nargs="+", help="folders (searched recursively) or *_stages.csv files")
    p.add_argument("--csv", help="also write the per-stage summary here")
    p.add_argument("--no-plots", action="store_true", help="skip the PNGs even if matplotlib is there")
    args = p.parse_args(argv)

    files = find_stage_csvs(args.paths)
    if not files:
        print("no *_stages.csv found", file=sys.stderr)
        return 1
    rows = read_rows(files)
    agg = aggregate(rows)
    print(f"{len(files)} run(s), {len(rows)} rows\n")
    print_summary(agg, chart_runs(rows))
    host_peaks({f.parent for f in files})

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(agg[0].keys()) if agg else ["stage_key"])
            w.writeheader()
            w.writerows(agg)
        print(f"\nwrote {args.csv}")

    if not args.no_plots:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            print("\n(no matplotlib: plots skipped; run_scenarios.sh setup installs it into .pydeps)")
            return 0
        made = []
        for f in files:
            samples = f.with_name(f.name.replace("_stages.csv", "_samples.csv"))
            if samples.is_file():
                made.append(plot_run(samples, plt))
        for folder in sorted({f.parent for f in files}):
            made.append(plot_host(folder, plt))
        made = [m for m in made if m]
        if made:
            print("\nplots:\n  " + "\n  ".join(str(m) for m in made))
    return 0


if __name__ == "__main__":
    sys.exit(main())
