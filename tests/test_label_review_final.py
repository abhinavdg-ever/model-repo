"""Confirmed pages land in training_final.csv every 10 Next clicks."""
from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "training" / "page-classification" / "label-review" / "server.py"
spec = importlib.util.spec_from_file_location("label_review_server", MODULE_PATH)
assert spec is not None and spec.loader is not None
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)


def _write_source(path: Path, count: int) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["page_id", "chart_id", "page_index", "page_family", "page_type"],
        )
        writer.writeheader()
        for index in range(1, count + 1):
            writer.writerow(
                {
                    "page_id": f"c_Pg{index}",
                    "chart_id": "c",
                    "page_index": str(index),
                    "page_family": "Progress Note",
                    "page_type": "Visit Report",
                }
            )


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _bind(tmp_path: Path, pages: int) -> Path:
    source = tmp_path / "training_page_classification.csv"
    final = tmp_path / "training_final.csv"
    _write_source(source, pages)
    server._images_root = tmp_path
    server._final_path = final
    server.load_rows(source)
    server.load_final(final)
    return source


def test_source_stays_put_until_ten_next_clicks(tmp_path: Path):
    source = _bind(tmp_path, 12)
    before = source.read_text(encoding="utf-8")
    for index in range(1, 10):
        result = server.confirm_page(f"c_Pg{index}", "Labs", "Lab Report", flush=False)
        assert result["flushed"] is False
    assert not server._final_path.is_file()
    tenth = server.confirm_page("c_Pg10", "Labs", "Lab Report", flush=False)
    assert tenth["flushed"] is True
    assert tenth["final_count"] == 10
    assert source.read_text(encoding="utf-8") == before
    rows = _read(server._final_path)
    assert [row["page_id"] for row in rows] == [f"c_Pg{index}" for index in range(1, 11)]
    assert rows[0]["page_family"] == "Labs"
    assert rows[0]["page_type"] == "Lab Report"


def test_last_page_flushes_the_remainder_and_upserts(tmp_path: Path):
    _bind(tmp_path, 3)
    server.confirm_page("c_Pg1", "Progress Note", "Visit Report", flush=False)
    server.confirm_page("c_Pg2", "Progress Note", "Visit Report", flush=False)
    done = server.confirm_page("c_Pg3", "Patient Demographics", "Face Sheet", flush=True)
    assert done["flushed"] is True
    assert done["final_count"] == 3
    again = server.confirm_page("c_Pg3", "Patient Demographics", "Insurance", flush=True)
    assert again["final_count"] == 3
    rows = _read(server._final_path)
    assert len(rows) == 3
    assert rows[-1]["page_type"] == "Insurance"


def test_a_family_can_be_chosen_as_the_page_type(tmp_path: Path):
    catalog = tmp_path / "types.csv"
    catalog.write_text(
        "page_type,page_family\nOffice Visit Note,Progress Note\n",
        encoding="utf-8",
    )
    server.load_type_catalog(catalog)
    by_type = {item["page_type"]: item["page_family"] for item in server._catalog}
    assert by_type["Office Visit Note"] == "Progress Note"
    assert by_type["Progress Note"] == "Progress Note"


def test_heic_scan_counts_as_an_image(tmp_path: Path):
    (tmp_path / "IMG_1.HEIC").write_bytes(b"not-an-image")
    server._images_root = tmp_path
    found = server.find_image({"chart_id": "x", "page_index": "1", "page_id": "IMG_1"})
    assert found is not None
    assert found.suffix.lower() == ".heic"


def test_restart_shows_labels_already_in_the_final_file(tmp_path: Path):
    source = _bind(tmp_path, 2)
    server.confirm_page("c_Pg1", "Labs", "Lab Report", flush=True)
    server.load_rows(source)
    server.load_final(server._final_path)
    assert server._rows[0]["page_family"] == "Labs"
    assert server._rows[0]["page_type"] == "Lab Report"
    assert server._rows[1]["page_family"] == "Progress Note"
