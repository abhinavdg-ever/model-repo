"""utilities/clone_client_db.py copies every chart-linked table and never the ground truth."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("clone_client_db", REPO / "utilities" / "clone_client_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tables() -> dict[str, str]:
    sql = (REPO / "schema" / "v1.sql").read_text(encoding="utf-8")
    return {m.group(1): m.group(2) for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.S)}


def test_every_chart_linked_table_is_copied():
    linked = {name for name, body in _tables().items() if re.search(r"^\s+chart_id\s", body, re.M)}
    assert linked == set(_script().CHART_TABLES)


def test_ground_truth_is_never_copied():
    copied = set(_script().CHART_TABLES) | {"chart_list", "manifest_member_list", "users"}
    assert not copied & {"page_ground_truth", "page_ground_truth_entry", "encounter_review", "user_login", "chart_deletion"}
    assert {"page_ground_truth", "page_ground_truth_entry"} <= set(_tables())


def test_a_chart_list_file_reads_one_name_per_line(tmp_path):
    """2026-10-27 (user): move the charts named in a notepad file."""
    path = tmp_path / "charts.txt"
    path.write_text("﻿52743839_44976074\n\n# a comment\n45726104_1, 45726104_2  # trailing note\n52743839_44976074\n",
                    encoding="utf-8")
    assert _script().read_chart_list(str(path)) == ["52743839_44976074", "45726104_1", "45726104_2"]


def test_the_date_and_the_list_both_filter():
    from datetime import datetime, timezone

    script = _script()
    cutoff = datetime(2026, 10, 26, tzinfo=timezone.utc)
    assert script.chart_filter(cutoff, None) == ("updated_at >= %s", (cutoff,))
    assert script.chart_filter(None, ["a"]) == ("chart_name = ANY(%s)", (["a"],))
    assert script.chart_filter(cutoff, ["a"]) == ("updated_at >= %s AND chart_name = ANY(%s)", (cutoff, ["a"]))
    assert script.chart_filter(None, None) == ("TRUE", ())


def test_a_list_of_chart_ids(tmp_path):
    """2026-10-27 (user): batches by chart_id as well as by chart name."""
    import pytest

    script = _script()
    path = tmp_path / "ids.txt"
    path.write_text("12\n7, 12\n", encoding="utf-8")
    assert script.read_chart_ids(str(path)) == [12, 7]
    assert script.chart_filter(None, [12, 7], by_id=True) == ("id = ANY(%s)", ([12, 7],))
    path.write_text("12\n52743839_44976074\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        script.read_chart_ids(str(path))
