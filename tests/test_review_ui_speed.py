"""review-ui chart-open speed-ups keep their answers: the OCR text cache and the parallel
imaging overlay."""
from __future__ import annotations

from contextlib import contextmanager

from app.adapters.postgres import repository as repo_mod
from app.adapters.postgres.repository import PostgresFolderRepository
from app.core.schemas import OcrTextResponse


class _Cursor:
    def __init__(self, answer):
        self.answer, self.rows = answer, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.rows = self.answer(sql, params)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, answer):
        self.answer = answer

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _Cursor(self.answer)


def _repo(monkeypatch, answer):
    repo = PostgresFolderRepository("postgresql://u:p@db:5432/x")
    monkeypatch.setattr(repo, "_connect", lambda: _Conn(answer))
    return repo


def test_ocr_text_is_rebuilt_only_when_its_rows_change(monkeypatch):
    repo_mod._OCR_TEXT_CACHE.clear()
    state = {"fingerprint": (3, "t1", 3, "t1", None)}
    repo = _repo(monkeypatch, lambda sql, params: [state["fingerprint"]])
    built = []

    def build(folder_id, kind):
        built.append(kind)
        return OcrTextResponse(folder_id=folder_id, kind="final1", text=f"text {len(built)}")

    monkeypatch.setattr(repo, "_build_ocr_text", build)
    first = repo.get_ocr_text("c1", "final1")
    again = repo.get_ocr_text("c1", "final1")
    assert built == ["final1"] and again is first
    state["fingerprint"] = (3, "t1", 3, "t2", None)  # a page re-OCR'd
    changed = repo.get_ocr_text("c1", "final1")
    assert len(built) == 2 and changed.text == "text 2"


def test_one_failing_overlay_query_skips_only_its_part(monkeypatch):
    def answer(sql, params):
        if "to_regclass" in sql:
            return [(params[0] != "imaging_final",)]  # an older database: no imaging_final
        if "FROM member_extraction_results" in sql:
            raise RuntimeError("column missing")
        return []

    monkeypatch.setattr(repo_mod.db, "_table_exists_cache", {})
    monkeypatch.setattr(repo_mod.db, "connection", lambda url, schema: _Conn(answer))
    repo = _repo(monkeypatch, answer)
    rows = repo._overlay_rows("c1")
    assert set(rows) == set(repo_mod._OVERLAY_SQL)
    assert rows["member"] is None and rows["imaging_final"] == [] and rows["quality"] == []
