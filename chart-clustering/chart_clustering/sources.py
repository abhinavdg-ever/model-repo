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
import threading
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
       COALESCE(f.printed_or_handwritten, '') AS printed_or_handwritten,
       COALESCE(f.document_seq::text, '')  AS document_seq
  FROM chart_list c
  JOIN page_list p      ON p.chart_id = c.id
  LEFT JOIN ocr_results o ON o.page_id = p.id AND o.ocr_type = 'tesseract'
  LEFT JOIN imaging_final f ON f.page_id = p.id
 WHERE c.chart_name = ANY(%(names)s)
 ORDER BY c.chart_name, p.page_number
"""
DB_CHARTS_SQL = "SELECT chart_name FROM chart_list ORDER BY chart_name"
# Where each chart's page images are: blob_path is the Raw_Input chart folder
# (Raw_Input/Run1/Batch1/DEID_PNGs/<chart>), output_path the Processed one.
DB_PATHS_SQL = """
SELECT chart_name, COALESCE(blob_container, ''), COALESCE(blob_path, ''), COALESCE(output_path, '')
  FROM chart_list WHERE chart_name = ANY(%(names)s)
"""


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
            chart_list: Optional[list[str]] = None, batch: int = 50,
            missing: Optional[list[str]] = None) -> Iterator[Chart]:
    """Charts from Postgres (DATABASE_URL by default). ``chart_list`` limits them; the charts can
    come from any runs / batches (looked up by name). Listed charts with no pages go to ``missing``."""
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
                cur.execute(DB_PATHS_SQL, {"names": part})
                where = {str(r[0]): (str(r[1]), str(r[2]), str(r[3])) for r in cur.fetchall()}
            by_chart: dict[str, list[tuple]] = {}
            for row in rows:
                by_chart.setdefault(str(row[0]), []).append(row)
            for name in part:
                pages = by_chart.get(name) or []
                if not pages:
                    if missing is not None:
                        missing.append(name)
                    continue
                texts = [(str(r[1]), str(r[3] or "")) for r in pages]
                labels = {
                    str(r[1]): {"page_number": str(r[2] or ""), "page_type": r[4], "blank_junk_flag": r[5],
                                "is_duplicate": r[6], "printed_or_handwritten": r[7], "document_seq": r[8]}
                    for r in pages
                }
                # Labels only when imaging_final has rows for the chart.
                has_labels = any(r[4] or r[5] for r in pages)
                chart = chart_from_pages(name, texts, labels if has_labels else {})
                chart.blob_container, chart.blob_path, chart.output_path = where.get(name, ("", "", ""))
                yield chart
    finally:
        conn.close()


# ---- blob ---------------------------------------------------------------------------------

# AZURE_STORAGE_AUTH as in core-pipeline (common/db/blob_store.py): a connection string always
# wins; then managed_identity / entra_interactive / entra / key.
MANAGED_IDENTITY_MODES = {"managed_identity", "managed-identity", "mi", "msi"}
ENTRA_INTERACTIVE_MODES = {"entra_interactive", "entra-interactive", "browser"}
ENTRA_MODES = {"entra", "aad", "azuread"}
# Same token cache and sign-in record as core-pipeline: one browser prompt per machine.
AUTH_RECORD_PATH = Path.home() / ".advantmed" / "azure_auth_record.json"
TOKEN_SCOPE = "https://storage.azure.com/.default"
_SERVICES: dict[str, object] = {}
_SERVICE_LOCK = threading.Lock()


class _RememberedBrowser:
    """InteractiveBrowserCredential that reuses core-pipeline's saved sign-in (no prompt when
    the machine has signed in before), and saves it after the first prompt."""

    def __init__(self, cache) -> None:
        from azure.identity import AuthenticationRecord, InteractiveBrowserCredential

        record = None
        try:
            if AUTH_RECORD_PATH.is_file():
                record = AuthenticationRecord.deserialize(AUTH_RECORD_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable record: sign in again
            logger.warning("unreadable %s; signing in again", AUTH_RECORD_PATH)
        self._known = record is not None
        self._inner = InteractiveBrowserCredential(cache_persistence_options=cache, authentication_record=record)
        self._lock = threading.Lock()

    def _remember(self, scopes) -> None:
        with self._lock:
            if self._known:
                return
            record = self._inner.authenticate(scopes=list(scopes) or [TOKEN_SCOPE])
            try:
                AUTH_RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
                AUTH_RECORD_PATH.write_text(record.serialize(), encoding="utf-8")
            except OSError:
                logger.warning("could not save %s", AUTH_RECORD_PATH)
            self._known = True

    def get_token(self, *scopes, **kwargs):
        self._remember(scopes)
        return self._inner.get_token(*scopes, **kwargs)

    def get_token_info(self, *scopes, options=None):
        self._remember(scopes)
        return self._inner.get_token_info(*scopes, options=options)

    def close(self) -> None:
        self._inner.close()


def _credential(auth: str, client_id: Optional[str], key: Optional[str]):
    if auth in MANAGED_IDENTITY_MODES:
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
    if auth in ENTRA_INTERACTIVE_MODES:
        # Managed identity, then az login, then a browser prompt (once per machine).
        from azure.identity import (AzureCliCredential, ChainedTokenCredential, ManagedIdentityCredential,
                                    TokenCachePersistenceOptions)

        cache = TokenCachePersistenceOptions(name="advantmed_blob_storage", allow_unencrypted_storage=True)
        mi = ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
        return ChainedTokenCredential(mi, AzureCliCredential(process_timeout=30), _RememberedBrowser(cache))
    if auth == "key" or (auth not in ENTRA_MODES and key):
        if not key:
            raise SystemExit("AZURE_STORAGE_AUTH=key needs AZURE_STORAGE_ACCOUNT_KEY (in .env)")
        return key
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(**({"managed_identity_client_id": client_id} if client_id else {}))


def _service():
    """One BlobServiceClient per process (thread-safe), with a connection pool of
    AZURE_BLOB_CONNECTION_POOL_SIZE for the parallel image downloads."""
    with _SERVICE_LOCK:
        if "service" in _SERVICES:
            return _SERVICES["service"]
        from azure.storage.blob import BlobServiceClient

        kwargs: dict = {"max_single_get_size": 64 * 1024 * 1024, "connection_timeout": 20, "read_timeout": 120}
        pool = int(os.environ.get("AZURE_BLOB_CONNECTION_POOL_SIZE") or 0)
        if pool > 0:
            from azure.core.pipeline.transport import RequestsTransport

            kwargs["transport"] = RequestsTransport(connection_pool_maxsize=pool)
        conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn_str:
            service = BlobServiceClient.from_connection_string(conn_str, **kwargs)
        else:
            account = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME") or ""
            if not account:
                raise SystemExit("Set AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_NAME (in .env)")
            auth = (os.environ.get("AZURE_STORAGE_AUTH") or "").strip().casefold()
            credential = _credential(auth, os.environ.get("AZURE_CLIENT_ID") or None,
                                     os.environ.get("AZURE_STORAGE_ACCOUNT_KEY") or None)
            service = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=credential, **kwargs)
        _SERVICES["service"] = service
        return service


def _container_client(container: str):
    """A container on the account in .env (see _service for the sign-in)."""
    name = (container or os.environ.get("AZURE_STORAGE_CONTAINER") or "").strip()
    if not name:
        raise SystemExit("No blob container: pass --container or set AZURE_STORAGE_CONTAINER (in .env)")
    return _service().get_container_client(name)


def _download_text(client, name: str) -> Optional[str]:
    try:
        return client.download_blob(name).readall().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - a missing optional file
        return None


def db_output_paths(chart_list: list[str], database_url: Optional[str] = None,
                    schema: str = "public") -> dict[str, tuple[str, str]]:
    """chart name -> (blob container, output path) from chart_list, for charts across any runs /
    batches (core-pipeline stores each chart's Processed/... destination there)."""
    url = database_url or os.environ.get("DATABASE_URL") or ""
    if not url:
        raise SystemExit("--paths-from-db needs DATABASE_URL (in .env)")
    conn = _connect(url, schema)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT chart_name, COALESCE(blob_container, ''), COALESCE(output_path, '') "
                        "FROM chart_list WHERE chart_name = ANY(%(names)s)", {"names": sorted(set(chart_list))})
            return {str(n): (str(c), str(o)) for n, c, o in cur.fetchall() if o}
    finally:
        conn.close()


def from_blob(container: str, prefixes: list[str], chart_list: Optional[list[str]] = None,
              paths: Optional[dict[str, tuple[str, str]]] = None,
              missing: Optional[list[str]] = None) -> Iterator[Chart]:
    """Charts from Blob. Each chart folder holds ocr/<chart>_prelim.txt (required) and
    imaging/<chart>_final.csv (read when present).

    With ``chart_list`` (charts across runs / batches, 2026-11-15, user) each chart is fetched
    directly, no listing: first from its own output path (``paths``, from chart_list when
    --paths-from-db), else under each of ``prefixes`` in turn (Processed/Run1, Processed/Run2 ...).
    Without a list, every chart folder under the prefixes is read. Charts not found are added to
    ``missing``."""
    clients: dict[str, object] = {}

    def client_for(name: str):
        if name not in clients:
            clients[name] = _container_client(name)
        return clients[name]

    def base(prefix: str) -> str:
        cleaned = prefix.strip("/").replace("\\", "/")
        return f"{cleaned}/" if cleaned else ""

    def fetch(cont: str, folder: str, name: str) -> Optional[Chart]:
        client = client_for(cont)
        prelim = _download_text(client, f"{folder}ocr/{name}_prelim.txt")
        if not prelim:
            return None
        final = _download_text(client, f"{folder}imaging/{name}_final.csv")
        return chart_from_pages(name, split_prelim(prelim), final_rows_from_csv(final) if final else {})

    prefixes = prefixes or [""]
    if chart_list:
        for name in sorted(set(chart_list)):
            chart = None
            own = (paths or {}).get(name)
            if own:
                folder = base(own[1])
                if not folder.rstrip("/").endswith(name):
                    folder = f"{folder}{name}/"
                chart = fetch(own[0] or container, folder, name)
            for prefix in prefixes:
                if chart is None:
                    chart = fetch(container, f"{base(prefix)}{name}/", name)
            if chart is None:
                if missing is not None:
                    missing.append(name)
                continue
            yield chart
        return

    client = client_for(container)
    for prefix in prefixes:
        root = base(prefix)
        names: set[str] = set()
        for blob in client.list_blobs(name_starts_with=root):
            parts = blob.name[len(root):].split("/")
            if len(parts) == 3 and parts[1] == "ocr" and parts[2] == f"{parts[0]}_prelim.txt":
                names.add(parts[0])
        for name in sorted(names):
            chart = fetch(container, f"{root}{name}/", name)
            if chart is not None:
                yield chart


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
