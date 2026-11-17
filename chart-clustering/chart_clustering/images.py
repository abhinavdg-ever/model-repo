"""Page images for the visual track (2026-11-17).

The pages are a chart's anchor pages (the first pages of its first Progress Notes, else its
fallback page). Each is downloaded once into ``--image-cache/<chart>/<page name>``; a re-run
reads the cache.

Where an image is looked for, in order:

  --source db      1. ``{blob_path}/{page name}``: the Raw_Input chart folder in chart_list,
                      e.g. Raw_Input/Run1/Batch1/DEID_PNGs/<chart>/1.jpg
                   2. a listing of that folder (sub-folders too) for the same file name
                   3. with --processed-fallback: ``{output_path}/pages/{page name}``
  --source folder  ``<root>/<chart>/pages/<page name>``, then ``<root>/<chart>/<page name>``
  --source blob    no image locations: the visual track has nothing to read

Nothing here reads or writes page text.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from chart_clustering.ingest import Chart, Page

logger = logging.getLogger("chart_clustering.images")

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp")


def _join(*parts: str) -> str:
    return "/".join(p.strip("/").replace("\\", "/") for p in parts if p and p.strip("/"))


class ImageFetcher:
    def __init__(self, cache: Path, source: str, root: Optional[Path] = None,
                 processed_fallback: bool = False, workers: int = 16) -> None:
        self.cache = cache
        self.source = source
        self.root = root
        self.processed_fallback = processed_fallback
        self.workers = max(1, workers)

    def fetch_all(self, work: list[tuple[Chart, list[Page]]]) -> list[list[Path]]:
        """Each chart's anchor-page images (local paths), in the order given; [] when none."""
        if self.source == "blob":
            return [[] for _ in work]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(lambda item: self._chart(*item), work))

    def _chart(self, chart: Chart, pages: list[Page]) -> list[Path]:
        out: list[Path] = []
        listing: Optional[dict[str, str]] = None
        for page in pages:
            cached = self.cache / chart.name / page.name
            if cached.is_file() and cached.stat().st_size > 0:
                out.append(cached)
                continue
            if self.source == "folder":
                found = self._local(chart, page)
                if found:
                    out.append(found)
                continue
            try:
                got, listing = self._from_blob(chart, page, cached, listing)
            except Exception as exc:  # noqa: BLE001 - one page must not sink the run
                logger.warning("chart %s page %s: image not downloaded (%s)", chart.name, page.name,
                               type(exc).__name__)
                got = False
            if got:
                out.append(cached)
        return out

    def _local(self, chart: Chart, page: Page) -> Optional[Path]:
        if self.root is None:
            return None
        for path in (self.root / chart.name / "pages" / page.name, self.root / chart.name / page.name):
            if path.is_file():
                return path
        return None

    def _from_blob(self, chart: Chart, page: Page, dest: Path,
                   listing: Optional[dict[str, str]]) -> tuple[bool, Optional[dict[str, str]]]:
        from chart_clustering.sources import _container_client

        if not chart.blob_path and not (self.processed_fallback and chart.output_path):
            return False, listing
        client = _container_client(chart.blob_container)
        candidates: list[str] = []
        if chart.blob_path:
            candidates.append(_join(chart.blob_path, page.name))
        for name in candidates:
            if _download(client, name, dest):
                return True, listing
        if chart.blob_path:
            if listing is None:
                # Every image under the Raw_Input chart folder, by file name (lower case).
                prefix = _join(chart.blob_path) + "/"
                listing = {}
                for blob in client.list_blobs(name_starts_with=prefix):
                    base = blob.name.rsplit("/", 1)[-1]
                    if base.lower().endswith(IMAGE_EXT):
                        listing.setdefault(base.lower(), blob.name)
            hit = listing.get(page.name.lower())
            if hit and _download(client, hit, dest):
                return True, listing
        if self.processed_fallback and chart.output_path:
            out = chart.output_path if chart.output_path.rstrip("/").endswith(chart.name) \
                else _join(chart.output_path, chart.name)
            if _download(client, _join(out, "pages", page.name), dest):
                return True, listing
        return False, listing


def _download(client, name: str, dest: Path) -> bool:
    try:
        data = client.download_blob(name).readall()
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ in ("ResourceNotFoundError", "KeyError"):
            return False
        raise
    if not data:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return True
