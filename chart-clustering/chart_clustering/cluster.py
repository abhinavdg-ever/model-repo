"""Unsupervised segmentation of charts from their anchor pages.

1. Template words -> TF-IDF (a word must be on >= min_charts charts and <= 95% of them),
   one vectoriser per anchor (first Progress Note page, last page), stacked.
2. TF-IDF -> TruncatedSVD (LSA, up to 50 dimensions) -> unit length.
3. Layout numbers -> standardised, scaled by ``layout_weight`` and appended.
4. HDBSCAN (min_cluster_size, min_samples). Its noise, and charts with no usable anchor page,
   make ONE cluster: **Others** (2026-11-15, user).
5. Quality without labels: silhouette on the clustered charts, and stability as the mean
   adjusted Rand index over ``stability_runs`` re-clusterings of 80% samples.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

OTHERS = "Others"


@dataclass
class Settings:
    anchors: str = "both"            # first_pn | last | both
    min_charts: int = 3              # a template word must be on this many charts
    max_share: float = 0.95          # ... and on at most this share of them
    svd_dims: int = 50
    layout_weight: float = 0.35
    min_cluster_size: int = 5
    min_samples: int = 3
    stability_runs: int = 5
    seed: int = 42


@dataclass
class Result:
    labels: list[str]                    # per chart: "C01" ... or "Others"
    probability: list[float]             # HDBSCAN membership strength (0 for Others)
    top_terms: dict[str, list[str]]      # cluster -> its strongest template words (unfiltered)
    silhouette: Optional[float]
    stability_ari: Optional[float]
    vectors: np.ndarray = field(repr=False, default_factory=lambda: np.zeros((0, 0)))
    feature_names: list[str] = field(default_factory=list)


def _tfidf(docs: list[list[str]], cfg: Settings):
    from sklearn.feature_extraction.text import TfidfVectorizer

    usable = sum(1 for d in docs if d)
    vec = TfidfVectorizer(analyzer=lambda tokens: tokens, min_df=min(cfg.min_charts, max(1, usable)),
                          max_df=cfg.max_share if usable >= 20 else 1.0, sublinear_tf=True)
    matrix = vec.fit_transform(docs)
    return matrix, list(vec.get_feature_names_out())


def build_matrix(token_sets: dict[str, list[list[str]]], layout_rows: list[list[float]], cfg: Settings):
    """(dense matrix for clustering, sparse TF-IDF for the cards, feature names)."""
    from scipy.sparse import hstack
    from sklearn.decomposition import TruncatedSVD
    from sklearn.preprocessing import StandardScaler, normalize

    blocks, names = [], []
    for anchor, docs in token_sets.items():
        matrix, feats = _tfidf(docs, cfg)
        blocks.append(matrix / max(1, len(token_sets)) ** 0.5)
        names += [f"{anchor}:{f}" for f in feats]
    tfidf = hstack(blocks).tocsr()
    dims = max(1, min(cfg.svd_dims, tfidf.shape[1] - 1, tfidf.shape[0] - 1))
    if tfidf.shape[1] > 1 and dims >= 1:
        lsa = TruncatedSVD(n_components=dims, random_state=cfg.seed).fit_transform(tfidf)
    else:
        lsa = tfidf.toarray()
    lsa = normalize(lsa)
    layout = np.asarray(layout_rows, dtype=float)
    if layout.size:
        layout = StandardScaler().fit_transform(np.log1p(np.clip(layout, 0, None))) * cfg.layout_weight
        dense = np.hstack([lsa, layout])
    else:
        dense = lsa
    return dense, tfidf, names


def _hdbscan(x: np.ndarray, cfg: Settings) -> tuple[np.ndarray, np.ndarray]:
    from sklearn.cluster import HDBSCAN

    if len(x) < max(cfg.min_cluster_size, 2):
        return np.full(len(x), -1), np.zeros(len(x))
    import inspect

    extra = {"copy": True} if "copy" in inspect.signature(HDBSCAN).parameters else {}
    model = HDBSCAN(min_cluster_size=cfg.min_cluster_size, min_samples=cfg.min_samples, **extra)
    model.fit(x)
    return model.labels_, getattr(model, "probabilities_", np.ones(len(x)))


def run(dense: np.ndarray, tfidf, names: list[str], usable: list[bool], cfg: Settings) -> Result:
    """Cluster the charts with a usable anchor; every other chart is Others."""
    idx = [i for i, ok in enumerate(usable) if ok]
    raw = np.full(len(usable), -1)
    prob = np.zeros(len(usable))
    if idx:
        lab, p = _hdbscan(dense[idx], cfg)
        raw[idx], prob[idx] = lab, p

    # Name clusters C01, C02 ... by size (largest first); noise and no-anchor charts -> Others.
    sizes = {c: int((raw == c).sum()) for c in set(raw.tolist()) if c >= 0}
    order = sorted(sizes, key=lambda c: (-sizes[c], c))
    name_of = {c: f"C{i + 1:02d}" for i, c in enumerate(order)}
    labels = [name_of.get(int(c), OTHERS) for c in raw]
    prob = np.where(raw >= 0, prob, 0.0)

    top_terms: dict[str, list[str]] = {}
    for c, label in name_of.items():
        rows = np.where(raw == c)[0]
        mean = np.asarray(tfidf[rows].mean(axis=0)).ravel()
        top_terms[label] = [names[j] for j in np.argsort(-mean)[:40] if mean[j] > 0]

    sil = None
    clustered = [i for i in idx if raw[i] >= 0]
    if len(set(raw[clustered].tolist())) >= 2:
        from sklearn.metrics import silhouette_score

        sil = float(silhouette_score(dense[clustered], raw[clustered]))
    return Result(labels, prob.tolist(), top_terms, sil, _stability(dense, idx, raw, cfg),
                  vectors=dense, feature_names=names)


def _stability(dense: np.ndarray, idx: list[int], full: np.ndarray, cfg: Settings) -> Optional[float]:
    """Mean ARI between the full clustering and re-clusterings of 80% samples (noise counted
    as its own group), on the charts each sample kept. >= 0.8 is the concept note's target."""
    if cfg.stability_runs <= 0 or len(idx) < 2 * cfg.min_cluster_size:
        return None
    from sklearn.metrics import adjusted_rand_score

    rng = np.random.default_rng(cfg.seed)
    scores = []
    for _ in range(cfg.stability_runs):
        pick = np.sort(rng.choice(idx, size=max(2, int(0.8 * len(idx))), replace=False))
        lab, _ = _hdbscan(dense[pick], cfg)
        scores.append(adjusted_rand_score(full[pick], lab))
    return float(np.mean(scores))
