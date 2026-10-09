"""File Viewer reads data/folders/pages and can switch to corrected-pages."""
from __future__ import annotations

from app.adapters.local.repository import LocalFolderRepository


def test_file_viewer_lists_pages_folders_and_corrected_flag(tmp_path):
    root = tmp_path / "folders"
    with_pages = root / "chart_a"
    (with_pages / "pages").mkdir(parents=True)
    (with_pages / "pages" / "1.jpg").write_bytes(b"orig")
    (with_pages / "pages" / "2.jpg").write_bytes(b"orig2")
    (with_pages / "corrected-pages").mkdir()
    (with_pages / "corrected-pages" / "1.jpg").write_bytes(b"fixed")

    no_pages = root / "chart_b"
    no_pages.mkdir(parents=True)
    (no_pages / "notes.txt").write_text("x", encoding="utf-8")

    repo = LocalFolderRepository(root, database_url="postgresql://user:pw@127.0.0.1:5432/db")
    listed = repo.list_file_viewer_folders()
    assert [item.name for item in listed] == ["chart_a"]
    assert listed[0].page_count == 2
    assert listed[0].has_corrected is True

    detail = repo.get_file_viewer_folder("chart_a")
    assert [page.filename for page in detail.pages] == ["1.jpg", "2.jpg"]
    assert repo.get_file_viewer_image_path("chart_a", 1, corrected=False).read_bytes() == b"orig"
    assert repo.get_file_viewer_image_path("chart_a", 1, corrected=True).read_bytes() == b"fixed"
    assert repo.get_file_viewer_image_path("chart_a", 2, corrected=True).read_bytes() == b"orig2"


def test_results_open_on_pages_and_can_switch_to_corrected(tmp_path):
    root = tmp_path / "folders"
    chart = root / "chart_a"
    (chart / "pages").mkdir(parents=True)
    (chart / "pages" / "1.jpg").write_bytes(b"orig")
    (chart / "corrected-pages").mkdir()
    (chart / "corrected-pages" / "1.jpg").write_bytes(b"fixed")

    repo = LocalFolderRepository(root, database_url="postgresql://user:pw@127.0.0.1:5432/db")
    assert repo.get_page_image_path("chart_a", 1).read_bytes() == b"orig"
    assert repo.get_page_image_path("chart_a", 1, corrected=True).read_bytes() == b"fixed"
