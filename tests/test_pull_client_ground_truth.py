"""utilities/pull_client_ground_truth.py (2026-11-08): the helpers that need no database.
The copy itself was run end to end on two throwaway Postgres 16 databases."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pull_gt", REPO / "utilities" / "pull_client_ground_truth.py")
pull = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pull)


def test_chart_list_reads_lines_commas_and_comments(tmp_path):
    f = tmp_path / "charts.txt"
    f.write_text("C1, C2\n# skip\nC3  # note\nC1\n", encoding="utf-8")
    assert pull.read_chart_list(str(f)) == ["C1", "C2", "C3"]


def test_users_are_matched_by_username():
    client_names = {1: "zed", 2: "alice"}
    ours = {"alice": 7}
    assert pull.map_user(2, client_names, ours) == 7
    assert pull.map_user(1, client_names, ours) is None      # no such user here
    assert pull.map_user(None, client_names, ours) is None


def test_every_review_ui_ground_truth_table_is_covered():
    import re
    schema = (REPO / "schema" / "schema.sql").read_text(encoding="utf-8")
    blocks = re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", schema, flags=re.S)
    keyed = {name for name, body in blocks if re.search(r"^\s+chart_name\s", body, flags=re.M)}
    review_tables = {t for t in keyed if "ground_truth" in t or "review" in t}
    assert review_tables == set(pull.CHART_TABLES)
    assert pull.TABLES.index("page_ground_truth") < pull.TABLES.index("page_ground_truth_entry")
