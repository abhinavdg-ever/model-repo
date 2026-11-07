"""utilities/migrate_postgres_to_cosmos.py covers every table CosmosStore keeps per page."""
from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

from common.db import cosmos_store

SPEC = importlib.util.spec_from_file_location(
    "migrate_postgres_to_cosmos",
    Path(__file__).resolve().parents[1] / "utilities" / "migrate_postgres_to_cosmos.py")
migrate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migrate)


def test_every_page_table_of_the_cosmos_store_is_migrated():
    migrated = {attr for attr, _ in migrate.PAGE_TABLES.values()}
    assert migrated == set(cosmos_store.PAGE_FIELDS) | set(cosmos_store.TUPLE_FIELDS)


def test_keys_match_the_store():
    row = {"page_id": 7, "ocr_type": "tesseract", "pass_no": 2, "stage_name": "dos_extract"}
    assert migrate.PAGE_TABLES["ocr_results"][1](row) == (7, "tesseract")
    assert migrate.PAGE_TABLES["blank_junk_classification"][1](row) == (7, 2)
    assert migrate.PAGE_TABLES["page_stage_status"][1](row) == (7, "dos_extract", 2)
    assert migrate.PAGE_TABLES["dos_extraction_results"][1](row) == 7


def test_decimals_become_floats():
    assert migrate.plain({"a": Decimal("0.9500"), "b": [Decimal("1.5")]}) == {"a": 0.95, "b": [1.5]}
