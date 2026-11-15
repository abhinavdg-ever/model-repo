"""python -m chart_clustering --source folder|db|blob ... --out <folder>

  folder: --root <charts folder>
  db:     DATABASE_URL in .env (or --database-url-env NAME), --schema public
  blob:   --container <name> --prefix Processed/Run1; AZURE_STORAGE_* in .env
  all:    --chart-list charts.txt (optional), --anchors first_pn|last|both, --min-cluster-size 5,
          --write-db (also write chart_list.chart_cluster; needs DATABASE_URL)
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from chart_clustering import anchors, cluster, features, report, sources

logger = logging.getLogger("chart_clustering")


def _load_env(path: Path) -> None:
    """KEY=VALUE lines from .env into the environment (existing values win). Never printed."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="chart_clustering", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("folder", "db", "blob"), required=True)
    ap.add_argument("--root", help="folder source: the charts folder")
    ap.add_argument("--schema", default="public", help="db source: Postgres schema")
    ap.add_argument("--database-url-env", default="DATABASE_URL", help="db source: env var holding the URL")
    ap.add_argument("--container", help="blob source: container")
    ap.add_argument("--prefix", action="append", default=[],
                    help="blob source: prefix above the chart folders; repeatable for charts across runs "
                         "(--prefix Processed/Run1 --prefix Processed/Run2)")
    ap.add_argument("--paths-from-db", action="store_true",
                    help="blob source with --chart-list: find each chart at its own chart_list.output_path "
                         "(any run / batch; needs DATABASE_URL), then the --prefix folders")
    ap.add_argument("--chart-list", help="optional .txt of chart names, one per line")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--anchors", choices=("first_pn", "last", "both"), default="both")
    ap.add_argument("--notes", type=int, default=3,
                    help="first pages of up to this many Progress Notes per chart, words pooled (default 3)")
    ap.add_argument("--min-charts", type=int, default=3, help="a template word must be on this many charts")
    ap.add_argument("--min-cluster-size", type=int, default=5)
    ap.add_argument("--min-samples", type=int, default=3)
    ap.add_argument("--layout-weight", type=float, default=0.35)
    ap.add_argument("--stability-runs", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--write-db", action="store_true",
                    help="also write each chart's cluster to chart_list.chart_cluster (DATABASE_URL); "
                         "review-ui filters on it")
    return ap.parse_args(argv)


def charts_from(args, missing: list[str]):
    """The charts to cluster. With --chart-list they may come from any runs / batches; listed
    charts that are not found are appended to ``missing`` (written to missing_charts.txt)."""
    names = sources.read_chart_list(args.chart_list)
    if args.source == "folder":
        if not args.root:
            raise SystemExit("--root is required for --source folder")
        return _with_missing(sources.from_folder(Path(args.root), names), names, missing), \
            f"folder:{Path(args.root).name}"
    if args.source == "db":
        return sources.from_db(os.environ.get(args.database_url_env), args.schema, names, missing=missing), \
            f"db:{args.schema}"
    if not args.container:
        raise SystemExit("--container is required for --source blob")
    if args.paths_from_db and not names:
        raise SystemExit("--paths-from-db needs --chart-list")
    paths = (sources.db_output_paths(names, os.environ.get(args.database_url_env), args.schema)
             if args.paths_from_db else None)
    return sources.from_blob(args.container, args.prefix, names, paths=paths, missing=missing), \
        f"blob:{args.container}/{'+'.join(args.prefix) or '(root)'}{' +db paths' if paths is not None else ''}"


def _with_missing(charts, names, missing: list[str]):
    seen = set()
    for chart in charts:
        seen.add(chart.name)
        yield chart
    missing.extend(sorted(set(names or []) - seen))


def main(argv=None) -> int:
    args = parse(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _load_env(Path(args.env_file))
    cfg = cluster.Settings(anchors=args.anchors, min_charts=args.min_charts, min_cluster_size=args.min_cluster_size,
                           min_samples=args.min_samples, layout_weight=args.layout_weight,
                           stability_runs=args.stability_runs, seed=args.seed)
    missing: list[str] = []
    charts, source = charts_from(args, missing)

    rows, layout_rows, usable = [], [], []
    tokens: dict[str, list[list[str]]] = {"first_pn": [], "last": []}
    for chart in charts:
        notes = anchors.progress_note_starts(chart, max(1, args.notes))
        first = notes[0] if notes else anchors.first_progress_note(chart)
        last = anchors.last_page(chart)
        kept = [p for p in chart.pages if p.kept and p.text.strip()]
        hw_known = [p for p in kept if p.handwritten is not None]
        hw_share = sum(p.handwritten for p in hw_known) / len(hw_known) if hw_known else 0.0
        pn_share = anchors.progress_note_share(chart)
        main_page = first.page or last.page
        rows.append({
            "chart_name": chart.name,
            "first_pn_page": " ".join(n.page.name for n in notes) or (first.page.name if first.page else ""),
            "first_pn_how": first.how, "notes_used": len(notes),
            "last_page": last.page.name if last.page else "", "last_how": last.how,
            "pages": len(chart.pages), "kept_pages": len(kept),
            "handwritten_share": round(hw_share, 3), "progress_note_share": round(pn_share, 3),
            "has_pipeline_labels": chart.has_pipeline_csv,
        })
        # The 2-3 notes' first pages pooled: a word on several of them counts more.
        note_pages = [n.page for n in notes] or ([first.page] if first.page else [])
        tokens["first_pn"].append([t for page in note_pages for t in features.template_tokens(page.text)])
        tokens["last"].append(features.template_tokens(last.page.text) if last.page else [])
        layout = features.layout(main_page.text if main_page else "", len(chart.pages), hw_share, pn_share).values
        layout_rows.append(layout)
        rows[-1]["_layout"] = layout
        # Not enough to describe the chart (no anchor page with text): straight to Others.
        usable.append(main_page is not None and len(main_page.text.split()) >= anchors.MIN_WORDS)
    if missing:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        (Path(args.out) / "missing_charts.txt").write_text("\n".join(sorted(missing)) + "\n", encoding="utf-8")
        logger.warning("%d listed chart(s) not found: see missing_charts.txt", len(missing))
    if not rows:
        raise SystemExit("No charts with Tesseract text found")
    logger.info("charts read: %d (%d describable)", len(rows), sum(usable))

    use = {"first_pn": ["first_pn"], "last": ["last"], "both": ["first_pn", "last"]}[cfg.anchors]
    dense, tfidf, names = cluster.build_matrix({a: tokens[a] for a in use}, layout_rows, cfg)
    result = cluster.run(dense, tfidf, names, usable, cfg)
    manifest = report.write(Path(args.out), rows, result, cfg, source)
    logger.info("clusters: %s | silhouette %s | stability %s | out %s", manifest["clusters"],
                manifest["silhouette"], manifest["stability_ari"], args.out)
    if args.write_db:
        from chart_clustering.db_write import write_clusters

        n = write_clusters({r["chart_name"]: r["cluster"] for r in rows},
                           os.environ.get(args.database_url_env), args.schema)
        logger.info("chart_list.chart_cluster written for %d chart(s)", n)
    return 0
