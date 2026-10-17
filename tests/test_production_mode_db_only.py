"""Production Mode reads Postgres and Azure Blob only: no workspace, no staging."""
from __future__ import annotations

from app.adapters.factory import build_repository
from app.core.config import Settings


def _production(tmp_path) -> Settings:
    (tmp_path / "chart1" / "staging").mkdir(parents=True)
    (tmp_path / "chart1" / "staging" / "extraction.json").write_text('{"pages": {}}', encoding="utf-8")
    return Settings(data_mode="production", database_url="postgresql://u:p@localhost/db", data_root=str(tmp_path))


def test_the_production_repository_has_no_local_workspace(tmp_path):
    repo = build_repository(_production(tmp_path))
    assert repo._optional_local() is None  # no disk fallback for charts, pages or images


def test_the_extraction_pane_reads_no_staging_in_production(tmp_path):
    from app.api.routes import get_folder_extraction

    result = get_folder_extraction("chart1", page="1.jpg", settings=_production(tmp_path))
    assert result.available is False and result.fields == []
