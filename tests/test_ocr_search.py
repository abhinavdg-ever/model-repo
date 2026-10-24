"""review-ui Search (admins): a word in the pages' Tesseract text, by batch and chart."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.services import auth, ocr_search


def test_whole_word_and_phrases_across_line_breaks():
    _, pg, py = ocr_search.patterns("date of service", whole_word=True)
    assert pg == r"\mdate\s+of\s+service\M"
    assert py.search("DOS: Date of\nService 03/14") and not py.search("dates of services")
    _, _, contains = ocr_search.patterns("pain", whole_word=False)
    assert contains.search("painful") and not ocr_search.patterns("pain", whole_word=True)[2].search("painful")


def test_special_characters_are_literal():
    like, pg, py = ocr_search.patterns("a/p 50%", whole_word=False)
    assert pg == r"a\/p\s+50\%" and like == r"%a/p%"
    assert py.search("A/P 50% better") and not py.search("axp 50x")


def test_snippets_count_every_hit_and_show_a_few():
    _, _, py = ocr_search.patterns("pain", whole_word=True)
    count, shown = ocr_search.snippets("pain " * 5 + "x" * 100, py, limit=3)
    assert count == 5 and len(shown) == 3 and shown[0]["match"] == "pain"
    assert shown[0]["after"].endswith("…")


class FakeOcr:
    def __init__(self, rows):
        self.rows, self.sql, self.params = rows, "", []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.sql, self.params = sql, list(params)

    def fetchall(self):
        return self.rows


def _patch(monkeypatch, rows):
    fake = FakeOcr(rows)

    @contextmanager
    def connection(url, schema):
        yield fake

    monkeypatch.setattr(ocr_search, "connection", connection)
    return fake


def test_results_group_by_batch_then_chart(monkeypatch):
    fake = _patch(monkeypatch, [
        ("c1", "R1", "B1", 1, "1.jpg", "Hypertension, stable."),
        ("c1", "R1", "B1", 3, "3.jpg", "hypertension x2 hypertension"),
        ("c2", None, None, 2, "2.jpg", "history of hypertension"),
        ("c3", "R1", "B1", 4, "4.jpg", "hypertensive"),  # the database matched; whole word does not
    ])
    found = ocr_search.search_tesseract("db", "public", "  hypertension ", run_id="R1")
    assert "AND c.run_id = %s" in fake.sql and fake.params[2] == "R1"
    assert found["query"] == "hypertension"
    assert [(c["chart"], c["run"], c["batch"], c["hits"]) for c in found["charts"]] == [
        ("c1", "R1", "B1", 3), ("c2", "Test", "Test", 1)]
    assert [(b["run"], b["batch"], b["charts"], b["pages"]) for b in found["batches"]] == [
        ("R1", "B1", 1, 2), ("Test", "Test", 1, 1)]
    assert (found["total_pages"], found["total_hits"], found["truncated"]) == (3, 4, False)


def test_a_one_letter_query_is_refused():
    with pytest.raises(ocr_search.SearchError) as short:
        ocr_search.search_tesseract("db", "public", " a ")
    assert short.value.status == 400


def test_only_an_admin_searches(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.core.config import get_settings
    from app.main import app

    s = Settings(database_url="postgresql://u:p@db.example:5432/x", auth_secret="test-secret")
    app.dependency_overrides[get_settings] = lambda: s
    monkeypatch.setattr(routes, "database_url_usable", lambda url: True)
    _patch(monkeypatch, [("c1", "R1", "B1", 1, "1.jpg", "chest pain")])

    def bearer(role: str) -> dict:
        token = auth.issue_token(s, auth.SignedInUser(id=9, username="u", name="U", role=role))
        return {"Authorization": f"Bearer {token}"}

    try:
        client = TestClient(app)
        assert client.get("/api/search/ocr?q=pain").status_code == 401
        assert client.get("/api/search/ocr?q=pain", headers=bearer("reviewer")).status_code == 403
        ok = client.get("/api/search/ocr?q=pain", headers=bearer("admin"))
        assert ok.status_code == 200 and ok.json()["total_pages"] == 1
    finally:
        app.dependency_overrides.clear()


def test_a_page_with_no_blob_says_why():
    from app.adapters.postgres.repository import PostgresFolderRepository

    repo = PostgresFolderRepository("postgresql://u:p@db:5432/x")
    repo._no_blob("c1", 2, "not in imaging-pipeline/Processed/R1/c1/pages/2.jpg")
    repo._no_blob("c1", 3, "no page_list row for this page")
    assert repo.no_blob_reason("c1", 2) == "not in imaging-pipeline/Processed/R1/c1/pages/2.jpg"
    assert repo.no_blob_reason("c1", 3) == "no page_list row for this page"
    assert repo.no_blob_reason("c1", 4) is None
