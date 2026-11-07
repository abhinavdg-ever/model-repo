"""schema/migrate.sql runs every file in schema/migrations/, in date order (2026-11-07)."""
from __future__ import annotations

import re
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[1] / "schema"


def test_migrate_lists_every_migration_in_date_order():
    listed = re.findall(r"^\\ir migrations/(\S+\.sql)$", (SCHEMA / "migrate.sql").read_text(encoding="utf-8"), re.M)
    on_disk = sorted(p.name for p in (SCHEMA / "migrations").glob("*.sql") if not p.name.startswith("._"))
    assert listed == on_disk


# Retired before anything read them: a stored accuracy table (accuracy is a view) and an
# encounter table that lived a few hours on 2026-10-25.
RETIRED_TABLES = {"accuracy_snapshot", "encounter_results"}


def test_migrations_never_drop_a_column_or_a_table_in_use():
    for path in (SCHEMA / "migrations").glob("[0-9]*.sql"):
        text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        assert not re.search(r"DROP\s+COLUMN\b", text, re.I), path.name
        dropped = set(re.findall(r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)", text, re.I))
        assert dropped <= RETIRED_TABLES, (path.name, dropped)
        assert not re.search(r"\bDELETE\s+FROM\b|\bTRUNCATE\b", text, re.I), path.name


def test_the_part_2_migration_matches_schema_sql_part_2():
    """migrations/2026-11-07.sql creates Part 2 on an existing database: same statements."""
    schema = (SCHEMA / "schema.sql").read_text(encoding="utf-8")
    part2 = schema[schema.rfind("\n", 0, schema.index("PART 2 — NOT IMPLEMENTED YET")):]
    migration = (SCHEMA / "migrations" / "2026-11-07.sql").read_text(encoding="utf-8")
    copied = migration[migration.index("-- >>> PART 2 START") : migration.index("-- <<< PART 2 END")]

    def statements(text):
        code = "\n".join(re.sub(r"--.*$", "", ln) for ln in text.splitlines())
        return [" ".join(s.split()) for s in code.split(";") if s.strip()]

    assert statements(copied) == statements(part2)
