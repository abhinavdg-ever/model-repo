"""Outputs (numbers, ids and whitelisted generic words only; never page text):

  chart_clusters.csv    one row per chart: cluster, membership strength, anchor pages used and
                        how they were found, page counts, handwritten / Progress Note shares
  cluster_summary.csv   one row per cluster: size, share, top template words (whitelisted),
                        how the anchors were found, layout averages, 10 example charts
  cluster_cards.md      the same, readable, for a person to name each cluster
  run_manifest.json     settings, versions, counts, silhouette, stability, data hash
  features.npz          the clustering matrix + chart names (for re-use and plots)
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from chart_clustering import __version__
from chart_clustering.cluster import OTHERS, Result, Settings
from chart_clustering.features import LAYOUT_NAMES
from chart_clustering.vocab import displayable


def data_hash(rows: list[dict[str, Any]]) -> str:
    key = "\n".join(f"{r['chart_name']}|{r['first_pn_page']}|{r['last_page']}" for r in rows)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _display_terms(terms: list[str], limit: int = 12) -> list[str]:
    """The cluster's strongest words that are on the display list (vocab.py), anchor prefix
    ("first_pn:" / "last:") dropped; a field label shows as "Name:"."""
    out: list[str] = []
    for term in terms:
        word = term.split(":", 1)[1] if ":" in term else term
        if not displayable(word):
            continue
        shown = word[len("label:"):] + ":" if word.startswith("label:") else word
        if shown not in out:
            out.append(shown)
        if len(out) >= limit:
            break
    return out


def write(out_dir: Path, rows: list[dict[str, Any]], result: Result, cfg: Settings, source: str) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for row, label, p in zip(rows, result.labels, result.probability):
        row["cluster"] = label
        row["membership"] = round(float(p), 3)

    fields = ["chart_name", "cluster", "membership", "first_pn_page", "first_pn_how", "last_page", "last_how",
              "pages", "kept_pages", "handwritten_share", "progress_note_share", "has_pipeline_labels"]
    with (out_dir / "chart_clusters.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(result.labels)
    order = sorted((c for c in counts if c != OTHERS)) + ([OTHERS] if OTHERS in counts else [])
    summary = []
    layout = np.asarray([r["_layout"] for r in rows], dtype=float) if rows else np.zeros((0, len(LAYOUT_NAMES)))
    for cluster in order:
        members = [i for i, label in enumerate(result.labels) if label == cluster]
        how = Counter(rows[i]["first_pn_how"] for i in members)
        means = layout[members].mean(axis=0) if members else np.zeros(len(LAYOUT_NAMES))
        summary.append({
            "cluster": cluster,
            "charts": len(members),
            "share": round(len(members) / max(1, len(rows)), 3),
            "top_words": ", ".join(_display_terms(result.top_terms.get(cluster, []))),
            "anchor_found_by": "; ".join(f"{k} {v}" for k, v in how.most_common()),
            **{f"avg_{n}": round(float(v), 3) for n, v in zip(LAYOUT_NAMES, means)},
            "examples": " ".join(rows[i]["chart_name"] for i in members[:10]),
        })
    with (out_dir / "cluster_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary[0]) if summary else ["cluster"])
        writer.writeheader()
        writer.writerows(summary)

    lines = ["# Chart clusters (first cut)", "",
             f"{len(rows)} charts, {len([c for c in counts if c != OTHERS])} clusters, "
             f"{counts.get(OTHERS, 0)} in Others. Name each cluster from its words and examples.", ""]
    for s in summary:
        lines += [f"## {s['cluster']} -- {s['charts']} charts ({s['share']:.0%})", "",
                  f"- Template words: {s['top_words'] or '(none on the display list)'}",
                  f"- Anchor found by: {s['anchor_found_by']}",
                  f"- Avg lines {s['avg_lines']:.0f}, words {s['avg_words']:.0f}, label lines "
                  f"{s['avg_label_line_share']:.0%}, fax header {s['avg_fax_header']:.0%}, handwritten "
                  f"{s['avg_chart_handwritten_share']:.0%}, Progress Note pages {s['avg_chart_progress_note_share']:.0%}",
                  f"- Examples: {s['examples']}", "- Name: ________________", ""]
    (out_dir / "cluster_cards.md").write_text("\n".join(lines), encoding="utf-8")

    np.savez_compressed(out_dir / "features.npz", x=result.vectors,
                        charts=np.asarray([r["chart_name"] for r in rows]))
    manifest = {
        "version": __version__,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "settings": cfg.__dict__,
        "charts": len(rows),
        "clusters": {c: counts[c] for c in order},
        "silhouette": None if result.silhouette is None else round(result.silhouette, 4),
        "stability_ari": None if result.stability_ari is None else round(result.stability_ari, 4),
        "data_hash": data_hash(rows),
    }
    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
