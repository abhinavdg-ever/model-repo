"""chart-clustering (2026-11-15, visual + mix tracks 2026-11-17): unsupervised segments from
anchor pages; odd and undescribable charts are one Others cluster; no page text in the outputs."""
import csv
import json
import random
import sys
from pathlib import Path

import pytest

pytest.importorskip("sklearn")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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


def test_finds_the_templates_and_puts_the_rest_in_others(tmp_path):
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


def _draw(path: Path, kind: str, rng: random.Random) -> None:
    """A synthetic page: a header band (form), two text columns, or a ruled table."""
    from PIL import Image, ImageDraw

    im = Image.new("L", (850, 1100), 255)
    d = ImageDraw.Draw(im)
    jitter = lambda: rng.randint(-6, 6)  # noqa: E731
    if kind == "athena":
        d.rectangle([0, 0, 850, 140 + jitter()], fill=40)
        for y in range(200, 1000, 28):
            d.rectangle([60, y, 780 + jitter(), y + 10], fill=90)
    elif kind == "fax":
        for x0 in (50, 450):
            for y in range(80, 1050, 22):
                d.rectangle([x0, y, x0 + 330 + jitter(), y + 8], fill=70)
    else:
        for y in range(100, 1050, 60):
            d.line([40, y + jitter(), 810, y], fill=0, width=3)
        for x in range(40, 820, 155):
            d.line([x, 100, x, 1040], fill=0, width=3)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def _charts_with_images(root: Path, rng: random.Random) -> None:
    for kind, template in TEMPLATES.items():
        for i in range(8):
            name = f"{kind}_{i:02d}"
            _chart(root, name, template, rng)
            for page in ("1.jpg", "2.jpg", "3.jpg"):
                _draw(root / name / "pages" / page, kind, rng)


def test_visual_track_groups_by_page_look(tmp_path):
    """2026-11-17 (handoff): the visual track clusters the charts' page images alone."""
    pytest.importorskip("PIL")
    rng = random.Random(3)
    root, out = tmp_path / "charts", tmp_path / "out"
    _charts_with_images(root, rng)
    assert cli.main(["--source", "folder", "--root", str(root), "--out", str(out), "--track", "visual",
                     "--stability-runs", "0"]) == 0
    rows = {r["chart_name"]: r for r in csv.DictReader((out / "chart_clusters.csv").open(encoding="utf-8"))}
    by_kind = {k: {rows[f"{k}_{i:02d}"]["cluster"] for i in range(8)} for k in TEMPLATES}
    assert all(len(v) == 1 and "Others" not in v for v in by_kind.values())
    assert len({next(iter(v)) for v in by_kind.values()}) == 3
    assert rows["athena_00"]["images_used"] == "1"   # the anchor page's image
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["settings"]["track"] == "visual" and manifest["visual_used"] is True


def test_both_writes_text_and_visual_apart(tmp_path):
    """--track both -> out/text and out/visual, each a full result."""
    pytest.importorskip("PIL")
    rng = random.Random(4)
    root, out = tmp_path / "charts", tmp_path / "out"
    _charts_with_images(root, rng)
    assert cli.main(["--source", "folder", "--root", str(root), "--out", str(out), "--track", "both",
                     "--stability-runs", "0"]) == 0
    for track in ("text", "visual"):
        manifest = json.loads((out / track / "run_manifest.json").read_text(encoding="utf-8"))
        assert manifest["settings"]["track"] == track
        assert len([c for c in manifest["clusters"] if c != "Others"]) == 3


def test_mix_fuses_text_and_visual(tmp_path):
    pytest.importorskip("PIL")
    rng = random.Random(6)
    root, out = tmp_path / "charts", tmp_path / "out"
    _charts_with_images(root, rng)
    assert cli.main(["--source", "folder", "--root", str(root), "--out", str(out), "--stability-runs", "0"]) == 0
    rows = {r["chart_name"]: r for r in csv.DictReader((out / "chart_clusters.csv").open(encoding="utf-8"))}
    by_kind = {k: {rows[f"{k}_{i:02d}"]["cluster"] for i in range(8)} for k in TEMPLATES}
    assert all(len(v) == 1 and "Others" not in v for v in by_kind.values())
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["settings"]["track"] == "mix" and manifest["visual_used"] is True


def test_mix_without_images_falls_back_to_text_and_says_so(tmp_path):
    rng = random.Random(5)
    root, out = tmp_path / "charts", tmp_path / "out"
    for kind, template in TEMPLATES.items():
        for i in range(6):
            _chart(root, f"{kind}_{i:02d}", template, rng)
    assert cli.main(["--source", "folder", "--root", str(root), "--out", str(out), "--stability-runs", "0"]) == 0
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["settings"]["track"] == "mix" and manifest["visual_used"] is False


def test_db_images_come_from_raw_input_then_a_listing(tmp_path, monkeypatch):
    """--source db: {blob_path}/{page} first, then the Raw_Input folder's listing; cached."""
    from chart_clustering import images, sources
    from chart_clustering.ingest import chart_from_pages

    store = {"Raw_Input/Run1/Batch1/DEID_PNGs/a/1.jpg": b"A1",
             "Raw_Input/Run2/Batch3/DEID_PNGs/b/sub/2.jpg": b"B2"}
    calls = []

    class _Blob:
        def __init__(self, data):
            self.data = data

        def readall(self):
            return self.data

    class _Item:
        def __init__(self, name):
            self.name = name

    class _Client:
        def download_blob(self, name):
            calls.append(name)
            if name not in store:
                raise KeyError(name)
            return _Blob(store[name])

        def list_blobs(self, name_starts_with):
            return [_Item(n) for n in store if n.startswith(name_starts_with)]

    monkeypatch.setattr(sources, "_container_client", lambda container: _Client())
    a = chart_from_pages("a", [("1.jpg", "x")], {})
    a.blob_path = "Raw_Input/Run1/Batch1/DEID_PNGs/a"
    b = chart_from_pages("b", [("2.jpg", "y")], {})
    b.blob_path = "Raw_Input/Run2/Batch3/DEID_PNGs/b"
    fetcher = images.ImageFetcher(tmp_path / "cache", "db", workers=2)
    got = fetcher.fetch_all([(a, a.pages), (b, b.pages)])
    assert [p.read_bytes() for p in got[0]] == [b"A1"] and [p.read_bytes() for p in got[1]] == [b"B2"]
    calls.clear()
    fetcher.fetch_all([(a, a.pages)])          # cached: no download
    assert calls == []
