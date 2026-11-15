"""chart-clustering/ (first cut, 2026-11-15): unsupervised segments from anchor pages; odd and
undescribable charts are one Others cluster; no page text in the outputs."""
import csv
import json
import random
import sys
from pathlib import Path

import pytest

pytest.importorskip("sklearn")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chart-clustering"))
from chart_clustering import cli  # noqa: E402

TEMPLATES = {
    "athena": "athena Kreptowski Family Practice\nChief Complaint: {w}\nHPI: {w} {w}\nReviewed Medications\n"
              "Allergies: {w}\nVitals: BP {n}/{n}\nAssessment: {w}\nPlan: {w}\nPatient's Care Team: {w}\n",
    "fax": "Fax 555 {n} Northeast Endocrinology Page 2 of 5\nSubjective\nCC: {w}\nROS: Const denies {w}\n"
           "Objective\nExam: {w} {w}\nLab Acquired: {n}\nComp Metabolic Panel glucose {n}\n",
    "hospital": "Island Hospital *Live* PROGRESS NOTE\nAdmission Date: {n}\nHospital Course: {w} {w}\n"
                "Physical Examination: {w}\nImpression: {w}\nDischarge Plan: {w}\nAttending Physician: {w}\n",
}


def _chart(root: Path, name: str, template: str, rng: random.Random) -> None:
    words = lambda: " ".join(f"zq{rng.randint(0, 10**9)}" for _ in range(6))  # one-off "patient" words
    def fill(t: str) -> str:
        t = t.replace("{n}", str(rng.randint(10, 99)))
        while "{w}" in t:
            t = t.replace("{w}", words(), 1)
        return t
    pages = [f"===== {i}.jpg =====\n{fill(template)}" for i in range(1, 4)]
    folder = root / name / "ocr"
    folder.mkdir(parents=True)
    (folder / f"{name}_prelim.txt").write_text("\n".join(pages), encoding="utf-8")


def test_first_cut_finds_the_templates_and_puts_the_rest_in_others(tmp_path):
    rng = random.Random(1)
    root, out = tmp_path / "charts", tmp_path / "out"
    for kind, template in TEMPLATES.items():
        for i in range(8):
            _chart(root, f"{kind}_{i:02d}", template, rng)
    for i in range(2):  # odd charts: their own words only
        _chart(root, f"odd_{i}", "\n".join("zz" + str(rng.random()) for _ in range(30)), rng)
    (root / "empty" / "ocr").mkdir(parents=True)
    (root / "empty" / "ocr" / "empty_prelim.txt").write_text("===== 1.jpg =====\n\n", encoding="utf-8")

    assert cli.main(["--source", "folder", "--root", str(root), "--out", str(out), "--stability-runs", "2"]) == 0
    rows = {r["chart_name"]: r for r in csv.DictReader((out / "chart_clusters.csv").open(encoding="utf-8"))}
    by_kind = {k: {rows[f"{k}_{i:02d}"]["cluster"] for i in range(8)} for k in TEMPLATES}
    assert all(len(v) == 1 and "Others" not in v for v in by_kind.values())
    assert len({next(iter(v)) for v in by_kind.values()}) == 3          # three different clusters
    assert rows["empty"]["cluster"] == "Others" and rows["odd_0"]["cluster"] == "Others"
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["charts"] == 27 and "Others" in manifest["clusters"]
    # No page text: the one-off words never reach the outputs.
    for name in ("chart_clusters.csv", "cluster_summary.csv", "cluster_cards.md"):
        assert "zq" not in (out / name).read_text(encoding="utf-8")
    summary = (out / "cluster_summary.csv").read_text(encoding="utf-8").lower()
    assert "athena" in summary and "hospital course:" in summary   # template words and field labels


def test_blob_fetches_listed_charts_across_runs(monkeypatch):
    """2026-11-15 (user): a chart list across runs / batches: each chart is fetched from its own
    output path (chart_list) or the first --prefix that has it; the rest are reported missing."""
    from chart_clustering import sources

    files = {
        "Processed/Run1/a/ocr/a_prelim.txt": "===== 1.jpg =====\nChief Complaint: x",
        "Processed/Run2/b/ocr/b_prelim.txt": "===== 1.jpg =====\nPlan: y",
        "Processed/Run7/B3/c/ocr/c_prelim.txt": "===== 1.jpg =====\nHPI: z",
    }

    class _Blob:
        def __init__(self, text):
            self.text = text

        def readall(self):
            return self.text.encode()

    class _Client:
        def download_blob(self, name):
            if name not in files:
                raise KeyError(name)
            return _Blob(files[name])

    monkeypatch.setattr(sources, "_container_client", lambda container: _Client())
    missing: list[str] = []
    charts = list(sources.from_blob("imaging-pipeline", ["Processed/Run1", "Processed/Run2"], ["a", "b", "c", "d"],
                                    paths={"c": ("", "Processed/Run7/B3/c")}, missing=missing))
    assert [c.name for c in charts] == ["a", "b", "c"] and missing == ["d"]


def test_up_to_three_progress_notes_are_the_anchor():
    """2026-11-15 (user): the first page of each of the first 2-3 Progress Notes, not one page."""
    from chart_clustering import anchors
    from chart_clustering.ingest import chart_from_pages

    note = "Chief Complaint: cough\nHPI: three days of cough and fever\nAssessment: bronchitis\nPlan: rest fluids recheck"
    lab = "Comprehensive metabolic panel glucose sodium potassium chloride result reference range units flag"
    pages = [("1.jpg", lab), ("2.jpg", note), ("3.jpg", note + " page two"), ("4.jpg", lab),
             ("5.jpg", note), ("6.jpg", lab), ("7.jpg", note), ("8.jpg", lab), ("9.jpg", note)]
    chart = chart_from_pages("x", pages, {})
    assert [a.page.name for a in anchors.progress_note_starts(chart, 3)] == ["2.jpg", "5.jpg", "7.jpg"]
    # With the pipeline's documents: one anchor per document.
    rows = {n: {"page_type": "Progress Note" if t == note or t.startswith(note) else "Lab Report",
                "document_seq": d, "blank_junk_flag": "not_blank_junk"}
            for (n, t), d in zip(pages, ["1", "2", "2", "3", "4", "5", "6", "7", "8"])}
    chart = chart_from_pages("x", pages, rows)
    assert [a.page.name for a in anchors.progress_note_starts(chart, 2)] == ["2.jpg", "5.jpg"]
