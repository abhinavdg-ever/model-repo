"""Where the charts come from. Standalone: nothing here imports core-pipeline.

  folder  a local folder of chart folders (core-pipeline/outputs/charts shape)
  db      Postgres: Tesseract text from ocr_results, page labels from imaging_final
  blob    Azure Blob: each chart's output prefix, the same files as a chart folder
          (<prefix>/<chart>/ocr/<chart>_prelim.txt, <prefix>/<chart>/imaging/<chart>_final.csv)

Each yields ingest.Chart objects, sorted by chart name. Credentials come from the
environment (.env), never from the command line, and are never printed.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterator, Optional

from chart_clustering.ingest import Chart, chart_from_pages, final_rows_from_csv, read_root, split_prelim

logger = logging.getLogger("chart_clustering.sources")


# ---- folder -------------------------------------------------------------------------------

def from_folder(root: Path, chart_list: Optional[list[str]] = None) -> Iterator[Chart]:
    yield from read_root(root, chart_list)


# ---- database -----------------------------------------------------------------------------

# One row per page: the Tesseract text (stage 2, every page) and the pipeline's final labels when
# the chart has been through imaging_final. Pages without Tesseract text come back with ''.
DB_PAGES_SQL = """
SELECT c.chart_name, p.page_name, p.page_number,
       COALESCE(o.raw_text, '')            AS raw_text,
       COALESCE(f.page_type, '')           AS page_type,
       COALESCE(f.blank_junk_flag, '')     AS blank_junk_flag,
       COALESCE(f.is_duplicate::text, '')  AS is_duplicate,
       COALESCE(f.printed_or_handwritten, '') AS printed_or_handwritten
  FROM chart_list c
  JOIN page_list p      ON p.chart_id = c.id
  LEFT JOIN ocr_results o ON o.page_id = p.id AND o.ocr_type = 'tesseract'
  LEFT JOIN imaging_final f ON f.page_id = p.id
 WHERE c.chart_name = ANY(%(names)s)
 ORDER BY c.chart_name, p.page_number
"""
DB_CHARTS_SQL = "SELECT chart_name FROM chart_list ORDER BY chart_name"


def _connect(database_url: str, schema: str):
    try:
        import psycopg  # psycopg 3

        conn = psycopg.connect(database_url)
    except ImportError:  # pragma: no cover - psycopg2 fallback
        import psycopg2

        conn = psycopg2.connect(database_url)
    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{schema}", public')
    return conn


def from_db(database_url: Optional[str] = None, schema: str = "public",
            chart_list: Optional[list[str]] = None, batch: int = 50) -> Iterator[Chart]:
    """Charts from Postgres (DATABASE_URL by default). ``chart_list`` limits them."""
    url = database_url or os.environ.get("DATABASE_URL") or ""
    if not url:
        raise SystemExit("DATABASE_URL is not set (put it in .env)")
    conn = _connect(url, schema)
    try:
        with conn.cursor() as cur:
            if chart_list:
                names = sorted(set(chart_list))
            else:
                cur.execute(DB_CHARTS_SQL)
                names = [str(r[0]) for r in cur.fetchall()]
        for start in range(0, len(names), batch):
            part = names[start:start + batch]
            with conn.cursor() as cur:
                cur.execute(DB_PAGES_SQL, {"names": part})
                rows = cur.fetchall()
            by_chart: dict[str, list[tuple]] = {}
            for row in rows:
                by_chart.setdefault(str(row[0]), []).append(row)
            for name in part:
                pages = by_chart.get(name) or []
                if not pages:
                    continue
                texts = [(str(r[1]), str(r[3] or "")) for r in pages]
                labels = {
                    str(r[1]): {"page_number": str(r[2] or ""), "page_type": r[4], "blank_junk_flag": r[5],
                                "is_duplicate": r[6], "printed_or_handwritten": r[7]}
                    for r in pages
                }
                # Labels only when imaging_final has rows for the chart.
                has_labels = any(r[4] or r[5] for r in pages)
                yield chart_from_pages(name, texts, labels if has_labels else {})
    finally:
        conn.close()


# ---- blob ---------------------------------------------------------------------------------

def _container_client(container: str):
    """AZURE_STORAGE_CONNECTION_STRING, else AZURE_STORAGE_ACCOUNT_NAME + _KEY, else the
    account name with DefaultAzureCredential (managed identity / az login)."""
    from azure.storage.blob import BlobServiceClient

    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn_str:
        service = BlobServiceClient.from_connection_string(conn_str)
    else:
        account = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME") or ""
        if not account:
            raise SystemExit("Set AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_NAME (in .env)")
        key = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
        if key:
            credential = key
        else:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
        service = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=credential)
    return service.get_container_client(container)


def _download_text(client, name: str) -> Optional[str]:
    try:
        return client.download_blob(name).readall().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - a missing optional file
        return None


def from_blob(container: str, prefix: str, chart_list: Optional[list[str]] = None) -> Iterator[Chart]:
    """Charts under ``<container>/<prefix>/<chart>/`` (e.g. prefix Processed/Run1): each needs
    ocr/<chart>_prelim.txt; imaging/<chart>_final.csv is read when present."""
    client = _container_client(container)
    base = prefix.strip("/").replace("\\", "/")
    base = f"{base}/" if base else ""
    wanted = set(chart_list) if chart_list else None
    names: set[str] = set()
    for blob in client.list_blobs(name_starts_with=base):
        rest = blob.name[len(base):]
        parts = rest.split("/")
        if len(parts) == 3 and parts[1] == "ocr" and parts[2] == f"{parts[0]}_prelim.txt":
            names.add(parts[0])
    for name in sorted(names):
        if wanted is not None and name not in wanted:
            continue
        prelim = _download_text(client, f"{base}{name}/ocr/{name}_prelim.txt")
        if not prelim:
            continue
        final = _download_text(client, f"{base}{name}/imaging/{name}_final.csv")
        yield chart_from_pages(name, split_prelim(prelim), final_rows_from_csv(final) if final else {})
    if wanted:
        missing = wanted - names
        if missing:
            logger.warning("%d listed chart(s) have no prelim text under %s", len(missing), base or "(root)")


def read_chart_list(path: Optional[str]) -> Optional[list[str]]:
    """A .txt of chart names, one per line (# comments, blank lines and a BOM ignored)."""
    if not path:
        return None
    names = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        name = line.split("#", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names
