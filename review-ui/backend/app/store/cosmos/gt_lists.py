"""Ground truth with several values on Cosmos (2026-11-14, user).

Provider Name (``rendering_provider``) and Provider Credentials (``provider_credentials``) are
stored comma separated in Postgres ("MD, NP"). In a Cosmos ``ground_truth`` document several
values are a JSON array (["MD", "NP"]); one value, and Not Found, stay text. Written by review-ui
(here), db-utilities (``dbutil/cosmos.py``) and the migration (``utilities/
migrate_postgres_to_cosmos.py``), each with the same small rule; read back as "MD, NP".
"""
from __future__ import annotations

from typing import Any

LIST_COLUMNS = ("rendering_provider", "provider_credentials")


def to_doc_value(value: Any) -> Any:
    """"MD, NP" -> ["MD", "NP"]; one value, Not Found and None as they are."""
    if not isinstance(value, str):
        return value
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts if len(parts) > 1 else value


def from_doc_value(value: Any) -> Any:
    """["MD", "NP"] -> "MD, NP"; anything else as it is."""
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if str(v).strip()) or None
    return value


def to_doc(labels: dict[str, Any]) -> dict[str, Any]:
    return {k: to_doc_value(v) if k in LIST_COLUMNS else v for k, v in labels.items()}


def from_doc(labels: dict[str, Any]) -> dict[str, Any]:
    return {k: from_doc_value(v) if k in LIST_COLUMNS else v for k, v in (labels or {}).items()}
