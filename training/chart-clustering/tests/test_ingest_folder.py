"""Step 1: a folder in outputs/charts format -> pages.csv / charts.csv, no text kept."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import ingest_folder  # noqa: E402
from chart_types.folder import split_pages, text_traits  # noqa: E402

SECRET = "Johnathan Q Sample"


def _chart(root: Path, name: str, *, with_csv: bool) -> None:
    (root / name / "ocr").mkdir(parents=True)
    (root / name / "ocr" / f"{name}_prelim.txt").write_text(
        f"===== 1.jpg =====\nPatient: {SECRET}\nChief Complaint: cough\n\n"
        "===== 2.jpg =====\nFax from: clinic\nAssessment: stable\n", encoding="utf-8")
    if with_csv:
        (root / name / "imaging").mkdir()
        with (root / name / "imaging" / f"{name}_final.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["page_name", "page_type", "specialty"])
            w.writeheader()
            w.writerow({"page_name": "1.jpg", "page_type": "Progress Note > Office Visit", "specialty": "Urology"})


def test_split_pages():
    assert split_pages("junk\n===== 1.jpg =====\na\n===== 2.jpg =====\nb\n") == {"1.jpg": "a", "2.jpg": "b"}


def test_text_traits_counts_label_and_fax_lines():
    t = text_traits("Fax from: x\nName: y\nfree text here\n")
    assert t["n_lines"] == 3 and t["fax_top"] == 1 and t["label_line_share"] > 0.6


def test_ingest_reads_any_folder_and_keeps_no_text(tmp_path):
    root, out = tmp_path / "charts", tmp_path / "work"
    _chart(root, "111_222", with_csv=True)
    _chart(root, "333_444", with_csv=False)
    (root / "empty_chart").mkdir()
    assert ingest_folder.main(["--root", str(root), "--out", str(out)]) == 0

    pages = list(csv.DictReader((out / "pages.csv").open(encoding="utf-8")))
    charts = {r["chart_name"]: r for r in csv.DictReader((out / "charts.csv").open(encoding="utf-8"))}
    assert set(charts) == {"111_222", "333_444"}
    assert charts["111_222"]["pages_with_labels"] == "1" and charts["111_222"]["progress_note_pages"] == "1"
    first = next(p for p in pages if p["chart_name"] == "111_222" and p["page_name"] == "1.jpg")
    assert first["labels_from"] == "pipeline_csv" and first["specialty"] == "Urology"
    for f in out.iterdir():
        assert SECRET not in f.read_text(encoding="utf-8")
