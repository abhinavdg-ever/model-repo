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


def test_file_viewer_lists_downloaded_charts_that_have_not_run(tmp_path):
    root = tmp_path / "folders"
    # Downloaded, not yet organised into pages/ or run: images at the folder root.
    at_root = root / "chart_c"
    at_root.mkdir(parents=True)
    (at_root / "1.png").write_bytes(b"one")
    (at_root / "2.png").write_bytes(b"two")
    # An empty pages/ does not hide images at the root either.
    empty_pages = root / "chart_d"
    (empty_pages / "pages").mkdir(parents=True)
    (empty_pages / "1.jpg").write_bytes(b"one")
    # Any image type core-pipeline downloads, not only jpg / png / tif.
    jp2 = root / "chart_e"
    (jp2 / "pages").mkdir(parents=True)
    (jp2 / "pages" / "1.jp2").write_bytes(b"jp2")

    repo = LocalFolderRepository(root, database_url="postgresql://user:pw@127.0.0.1:5432/db")
    listed = {item.name: item.page_count for item in repo.list_file_viewer_folders()}
    assert listed == {"chart_c": 2, "chart_d": 1, "chart_e": 1}
    assert [page.filename for page in repo.get_file_viewer_folder("chart_c").pages] == ["1.png", "2.png"]


def test_formats_a_browser_cannot_show_are_converted():
    from app.services.page_images import is_tiff_name

    assert all(is_tiff_name(f"1{suffix}") for suffix in (".tif", ".tiff", ".jp2", ".pnm", ".dib"))
    assert not any(is_tiff_name(f"1{suffix}") for suffix in (".jpg", ".png", ".gif", ".bmp", ".webp"))
