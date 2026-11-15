"""Write each chart's cluster to chart_list.chart_cluster (--write-db, 2026-11-15).

Needs schema/migrations/2026-11-15.sql. Writing a cluster is not a run, so chart_list.updated_at
(review-ui's Last Run At) is kept: the updated_at trigger is paused inside the one transaction,
as utilities/tag_chart.py does, which needs the table owner's rights.
"""
from __future__ import annotations

import os
from typing import Optional

TRIGGER = "trg_chart_list_updated_at"


def write_clusters(clusters: dict[str, str], database_url: Optional[str] = None, schema: str = "public") -> int:
    """chart name -> cluster label. Returns the rows updated."""
    from chart_clustering.sources import _connect

    url = database_url or os.environ.get("DATABASE_URL") or ""
    if not url:
        raise SystemExit("--write-db needs DATABASE_URL (in .env)")
    conn = _connect(url, schema)
    updated = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = 'chart_list' AND column_name = 'chart_cluster'",
                        (schema,))
            if cur.fetchone() is None:
                raise SystemExit("chart_list.chart_cluster is missing: run schema/migrate.sql (2026-11-15) first")
            try:
                cur.execute(f"ALTER TABLE chart_list DISABLE TRIGGER {TRIGGER}")
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                raise SystemExit("not allowed to pause chart_list's updated_at trigger: run as the table owner") from exc
            for name, label in sorted(clusters.items()):
                cur.execute("UPDATE chart_list SET chart_cluster = %s WHERE chart_name = %s", (label, name))
                updated += cur.rowcount or 0
            cur.execute(f"ALTER TABLE chart_list ENABLE TRIGGER {TRIGGER}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return updated
