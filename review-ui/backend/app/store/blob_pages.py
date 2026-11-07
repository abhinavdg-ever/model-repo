"""Page image location in Azure Blob, shared by the Postgres and Cosmos repositories.

A repository supplies the chart's page location rows (``_page_location_rows``); this mixin
finds each page's blob: Processed ``<output_path>/corrected-pages/`` or ``pages/`` first,
then the scan as it arrived in Raw_Input (listing order = page order).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.store.rows import page_locations

logger = logging.getLogger("review_ui.postgres")

# How long page → blob locations are cached (Raw_Input listing order = page_number).
BLOB_LIST_TTL_SEC = 300.0


class BlobPageImages:
    """Mixin: resolve_page_blob / no_blob_reason over ``_page_location_rows``."""

    def _init_blob_caches(self) -> None:
        # Per-chart caches for the page image route (TTL BLOB_LIST_TTL_SEC):
        # folder_id → sorted (key, etag) under the Raw_Input prefix
        self._raw_blob_lists: dict[str, tuple[float, list[tuple[str, str]]]] = {}
        # folder_id → page_number → page location row
        self._page_rows_cache: dict[str, tuple[float, dict[int, tuple]]] = {}
        # (folder_id, page_number, corrected) → resolved blob location
        self._resolved_blobs: dict[tuple[str, int, bool], tuple[float, dict[str, str]]] = {}
        # Processed <output_path>/pages/ per chart: page file name -> (key, etag).
        self._processed_pages: dict[str, tuple[float, dict[str, tuple[str, str]]]] = {}
        # Charts whose missing Blob image was already logged; the last listing error per chart.
        self._no_blob_logged: set[str] = set()
        # Why the last lookup found no blob for a page, for the image route's error.
        self._no_blob_reasons: dict[tuple[str, int], str] = {}
        self._blob_list_errors: dict[str, str] = {}

    def _page_location_rows(self, folder_id: str) -> list[tuple]:
        """(blob_container, blob_path, output_path, page_name, page_number, use_corrected,
        image_path) per page, in page order. Raises when the database cannot be read."""
        raise NotImplementedError

    def _page_rows(self, folder_id: str) -> dict[int, tuple] | None:
        """page_number → (container, blob_path, output_path, page_name,
        use_corrected, image_path, ingest_index), cached per chart.

        Every page image request needs this; reading the whole chart's pages per image
        made a filmstrip of N thumbnails cost N queries.
        """
        now = time.monotonic()
        cached = self._page_rows_cache.get(folder_id)
        if cached and (now - cached[0]) < BLOB_LIST_TTL_SEC:
            return cached[1]
        try:
            rows = self._page_location_rows(folder_id)
        except Exception as exc:
            logger.warning("resolve_page_blob DB failed for %s: %s", folder_id, exc)
            return None
        by_num = page_locations(rows)
        self._page_rows_cache[folder_id] = (now, by_num)
        return by_num

    def resolve_page_blob(
        self, folder_id: str, page_number: int, *, corrected: bool = False
    ) -> dict[str, str] | None:
        """Locate the Azure blob for a page using the chart's and page's location.

        Processed (``output_path``) first: ``corrected-pages/`` when ``corrected``, then
        ``pages/``; then the scan as it arrived in Raw_Input. The pipeline uploads
        ``pages/`` to Processed only with ``write_mode=all_files``, so most charts take
        their originals from Raw_Input.

        Returns ``container``, ``key``, ``filename`` and ``etag`` (empty when
        unknown). Results are cached per page for ``BLOB_LIST_TTL_SEC``.
        """
        now = time.monotonic()
        cache_key = (folder_id, page_number, corrected)
        cached = self._resolved_blobs.get(cache_key)
        if cached and (now - cached[0]) < BLOB_LIST_TTL_SEC:
            return cached[1]

        rows = self._page_rows(folder_id)
        hit = rows.get(page_number) if rows else None
        if hit is None:
            self._no_blob(folder_id, page_number, "no page_list row for this page")
            return None
        container, blob_path, output_path, page_name, _use_corrected, image_path, idx = hit
        if not container:
            # chart_list.blob_container wins; BLOB_CONTAINER is the default.
            from app.core.config import get_settings

            container = get_settings().blob_container.strip().strip("/")
        if not container:
            self._no_blob(folder_id, page_number, "no chart_list.blob_container and no BLOB_CONTAINER")
            return None

        out = (output_path or "").strip().strip("/")
        candidates: list[str] = []
        if corrected and out:
            stem = Path(page_name).stem
            candidates.append(f"{out}/corrected-pages/{stem}.jpg")
            candidates.append(f"{out}/corrected-pages/{page_name}")
            rel = image_path.replace("\\", "/").lstrip("/")
            if rel.startswith("corrected-pages/"):
                candidates.append(f"{out}/{rel}")

        ordered = list(dict.fromkeys(k.lstrip("/") for k in candidates if k.lstrip("/")))
        found = self._first_existing_blob(container, ordered) if ordered else None
        if found is None and out:
            # Processed pages/: one listing per chart, not a probe per image.
            found = self._processed_page_blob(folder_id, container, out, page_name)

        # Raw_Input comes from a live listing, so the key is known to exist.
        raw = None if found else self._raw_input_blob(folder_id, container, blob_path, idx)
        if found is None and raw:
            found = raw
        if found is None:
            listing_error = self._blob_list_errors.pop(folder_id, None)
            self._no_blob(
                folder_id, page_number,
                f"could not list Blob ({listing_error}); the paths are "
                f"{container}/{out or '(no output_path)'} and {container}/{blob_path or '(no blob_path)'}"
                if listing_error else
                f"not in {container}/{out or '(no output_path)'}/pages/{page_name} "
                f"nor in the Raw_Input listing of {container}/{blob_path or '(no blob_path)'}",
            )
            return None
        key, etag = found
        out_loc = {"container": container, "key": key, "filename": Path(key).name or page_name, "etag": etag}
        self._resolved_blobs[cache_key] = (now, out_loc)
        return out_loc

    def _no_blob(self, folder_id: str, page_number: int, why: str) -> None:
        """Say once per chart why its page images are not in Blob, and keep the reason for
        the page's error."""
        self._no_blob_reasons[(folder_id, page_number)] = why
        if folder_id in self._no_blob_logged:
            return
        self._no_blob_logged.add(folder_id)
        logger.warning("page image not in Blob: chart=%s page=%s — %s", folder_id, page_number, why)

    def no_blob_reason(self, folder_id: str, page_number: int) -> str | None:
        """Why the last lookup found no blob for this page, or None."""
        return self._no_blob_reasons.get((folder_id, page_number))

    def _processed_page_blob(
        self, folder_id: str, container: str, output_path: str, page_name: str
    ) -> tuple[str, str] | None:
        """(key, etag) of ``<output_path>/pages/<page_name>`` when Processed has it."""
        now = time.monotonic()
        cached = self._processed_pages.get(folder_id)
        if cached and (now - cached[0]) < BLOB_LIST_TTL_SEC:
            by_name = cached[1]
        else:
            try:
                from app.services.blob_store import list_image_blobs

                by_name = {
                    Path(key).name: (key, etag)
                    for key, etag in list_image_blobs(container, f"{output_path}/pages")
                }
            except Exception as exc:
                # Not cached: a sign-in failure must not hide the pages once Blob is back.
                self._blob_list_errors[folder_id] = str(getattr(exc, "detail", None) or exc)
                if getattr(exc, "status_code", None) != 503:
                    logger.warning("Processed pages list failed for %s: %s", folder_id, exc)
                return None
            self._processed_pages[folder_id] = (now, by_name)
        return by_name.get(page_name)

    def _first_existing_blob(self, container: str, keys: list[str]) -> tuple[str, str] | None:
        """First key that exists, with its ETag (fallback path only)."""
        try:
            from app.services.blob_store import _blob_service_client

            client = _blob_service_client()
            for key in keys:
                blob = client.get_blob_client(container=container, blob=key)
                try:
                    props = blob.get_blob_properties()
                    return key, str(props.etag or "").strip('"')
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("blob existence probe skipped: %s", exc)
            return (keys[0], "") if keys else None
        return None

    def _raw_input_blob(
        self,
        folder_id: str,
        container: str,
        blob_path: str | None,
        page_index_1based: int,
    ) -> tuple[str, str] | None:
        """Map page index → (Raw_Input blob key, etag), same sort order as ingest."""
        prefix = (blob_path or "").strip()
        if not container or not prefix:
            return None
        now = time.monotonic()
        cached = self._raw_blob_lists.get(folder_id)
        if cached and (now - cached[0]) < BLOB_LIST_TTL_SEC:
            blobs = cached[1]
        else:
            try:
                from app.services.blob_store import list_image_blobs

                blobs = list_image_blobs(container, prefix)
                self._raw_blob_lists[folder_id] = (now, blobs)
            except Exception as exc:
                self._blob_list_errors[folder_id] = str(getattr(exc, "detail", None) or exc)
                if getattr(exc, "status_code", None) != 503:  # 503: Blob sign-in failed, reported once
                    logger.warning(
                        "Raw_Input blob list failed for %s (%s/%s): %s",
                        folder_id, container, prefix, exc,
                    )
                return None
        if page_index_1based < 1 or page_index_1based > len(blobs):
            return None
        return blobs[page_index_1based - 1]
