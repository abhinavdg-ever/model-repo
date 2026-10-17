"""Production Mode reads Postgres and Azure Blob only: no workspace, no staging."""
from __future__ import annotations

from app.adapters.factory import build_repository
from app.core.config import Settings


def _production(tmp_path) -> Settings:
    (tmp_path / "chart1" / "staging").mkdir(parents=True)
    (tmp_path / "chart1" / "staging" / "extraction.json").write_text('{"pages": {}}', encoding="utf-8")
    return Settings(data_mode="production", database_url="postgresql://u:p@localhost/db", data_root=str(tmp_path))


def test_the_production_repository_reads_no_data_from_disk(tmp_path):
    repo = build_repository(_production(tmp_path))
    assert repo._optional_local() is None  # no disk fallback for a chart or its page list


def test_production_still_serves_a_workspace_page_image(tmp_path):
    # Page images are the source document: Blob first, else the workspace copy.
    settings = _production(tmp_path)
    (tmp_path / "chart1" / "pages").mkdir()
    (tmp_path / "chart1" / "pages" / "1.png").write_bytes(b"png")
    repo = build_repository(settings)
    assert repo.get_page_image_path("chart1", 1).name == "1.png"


def test_the_extraction_pane_reads_no_staging_in_production(tmp_path):
    from app.api.routes import get_folder_extraction

    result = get_folder_extraction("chart1", page="1.jpg", settings=_production(tmp_path))
    assert result.available is False and result.fields == []
