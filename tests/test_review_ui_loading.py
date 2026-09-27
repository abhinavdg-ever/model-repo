"""review-ui chart-open costs: page → blob resolution and header matching.

These pin the caching that keeps a filmstrip of N thumbnails from costing N
database queries and N blob probes, and keeps /ocr from re-scoring the same
header text against the whole canon list on every page.
"""
from __future__ import annotations

from contextlib import contextmanager


class _FakeCursor:
    def __init__(self, rows, log):
        self._rows = rows
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._log.append(sql)

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows, log):
        self._rows = rows
        self._log = log

    def cursor(self):
        return _FakeCursor(self._rows, self._log)


def _repo_with_pages(monkeypatch, n_pages):
    from app.adapters.postgres.repository import PostgresFolderRepository
    from app.services import blob_store

    rows = [
        ("imaging", "Raw_Input/R1/B1/chart", "Processed/chart", f"{i}.jpg", i, False, "")
        for i in range(1, n_pages + 1)
    ]
    queries: list[str] = []
    listings: list[str] = []

    repo = PostgresFolderRepository("postgresql://u:p@db:5432/x")

    @contextmanager
    def fake_connect():
        yield _FakeConn(rows, queries)

    monkeypatch.setattr(repo, "_connect", fake_connect)

    def fake_list(container, prefix):
        listings.append(prefix)
        return [(f"{prefix}/{i}.jpg", f"etag-{i}") for i in range(1, n_pages + 1)]

    monkeypatch.setattr(blob_store, "list_image_blobs", fake_list)

    def no_probe(*a, **k):
        raise AssertionError("Raw_Input keys come from a listing; no probe expected")

    monkeypatch.setattr(repo, "_first_existing_blob", no_probe)
    return repo, queries, listings


def test_every_page_of_a_chart_resolves_from_one_query_and_one_listing(monkeypatch):
    repo, queries, listings = _repo_with_pages(monkeypatch, 40)

    locs = [repo.resolve_page_blob("chart", n) for n in range(1, 41)]

    assert len(queries) == 1
    assert len(listings) == 1
    assert locs[0]["key"] == "Raw_Input/R1/B1/chart/1.jpg"
    assert locs[39]["etag"] == "etag-40"


def test_unknown_page_is_none_without_extra_queries(monkeypatch):
    repo, queries, _ = _repo_with_pages(monkeypatch, 3)
    repo.resolve_page_blob("chart", 1)
    assert repo.resolve_page_blob("chart", 99) is None
    assert len(queries) == 1


def test_derived_jpeg_cache_skips_the_producer_on_a_hit(tmp_path, monkeypatch):
    import tempfile

    from app.services import page_images

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    calls = []

    def produce():
        calls.append(1)
        return b"jpeg-bytes"

    stamp = "blob|c|k|etag-1|thumb"
    assert page_images.cached_derived_jpeg(stamp, produce) == b"jpeg-bytes"
    assert page_images.cached_derived_jpeg(stamp, produce) == b"jpeg-bytes"
    assert len(calls) == 1
    # A new ETag means new content — must re-produce.
    page_images.cached_derived_jpeg("blob|c|k|etag-2|thumb", produce)
    assert len(calls) == 2


def test_header_matching_is_memoised_per_text(monkeypatch):
    from app.adapters.local import repository as local_repo
    from app.core.schemas import OcrSectionHeader

    matcher = local_repo._load_header_matcher()
    assert matcher is not None
    local_repo._header_match_cache.clear()
    scored: list[str] = []
    real = matcher.best_header_match

    def counting(text, **kw):
        scored.append(text)
        return real(text, **kw)

    monkeypatch.setattr(matcher, "best_header_match", counting)
    headers = [
        OcrSectionHeader(text=t, level=2, left=0, top=0, width=0, height=0)
        for t in ["MEDICATIONS", "Patient was seen today for follow up."] * 50
    ]

    kept = local_repo._filter_headers_against_canon(headers)

    assert [h.text for h in kept] == ["Medications"] * 50
    assert len(scored) == 2
