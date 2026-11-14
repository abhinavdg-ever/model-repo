"""Incremental Postgres -> Cosmos sync (2026-11-14, user): the live mirror's failed list and
the sync script's bookkeeping (utilities/migrate_postgres_to_cosmos.py)."""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def migrate(monkeypatch):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)  # never read the real .env
    spec = importlib.util.spec_from_file_location("migrate_pg_cosmos", REPO / "utilities" / "migrate_postgres_to_cosmos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Conn:
    """Answers the chart-name lookups record_mirror_failure makes."""

    def __init__(self, charts=None, pages=None):
        self.charts, self.pages = charts or {}, pages or {}

    def execute(self, sql, params):
        class _R:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        value = params[0]
        if "FROM chart_list WHERE id" in sql:
            name = self.charts.get(value)
        else:
            name = self.pages.get(value)
        return _R({"chart_name": name} if name else None)


def test_a_failed_copy_is_recorded_with_its_chart(tmp_path, monkeypatch):
    from common.db import cosmos_store

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    conn = _Conn(charts={7: "chart_a"}, pages={70: "chart_b"})
    cosmos_store.record_mirror_failure("upsert_quality", conn, (), {"chart_id": 7, "page_id": 1})
    cosmos_store.record_mirror_failure("set_page_stage", conn, (), {"page_id": 70})
    cosmos_store.record_mirror_failure("upsert_chart", conn, (), {"chart_name": "chart_c"})
    cosmos_store.record_mirror_failure("mark_x", conn, (), {})
    lines = (tmp_path / cosmos_store.MIRROR_FAILED_FILE).read_text(encoding="utf-8").splitlines()
    assert [line.split("\t")[1:] for line in lines] == [
        ["chart_a", "upsert_quality"], ["chart_b", "set_page_stage"], ["chart_c", "upsert_chart"], ["?", "mark_x"]]


def test_the_log_purge_keeps_the_failed_list():
    from common import logging_setup

    assert "cosmos_mirror_failed.tsv" in logging_setup._KEEP_FILES


def test_failed_list_read_and_cleared(tmp_path, migrate):
    path = tmp_path / "cosmos_mirror_failed.tsv"
    path.write_text(
        "2026-11-14T09:00:00+00:00\tchart_a\tupsert_quality\n"
        "2026-11-14T09:05:00+00:00\t?\tmark_x\n"
        "2026-11-14T11:00:00+00:00\tchart_a\tset_page_stage\n"   # after the sync started: kept
        "2026-11-14T09:10:00+00:00\tchart_b\tupsert_dos\n",
        encoding="utf-8")
    names, unknown = migrate.read_failed(path)
    assert names == {"chart_a", "chart_b"} and unknown == 1
    assert migrate.clear_failed(path, {"chart_a", "chart_b"}, "2026-11-14T10:00:00+00:00") == 2
    left = path.read_text(encoding="utf-8").splitlines()
    assert [line.split("\t")[1] for line in left] == ["?", "chart_a"]
    assert migrate.read_failed(tmp_path / "missing.tsv") == (set(), 0)


def test_since_last_state_round_trip(tmp_path, migrate, monkeypatch):
    monkeypatch.setattr(migrate, "STATE_FILE", tmp_path / "state.json")
    assert migrate.load_state() == {}
    migrate.save_state("2026-11-14T10:00:00+00:00", 3)
    assert migrate.load_state() == {"last_sync_started": "2026-11-14T10:00:00+00:00", "charts": 3}


def test_since_last_needs_an_earlier_sync(tmp_path, migrate, monkeypatch, capsys):
    monkeypatch.setattr(migrate, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setenv("DB_BACKEND", "postgres")  # main() sets it; restored after the test

    class _Fake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            class _R:
                def fetchone(self):
                    from datetime import datetime, timezone

                    return {"t": datetime(2026, 11, 14, tzinfo=timezone.utc)}

            return _R()

    import common.db

    monkeypatch.setattr(common.db, "connect", lambda: _Fake())
    assert migrate.main(["--since-last", "--dry-run"]) == 2
    assert "Run --all once" in capsys.readouterr().out


def test_chart_list_file_is_read(tmp_path, migrate):
    """--chart-list (2026-11-15, user): one name per line; blanks, # comments, commas, a BOM ok."""
    f = tmp_path / "charts.txt"
    f.write_text("\ufeff52743839_44976074\r\n\n# a comment\n28394399_95601927, 34551225_32782696 # x\n"
                 "52743839_44976074\n", encoding="utf-8")
    assert migrate.read_chart_list(f) == ["52743839_44976074", "28394399_95601927", "34551225_32782696"]


def test_chart_list_migrates_only_those_charts(tmp_path, migrate, monkeypatch, capsys):
    monkeypatch.setenv("DB_BACKEND", "postgres")
    f = tmp_path / "charts.txt"
    f.write_text("A\nB\nC\n", encoding="utf-8")

    class _Fake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            class _R:
                def fetchone(self):
                    from datetime import datetime, timezone

                    return {"t": datetime(2026, 11, 15, tzinfo=timezone.utc)}

            return _R()

    import common.db

    monkeypatch.setattr(common.db, "connect", lambda: _Fake())
    asked = []
    # Postgres has pipeline runs for A and B; C has ground truth only.
    monkeypatch.setattr(migrate, "rows", lambda conn, sql, params=(): asked.append(params) or
                        [{"chart_name": n} for n in ("A", "B") if n in params[0]])
    copied = []
    monkeypatch.setattr(migrate, "migrate_chart", lambda conn, store, chart, dry_run: copied.append(chart["chart_name"])
                        or (True, {"pages": 1}, {"pages": 1}))
    extra = {}
    monkeypatch.setattr(migrate, "migrate_manifest_and_ground_truth", lambda conn, store, names, dry_run: extra.update(
        names=names) or {"manifest_rows": 0, "ground_truth_pages": 0, "chart_reviews": 0})
    monkeypatch.setattr(migrate, "migrate_deletions", lambda *a, **k: pytest.fail("no deletions for a chart list"))
    assert migrate.main(["--chart-list", str(f), "--dry-run"]) == 0
    assert copied == ["A", "B"] and extra["names"] == ["A", "B", "C"]
    assert "manifest / ground truth only" in capsys.readouterr().out


def test_chart_list_or_a_choice_is_required(tmp_path, migrate):
    with pytest.raises(SystemExit):
        migrate.main([])
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        migrate.main(["--chart-list", str(empty)])


class _Container:
    def __init__(self, ids):
        self.ids, self.deleted = list(ids), []

    def query_items(self, sql, partition_key):
        return [{"id": i} for i in self.ids]

    def delete_item(self, doc_id, partition_key):
        self.deleted.append((partition_key, doc_id))


def test_prune_removes_only_what_postgres_no_longer_has(migrate):
    """2026-11-14 (user): upsert first, then drop the partition's leftover documents."""
    container = _Container(["page-1", "page-2", "page-9"])
    assert migrate.prune(container, "chart_a", {"page-1", "page-2"}) == 1
    assert container.deleted == [("chart_a", "page-9")]


def test_chart_review_doc_from_postgres(migrate):
    class _Conn:
        def __init__(self):
            self.answers = {
                "chart_review": [{"user_id": 3, "created_at": "t", "updated_at": "t"}],
                "chart_ground_truth": [],
                "encounter_review": [{"seq": 1, "start_page": 1, "end_page": 4}],
                "chart_review_entry": [],
            }

        def execute(self, sql, params):
            table = next(t for t in ("chart_review_entry", "chart_review", "chart_ground_truth", "encounter_review")
                         if f"FROM {t} " in sql)

            class _R:
                def __init__(self, rows):
                    self.rows = rows

                def fetchall(self):
                    return self.rows

            return _R(self.answers[table])

    migrate._TABLES = {"chart_review", "chart_ground_truth", "encounter_review", "chart_review_entry"}
    doc = migrate.chart_review_doc(_Conn(), "chart_a")
    assert doc["id"] == "chart" and doc["chart_name"] == "chart_a"
    assert doc["chart_review"]["user_id"] == 3 and doc["chart_ground_truth"] is None
    assert doc["encounter_review"] == [{"seq": 1, "start_page": 1, "end_page": 4}] and doc["review_entries"] == []


def test_imaging_final_uses_ground_truth_names_in_cosmos():
    """2026-11-14 (user): imaging_final in a Cosmos page doc carries the ground truth's names;
    core-pipeline writes them, review-ui reads them back; both tables are the same."""
    from common.db import cosmos_store
    from app.store.cosmos import final_names

    assert cosmos_store.FINAL_TO_COSMOS == final_names.FINAL_TO_COSMOS
    row = {"provider_name": "Dr A", "codability": "Codable", "seq": 3, "page_type": "Progress Note",
           "encounter_type": "Outpatient (F2F)", "orientation_angle": 90}
    doc = cosmos_store.final_to_cosmos(row)
    assert doc == {"rendering_provider": "Dr A", "codeable": "Codable", "page_sequence": 3,
                   "page_type": "Progress Note", "encounter_type": "Outpatient (F2F)", "rotation": 90}
    assert cosmos_store.final_from_cosmos(doc) == row == final_names.from_cosmos(doc)
    # A document written before the rename reads as it is.
    assert final_names.from_cosmos(row) == row and cosmos_store.final_from_cosmos(row) == row
