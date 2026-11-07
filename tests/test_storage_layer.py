"""The storage layer (2026-11-07, user: Postgres -> Cosmos for NoSQL).

Every read and write goes through ``common.db``. Each of its functions runs SQL on Postgres
or, for any other backend (``common.db.backend.StoreBackend``), the backend's method of the
same name. These tests hold that shape, so the Cosmos backend has a complete checklist:
``dispatched_functions()`` is every method it must implement.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
DB = CORE / "common" / "db" / "__init__.py"

# Postgres-only connectivity probes, kept on purpose until a backend ping() replaces them.
ALLOWED_RAW_SQL = {
    ("apps/orchestrator_api/main.py", 'cur.execute("SELECT count(*) FROM pipeline_stage")'),
    ("apps/orchestrator_api/main.py", 'conn.execute("SELECT 1")'),
}


def dispatched_functions() -> list[str]:
    tree = ast.parse(DB.read_text(encoding="utf-8"))
    out = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        decorated = any(isinstance(d, ast.Name) and d.id == "_dispatch" for d in node.decorator_list)
        # A few functions branch on _mem(conn) themselves and call conn.<same name>(...).
        routed = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_mem"
                     for n in ast.walk(node))
        if decorated or (routed and node.name != "_dispatch"):
            out.append(node.name)
    return out


def test_every_dispatched_function_has_a_memory_store_method():
    from common.db.memory_store import MemoryStore

    missing = [name for name in dispatched_functions() if not callable(getattr(MemoryStore, name, None))]
    assert not missing, f"MemoryStore lacks: {missing}"


def test_memory_store_is_a_store_backend():
    from common.db.backend import StoreBackend
    from common.db.memory_store import MemoryStore

    assert issubclass(MemoryStore, StoreBackend)


def test_no_sql_outside_common_db():
    """Stages, the API and jobs call common.db; only the listed Postgres probes run SQL."""
    found = set()
    for path in [*(CORE / "apps").rglob("*.py"), *(CORE / "modules").rglob("*.py"), CORE / "cli.py"]:
        if path.name.startswith("._"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.search(r"\b(conn|cur|cursor)\.execute\(", line):
                found.add((path.relative_to(CORE).as_posix(), line.strip()))
    assert found <= ALLOWED_RAW_SQL, f"SQL outside common.db: {sorted(found - ALLOWED_RAW_SQL)}"


def test_new_storage_functions_work_in_memory():
    from common.db import insert_run_usage, list_finished_chart_names, list_jobs
    from common.db.memory_store import MemoryStore

    store = MemoryStore()
    job = store.create_job(chart_id=None, stage_name="pipeline_full")
    assert [j["id"] for j in list_jobs(store)] == [job]
    insert_run_usage(store, {"seconds": 3, "stage_seconds": {"ocr_prelim": 1}}, ["seconds", "stage_seconds"])
    assert store.run_usage == [{"seconds": 3, "stage_seconds": {"ocr_prelim": 1}}]
    assert list_finished_chart_names(store, ["nope"], ["completed"]) == set()
