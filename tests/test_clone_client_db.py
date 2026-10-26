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
