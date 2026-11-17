"""The visual fingerprint of a chart (2026-11-17): what its anchor pages look like, as numbers.

Classical, CPU only, no model to download. Each page image -> grey, upright (EXIF), resized to
a fixed portrait grid, then:

  thumbnail     16 x 20 cells of ink darkness (where the page is dark: logo, header band, tables)
  ink rows      ink share in 40 horizontal bands (line spacing, header / footer blocks)
  ink columns   ink share in 32 vertical bands (one column, two columns, a left-hand label column)
  edges         8 x 10 cells of edge density (text blocks, ruled forms, boxes)
  page          ink share, darkness spread, aspect ratio

A chart's vector is the mean over its anchor pages. In cluster.py the chart vectors are
standardised -> PCA (up to 50 dimensions) -> unit length. DINOv2-style embeddings are the
planned upgrade (METHODOLOGY.md §9).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("chart_clustering.visual")

WIDTH, HEIGHT = 128, 160          # every page, resized (portrait)
THUMB = (16, 20)                  # thumbnail cells (w, h)
ROW_BANDS, COL_BANDS = 40, 32
EDGE_GRID = (8, 10)               # (w, h)
INK = 0.55                        # a pixel darker than this (0 white .. 1 black) is ink


def _cells(a: np.ndarray, w: int, h: int) -> np.ndarray:
    """Mean of ``a`` (HEIGHT x WIDTH) over an h x w grid of cells, flattened."""
    return a.reshape(h, a.shape[0] // h, w, a.shape[1] // w).mean(axis=(1, 3)).ravel()


def page_vector(path: Path) -> Optional[np.ndarray]:
    """One page's fingerprint, or None when the file is not a readable image."""
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("L")
            aspect = im.height / max(1, im.width)
            im = im.resize((WIDTH, HEIGHT))
            dark = 1.0 - np.asarray(im, dtype=np.float32) / 255.0
    except Exception as exc:  # noqa: BLE001 - a broken file is skipped, the run goes on
        logger.warning("image %s not readable (%s)", path.name, type(exc).__name__)
        return None
    ink = (dark > INK).astype(np.float32)
    gy = np.abs(np.diff(dark, axis=0, append=dark[-1:, :]))
    gx = np.abs(np.diff(dark, axis=1, append=dark[:, -1:]))
    edges = ((gx + gy) > 0.25).astype(np.float32)
    return np.concatenate([
        _cells(dark, *THUMB),
        _cells(ink, 1, ROW_BANDS),
        _cells(ink, COL_BANDS, 1),
        _cells(edges, *EDGE_GRID),
        np.array([ink.mean(), dark.std(), aspect], dtype=np.float32),
    ])


def chart_vector(paths: list[Path]) -> Optional[np.ndarray]:
    """Mean of the readable pages' fingerprints; None when none could be read."""
    vectors = [v for v in (page_vector(p) for p in paths) if v is not None]
    return np.mean(vectors, axis=0) if vectors else None
