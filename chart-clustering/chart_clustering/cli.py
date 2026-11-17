"""python -m chart_clustering --source folder|db|blob ... --out <folder>

  folder: --root <charts folder>
  db:     DATABASE_URL in .env (or --database-url-env NAME), --schema public
  blob:   --container <name> --prefix Processed/Run1; AZURE_STORAGE_* in .env (text only)
  all:    --chart-list charts.txt (optional), --anchors first_pn|last|both, --min-cluster-size 5,
          --write-db (also write chart_list.chart_cluster; needs DATABASE_URL)

Default (2026-11-17): --track mix (text + page images, --visual-weight 0.5), clustered with
HDBSCAN, which decides how many groups. Images come from each chart's Raw_Input folder
(chart_list.blob_path) into --image-cache; a re-run reuses the cache.
  --track text | visual | mix | both        both: out/text and out/visual
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import logging
import os
from pathlib import Path

from chart_clustering import anchors, cluster, features, images, report, sources, visual

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
    ap.add_argument("--track", choices=cluster.TRACKS, default="mix",
                    help="text, visual, mix (text + visual fused; default) or both (text and visual "
                         "run apart into out/text and out/visual)")
    ap.add_argument("--visual-weight", type=float, default=0.5, help="mix: the visual block's weight (default 0.5)")
    ap.add_argument("--image-cache", help="where page images are kept between runs (default <out>/image_cache)")
    ap.add_argument("--processed-fallback", action="store_true",
                    help="visual: also look for a page image under the Processed folder "
                         "(chart_list.output_path/<chart>/pages/) when Raw_Input has none")
    ap.add_argument("--image-workers", type=int, default=0,
                    help="parallel image downloads (default AZURE_BLOB_CONNECTION_POOL_SIZE, else 16)")
    ap.add_argument("--write-db", action="store_true",
                    help="also write each chart's cluster to chart_list.chart_cluster (DATABASE_URL); "
                         "review-ui filters on it. With --track both the text labels are written")
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


@dataclasses.dataclass
class Described:
    """What the run knows about the charts, one entry per chart, in the same order."""
    rows: list[dict] = dataclasses.field(default_factory=list)          # chart_clusters.csv
    layout: list[list[float]] = dataclasses.field(default_factory=list)  # layout numbers
    usable_text: list[bool] = dataclasses.field(default_factory=list)    # has a text anchor
    tokens: dict[str, list[list[str]]] = dataclasses.field(
        default_factory=lambda: {"first_pn": [], "last": []})            # template words per anchor
    image_work: list[tuple] = dataclasses.field(default_factory=list)    # (chart, anchor pages)


def describe(charts, notes_wanted: int) -> Described:
    """Each chart's anchor pages, template words, layout numbers and the pages to image."""
    d = Described()
    for chart in charts:
        notes = anchors.progress_note_starts(chart, max(1, notes_wanted))
        first = notes[0] if notes else anchors.first_progress_note(chart)
        last = anchors.last_page(chart)
        kept = [p for p in chart.pages if p.kept and p.text.strip()]
        hw_known = [p for p in kept if p.handwritten is not None]
        hw_share = sum(p.handwritten for p in hw_known) / len(hw_known) if hw_known else 0.0
        pn_share = anchors.progress_note_share(chart)
        main_page = first.page or last.page
        # The 2-3 notes' first pages pooled: a word on several of them counts more.
        note_pages = [n.page for n in notes] or ([first.page] if first.page else [])
        layout = features.layout(main_page.text if main_page else "", len(chart.pages), hw_share, pn_share).values
        d.rows.append({
            "chart_name": chart.name,
            "first_pn_page": " ".join(n.page.name for n in notes) or (first.page.name if first.page else ""),
            "first_pn_how": first.how, "notes_used": len(notes),
            "last_page": last.page.name if last.page else "", "last_how": last.how,
            "pages": len(chart.pages), "kept_pages": len(kept),
            "handwritten_share": round(hw_share, 3), "progress_note_share": round(pn_share, 3),
            "has_pipeline_labels": chart.has_pipeline_csv,
            "_layout": layout,
        })
        d.tokens["first_pn"].append([t for page in note_pages for t in features.template_tokens(page.text)])
        d.tokens["last"].append(features.template_tokens(last.page.text) if last.page else [])
        d.layout.append(layout)
        # Not enough to describe the chart (no anchor page with text): straight to Others.
        d.usable_text.append(main_page is not None and len(main_page.text.split()) >= anchors.MIN_WORDS)
        # The visual track looks at the same pages: the notes' first pages, else the fallback.
        d.image_work.append((chart, note_pages or ([main_page] if main_page else [])))
    return d


def image_vectors(args, d: Described, out: Path) -> list:
    """Each chart's visual fingerprint (None when it has no readable image)."""
    if args.source == "blob":
        logger.warning("--source blob has no image locations: the visual track needs --source db or folder")
    workers = args.image_workers or int(os.environ.get("AZURE_BLOB_CONNECTION_POOL_SIZE") or 0) or 16
    fetcher = images.ImageFetcher(Path(args.image_cache) if args.image_cache else out / "image_cache",
                                  args.source, Path(args.root) if args.root else None,
                                  processed_fallback=args.processed_fallback, workers=workers)
    paths = fetcher.fetch_all(d.image_work)
    vectors = [visual.chart_vector(p) if p else None for p in paths]
    for row, p, v in zip(d.rows, paths, vectors):
        row["images_used"] = len(p) if v is not None else 0
    logger.info("page images: %d chart(s) with images, %d without",
                sum(v is not None for v in vectors), sum(v is None for v in vectors))
    return vectors


def main(argv=None) -> int:
    args = parse(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _load_env(Path(args.env_file))
    cfg = cluster.Settings(track=args.track, visual_weight=args.visual_weight, anchors=args.anchors,
                           min_charts=args.min_charts, min_cluster_size=args.min_cluster_size,
                           min_samples=args.min_samples, layout_weight=args.layout_weight,
                           stability_runs=args.stability_runs, seed=args.seed)
    out = Path(args.out)
    missing: list[str] = []
    charts, source = charts_from(args, missing)
    d = describe(charts, args.notes)
    if missing:
        out.mkdir(parents=True, exist_ok=True)
        (out / "missing_charts.txt").write_text("\n".join(sorted(missing)) + "\n", encoding="utf-8")
        logger.warning("%d listed chart(s) not found: see missing_charts.txt", len(missing))
    if not d.rows:
        raise SystemExit("No charts with Tesseract text found")
    logger.info("charts read: %d (%d describable by text)", len(d.rows), sum(d.usable_text))

    wants_images = args.track != "text"
    vectors = image_vectors(args, d, out) if wants_images else [None] * len(d.rows)
    has_image = [v is not None for v in vectors]
    if args.track in ("visual", "both") and not any(has_image):
        raise SystemExit(f"--track {args.track}: no page images found (see the log)")
    if args.track == "mix" and not any(has_image):
        # Recorded in run_manifest.json (visual_used: false), not silent.
        logger.warning("--track mix: no page images found; clustering on text only")

    use = {"first_pn": ["first_pn"], "last": ["last"], "both": ["first_pn", "last"]}[cfg.anchors]
    text_dense, tfidf, names = cluster.build_matrix({a: d.tokens[a] for a in use}, d.layout, cfg)
    visual_dense = cluster.visual_matrix(vectors, cfg) if wants_images else None
    matrices = {
        "text": (text_dense, d.usable_text, tfidf),
        "visual": (visual_dense, has_image, None),
        "mix": (cluster.fuse(text_dense, visual_dense, cfg.visual_weight) if wants_images else None,
                d.usable_text, tfidf),
    }

    tracks = ["text", "visual"] if args.track == "both" else [args.track]
    written = None
    for track in tracks:
        dense, usable, terms = matrices[track]
        run_cfg = dataclasses.replace(cfg, track=track)
        rows = copy.deepcopy(d.rows)
        result = cluster.run(dense, terms, names if terms is not None else [], usable, run_cfg)
        extra = {"visual_used": track != "text" and any(has_image),
                 "charts_with_images": sum(has_image) if wants_images else None}
        folder = out / track if len(tracks) > 1 else out
        manifest = report.write(folder, rows, result, run_cfg, source, extra)
        logger.info("%s clusters: %s | silhouette %s | stability %s | out %s", track,
                    manifest["clusters"], manifest["silhouette"], manifest["stability_ari"], folder)
        written = written or rows
    if args.write_db:
        from chart_clustering.db_write import write_clusters

        n = write_clusters({r["chart_name"]: r["cluster"] for r in written},
                           os.environ.get(args.database_url_env), args.schema)
        logger.info("chart_list.chart_cluster written for %d chart(s)", n)
    return 0
