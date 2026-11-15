"""chart_list.chart_cluster (2026-11-15, user): History's Cluster filter and the loader."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "review-ui" / "backend"))

from app.core.schemas import FolderSummary  # noqa: E402
from app.services.folder_list import FolderListParams, chart_summary, filter_sort_page  # noqa: E402


def _rows():
    return [
        FolderSummary(id="a", name="a", ocr_status="IMAGING_COMPLETED", chart_cluster="C01"),
        FolderSummary(id="b", name="b", ocr_status="IMAGING_COMPLETED", chart_cluster="Others"),
        FolderSummary(id="c", name="c", ocr_status="IMAGING_COMPLETED"),
    ]


def test_history_filters_on_cluster_with_a_blank_option():
    result = filter_sort_page(_rows(), FolderListParams(cluster=["C01"]))
    assert [r.name for r in result.items] == ["a"]
    assert result.cluster_options == ["C01", "Others", "(Blank)"]
    assert [r.name for r in filter_sort_page(_rows(), FolderListParams(cluster=["(Blank)"])).items] == ["c"]


def test_chart_summary_carries_the_cluster():
    row = chart_summary("x", 3, "completed", None, None, None, None, " C02 ")
    assert row.chart_cluster == "C02"
    assert chart_summary("x", 3, "completed", None, None, None, None).chart_cluster is None


def test_the_loader_reads_chart_clustering_output(tmp_path):
    spec = importlib.util.spec_from_file_location("load_chart_clusters", REPO / "utilities" / "load_chart_clusters.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    f = tmp_path / "chart_clusters.csv"
    f.write_text("﻿chart_name,cluster,membership\n52743839_44976074,C01,0.9\n1234, Others ,0\n9,,0\n",
                 encoding="utf-8")
    assert mod.read_file(f) == {"52743839_44976074": "C01", "1234": "Others", "9": ""}
