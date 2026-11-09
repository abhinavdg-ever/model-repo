"""DB_BACKEND=cosmos: the chart views read from Azure Cosmos DB for NoSQL (2026-11-07).

core-pipeline's CosmosStore (core-pipeline/common/db/cosmos_store.py) writes, partition key
``/chart_name`` everywhere:

  charts       {id: "chart", chart_name, chart: <chart_list row>, member_summary}
  pages        {id: "page-<page_id>", chart_name, page_id, page: <page_list row>,
                stages: {"<stage>|<pass>": row}, ocr: {"<ocr_type>": row}, quality,
                blank_junk: {"<pass_no>": row}, dos, classification, encounter, sequencing,
                continuity, additional_details, imaging_final, provider_signature,
                member_extraction}
  manifest     {id: "m-<id>", chart_name: <record_id>, member: <manifest_member_list row>}
  ground_truth {id: "page-<n>", chart_name, page_number, <label columns>, entries}
               (db-utilities/dbutil/cosmos.py)

Each row is the Postgres row of the same table, so this repository turns a chart's documents
into the tuples the Postgres queries return and builds the responses with the same code
(store/rows.py). Page images come from Azure Blob exactly as with Postgres
(store/blob_pages.py).

Cost: a chart view reads one chart document (point read) and the chart's page documents
(one single-partition query), cached per chart until a cheap fingerprint query (page count
and latest ``_ts``) says a page changed. The landing list is one cross-partition query over
the chart documents; no page document is read for it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from app.core.schemas import (
    FolderDetail,
    FolderSummary,
    ImagingDocumentResponse,
    ImagingManifestDetails,
    ImagingPageResult,
    ImagingVerificationDetails,
    OcrTextResponse,
)
from app.store import rows as R
from app.store.base import FolderRepository
from app.store.blob_pages import BlobPageImages
from app.store.shared import _normalize_kind

logger = logging.getLogger("review_ui.cosmos")

# pipeline_stage's phase-1 rows (schema/schema.sql seed; core-pipeline's
# DEFAULT_PIPELINE_STAGES): a page is done when every one is completed or skipped.
PHASE1_STAGES: tuple[tuple[str, int], ...] = (
    ("ocr_quality", 1), ("ocr_prelim", 1), ("blank_junk", 1), ("ocr_final1", 1),
    ("ocr_final2", 1), ("section_headers", 1), ("kv_extract", 1), ("blank_junk", 2),
    ("member_verify", 1), ("page_subtype", 1), ("dos_extract", 1), ("continuity", 1),
    ("encounter_type", 1), ("page_sequencing", 1), ("imaging_final", 1),
)
_DONE = frozenset({"completed", "skipped"})
_FINAL_OCR_STAGE = {"docling": "ocr_final1", "azuredocintel": "ocr_final2"}

# The page_ground_truth columns in services/ground_truth.py _COLUMNS order (after
# page_number, source_page_id): what _ground_truth_from_row reads.
_TRUTH_COLUMNS = (
    "member_name", "member_dob", "dos_from", "dos_to", "encounter_type", "page_type",
    "codeable", "blank_page", "junk_page", "is_invoice", "page_sequence", "rotation",
    "is_visible", "rendering_provider", "provider_signature", "member_id",
    "provider_credentials", "page_subtype", "provider_specialty", "encounter_label", "tilt",
    "quality", "mirrored", "printed_or_handwritten", "is_duplicate", "handwritten_pct",
)

CONTAINERS = ("charts", "pages", "manifest", "ground_truth", "runs", "users", "metrics")
# Charts whose page documents are kept in memory (least recently used out).
_CHART_CACHE_SIZE = 16
# A fingerprint whose newest _ts is this recent is not cached: _ts has one-second
# resolution, so a write in the same second could otherwise hide behind it.
_SETTLE_SEC = 2


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status_code", None)


def _as_datetime(value: Any) -> datetime | None:
    """A stored timestamp (CosmosStore writes str(datetime)) as a datetime."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _page_sort_key(doc: dict[str, Any]) -> tuple:
    """ORDER BY page_number NULLS LAST, id."""
    num = (doc.get("page") or {}).get("page_number")
    return (num is None, num if num is not None else 0, int(doc.get("page_id") or 0))


def _blank(text: Any) -> bool:
    """Postgres ``length(trim(raw_text)) > 0`` is false (trim strips spaces only)."""
    return text is None or not str(text).strip(" ")


class _Chart:
    """One chart's documents: the chart doc and its page docs in page order."""

    def __init__(self, chart: dict[str, Any] | None, pages: list[dict[str, Any]]):
        self.chart_doc = chart
        self.row = (chart or {}).get("chart") or {}
        chart_id = self.row.get("id")
        # As the Postgres JOIN chart_list ON id = page_list.chart_id.
        self.pages = [
            d for d in pages
            if chart_id is None or (d.get("page") or {}).get("chart_id") in (None, chart_id)
        ] if chart else []
        self.names = {int(d["page_id"]): str(d["page"]["page_name"]) for d in self.pages}

    def each(self, field: str):
        """(page_name, row) for every page whose ``field`` is set."""
        for d in self.pages:
            row = d.get(field)
            if row:
                yield self.names[int(d["page_id"])], row


class CosmosFolderRepository(BlobPageImages, FolderRepository):
    def __init__(
        self,
        containers: dict[str, Any] | None = None,
        *,
        endpoint: str = "",
        key: str = "",
        database: str = "imaging",
    ):
        """``containers`` (name -> container client) for tests; otherwise built on first
        use from the endpoint, key and database."""
        self._containers = dict(containers) if containers is not None else None
        self._endpoint = (endpoint or "").strip()
        self._key = (key or "").strip()
        self._database = (database or "imaging").strip() or "imaging"
        self._lock = threading.RLock()
        # chart name -> (fingerprint, page docs, {kind: OcrTextResponse})
        self._charts: "OrderedDict[str, tuple[tuple, list[dict[str, Any]], dict[str, OcrTextResponse]]]" = OrderedDict()
        self._init_blob_caches()

    # -- Cosmos access ------------------------------------------------------

    def _container(self, name: str) -> Any:
        with self._lock:
            if self._containers is None:
                if not self._endpoint or not self._key:
                    raise HTTPException(
                        status_code=503,
                        detail="DB_BACKEND=cosmos needs COSMOS_ENDPOINT and COSMOS_KEY in review-ui/.env",
                    )
                from azure.cosmos import CosmosClient

                db = CosmosClient(self._endpoint, credential=self._key).get_database_client(self._database)
                self._containers = {n: db.get_container_client(n) for n in CONTAINERS}
            return self._containers[name]

    def _query(self, container: str, sql: str, partition_key: str, parameters: list | None = None) -> list[dict]:
        return list(self._container(container).query_items(
            sql, parameters=parameters or [], partition_key=partition_key))

    def _chart_doc(self, folder_id: str) -> dict[str, Any] | None:
        try:
            return self._container("charts").read_item("chart", partition_key=folder_id)
        except HTTPException:
            raise
        except Exception as exc:
            if _status(exc) == 404:
                return None
            raise

    def _fingerprint(self, folder_id: str) -> tuple:
        """(page doc count, newest _ts) for the chart, or () when it cannot be read."""
        try:
            rows = self._query("pages", "SELECT COUNT(1) AS n, MAX(c._ts) AS ts FROM c", folder_id)
        except Exception as exc:
            logger.debug("page fingerprint failed for %s: %s", folder_id, exc)
            return ()
        row = rows[0] if rows else {}
        return (int(row.get("n") or 0), row.get("ts"))

    def _page_docs(self, folder_id: str) -> tuple[tuple, list[dict[str, Any]]]:
        """The chart's page docs in page order, reused while the fingerprint holds."""
        fingerprint = self._fingerprint(folder_id)
        with self._lock:
            hit = self._charts.get(folder_id)
            if fingerprint and hit and hit[0] == fingerprint:
                self._charts.move_to_end(folder_id)
                return fingerprint, hit[1]
        docs = sorted(self._query("pages", "SELECT * FROM c", folder_id), key=_page_sort_key)
        newest = fingerprint[1] if fingerprint else None
        if fingerprint and (newest is None or int(newest) < time.time() - _SETTLE_SEC):
            with self._lock:
                self._charts[folder_id] = (fingerprint, docs, {})
                self._charts.move_to_end(folder_id)
                while len(self._charts) > _CHART_CACHE_SIZE:
                    self._charts.popitem(last=False)
        return fingerprint, docs

    def _load(self, folder_id: str) -> tuple[_Chart, tuple]:
        """The chart and its page docs, with the page fingerprint (() when unknown)."""
        chart = self._chart_doc(folder_id)
        if chart is None:
            return _Chart(None, []), ()
        fingerprint, docs = self._page_docs(folder_id)
        return _Chart(chart, docs), fingerprint

    def _chart(self, folder_id: str) -> _Chart:
        return self._load(folder_id)[0]

    def _manifest_rows(self, folder_id: str) -> list[dict[str, Any]]:
        """manifest_member_list rows with record_id = the chart name, by id."""
        docs = self._query("manifest", "SELECT * FROM c", folder_id)
        rows = [d["member"] for d in docs if isinstance(d.get("member"), dict)]
        return sorted(rows, key=lambda m: int(m.get("id") or 0))

    def _manifest_member(self, member_id: Any, folder_id: str, known: list[dict[str, Any]]) -> dict[str, Any] | None:
        """manifest_member_list row by id (the summary's matched_member_list_id): this
        chart's first, else any record (a '-test' chart verifies against its real one)."""
        if member_id is None:
            return None
        for row in known:
            if str(row.get("id")) == str(member_id):
                return row
        docs = list(self._container("manifest").query_items(
            "SELECT * FROM c WHERE c.id = @id",
            parameters=[{"name": "@id", "value": f"m-{member_id}"}],
            enable_cross_partition_query=True,
        ))
        return docs[0].get("member") if docs else None

    # -- list ---------------------------------------------------------------

    def list_folders(self) -> list[FolderSummary]:
        """Landing rows from the chart documents (one cross-partition query), sorted and
        filtered as the Postgres list is. Ground truth, History review, tags and chart
        metadata are Postgres services and are not merged here."""
        from app.services.folder_list import FolderListParams, chart_summary, filter_sort_page

        docs = self._container("charts").query_items(
            "SELECT c.chart FROM c WHERE c.id = 'chart'", enable_cross_partition_query=True)
        charts = [d["chart"] for d in docs if isinstance(d.get("chart"), dict)]
        # WHERE source <> 'manifest' OR page_count > 0
        charts = [c for c in charts if c.get("source") != "manifest" or int(c.get("page_count") or 0) > 0]
        # ORDER BY updated_at DESC NULLS LAST, id DESC
        charts.sort(key=lambda c: int(c.get("id") or 0), reverse=True)
        charts.sort(key=lambda c: (_as_datetime(c.get("updated_at")) is not None,
                                   _as_datetime(c.get("updated_at")) or datetime.min), reverse=True)
        rows = []
        for c in charts:
            summary = chart_summary(c.get("chart_name"), c.get("page_count"), c.get("status"),
                                    _as_datetime(c.get("updated_at")), c.get("current_stage"),
                                    c.get("run_id"), c.get("batch_id"))
            if summary is not None:
                rows.append(summary)
        params = FolderListParams(limit=None, offset=0, sort="updated", sort_dir="desc")
        return filter_sort_page(rows, params).items

    # -- folder -------------------------------------------------------------

    def get_folder(self, folder_id: str) -> FolderDetail:
        """Pages and OCR flags from the chart's documents; 404 when it has no pages."""
        try:
            chart = self._chart(folder_id)
            manifest = self._manifest_rows(folder_id) if chart.chart_doc else []
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("folder_from_cosmos failed for %s: %s", folder_id, exc)
            chart, manifest = _Chart(None, []), []
        detail = R.folder_detail(
            folder_id,
            self._chart_row(chart),
            [(d["page"].get("page_number"), d["page"]["page_name"]) for d in chart.pages],
            [
                (chart.names[int(d["page_id"])], ocr_type)
                for d in chart.pages
                for ocr_type, row in (d.get("ocr") or {}).items()
                if not _blank((row or {}).get("raw_text"))
            ],
            {chart.names[int(d["page_id"])] for d in chart.pages if self._imaging_done(d)},
            (lambda m: (m.get("member_name"), m.get("member_dob"), m.get("external_member_id")))(manifest[0])
            if manifest else None,
        )
        if detail is not None and detail.pages:
            return detail
        raise HTTPException(status_code=404, detail=f"Chart not found: {folder_id}")

    @staticmethod
    def _chart_row(chart: _Chart) -> tuple | None:
        if not chart.chart_doc:
            return None
        r = chart.row
        return (r.get("id"), r.get("status"), r.get("current_stage"), r.get("run_id"), r.get("batch_id"))

    @staticmethod
    def _imaging_done(doc: dict[str, Any]) -> bool:
        stages = doc.get("stages") or {}
        return all(
            str((stages.get(f"{name}|{pass_no}") or {}).get("status") or "") in _DONE
            for name, pass_no in PHASE1_STAGES
        )

    def get_page_image_path(
        self, folder_id: str, page_number: int, *, corrected: bool = False
    ) -> Path:
        """Never a disk path: page images come from Azure Blob only (2026-11-07)."""
        raise HTTPException(
            status_code=404,
            detail="No page image: review-ui serves page images from Azure Blob only",
        )

    def _page_location_rows(self, folder_id: str) -> list[tuple]:
        chart = self._chart(folder_id)
        r = chart.row
        return [
            (r.get("blob_container"), r.get("blob_path"), r.get("output_path"),
             d["page"]["page_name"], d["page"].get("page_number"),
             d["page"].get("use_corrected"), d["page"].get("image_path"))
            for d in chart.pages
        ]

    # -- OCR ----------------------------------------------------------------

    def get_ocr_text(self, folder_id: str, kind: str) -> OcrTextResponse:
        """The chart's OCR text for ``kind``, rebuilt only when a page document changed."""
        normalized = _normalize_kind(kind)
        ocr_type = R.KIND_TO_OCR_TYPE[normalized]
        try:
            chart, fingerprint = self._load(folder_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Cosmos OCR lookup failed: {exc}") from exc
        with self._lock:
            hit = self._charts.get(folder_id)
            if hit and fingerprint and hit[0] == fingerprint and normalized in hit[2]:
                return hit[2][normalized]
        built = R.ocr_text(folder_id, kind, self._ocr_rows(chart, ocr_type))
        with self._lock:
            hit = self._charts.get(folder_id)
            if hit and fingerprint and hit[0] == fingerprint:
                hit[2][normalized] = built
        return built

    @staticmethod
    def _ocr_rows(chart: _Chart, ocr_type: str) -> list[tuple]:
        """(page_name, raw_text, skip_reason) as the Postgres OCR query returns them: every
        page for a Final OCR (with its stage's skip reason), pages with a row otherwise."""
        rows: list[tuple] = []
        stage = _FINAL_OCR_STAGE.get(ocr_type)
        for d in chart.pages:
            name = chart.names[int(d["page_id"])]
            ocr = (d.get("ocr") or {}).get(ocr_type)
            if stage:
                skip = ((d.get("stages") or {}).get(f"{stage}|1") or {}).get("skip_reason")
                rows.append((name, (ocr or {}).get("raw_text"), skip))
            elif ocr is not None:
                rows.append((name, ocr.get("raw_text"), None))
        return rows

    # -- imaging ------------------------------------------------------------

    def get_imaging(self, folder_id: str) -> ImagingDocumentResponse:
        """The imaging panel from the chart's documents, built as the Postgres one is."""
        try:
            chart = self._chart(folder_id)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("imaging lookup failed for %s: %s", folder_id, exc)
            chart = _Chart(None, [])
        try:
            manifest_rows = self._manifest_rows(folder_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Cosmos manifest lookup failed: {exc}") from exc

        manifest: ImagingManifestDetails | None = None
        if manifest_rows:
            m = manifest_rows[0]
            manifest = R.manifest_details((
                m.get("member_name"), m.get("member_dob"), m.get("external_member_id"),
                R.json_text(m.get("measure")), R.json_text(m.get("service_note")),
            ))
        return R.imaging_document(
            folder_id,
            R.page_list([(d["page"].get("page_number"), d["page"]["page_name"]) for d in chart.pages]),
            R.page_imaging_fields(folder_id, self.overlay_rows(chart)),
            manifest,
            self._verification(chart, folder_id, manifest_rows),
            self._section_flags(chart),
            self._truth_attacher(folder_id),
        )

    def _verification(
        self, chart: _Chart, folder_id: str, manifest_rows: list[dict[str, Any]]
    ) -> ImagingVerificationDetails | None:
        summary = (chart.chart_doc or {}).get("member_summary")
        if not summary:
            return None
        try:
            matched = self._manifest_member(summary.get("matched_member_list_id"), folder_id, manifest_rows)
        except Exception as exc:
            logger.warning("matched manifest member lookup failed for %s: %s", folder_id, exc)
            matched = None
        return R.verification_details((
            summary.get("final_status"), summary.get("matched_name"),
            (matched or {}).get("external_member_id"), summary.get("confidence"),
            summary.get("pages_matched"), summary.get("pages_checked"),
            summary.get("decision_reason"),
        ))

    @staticmethod
    def overlay_rows(chart: _Chart) -> dict[str, list[tuple]]:
        """Every overlay section's rows, as the tuples of the Postgres _OVERLAY_SQL."""
        jt = R.json_text
        out: dict[str, list[tuple]] = {name: [] for name in R.OVERLAY_SECTIONS}
        for name, q in chart.each("quality"):
            detail = q.get("quality_detail")
            out["quality"].append((
                name, q.get("printed_or_handwritten"), q.get("hw_confidence"), q.get("quality_score"),
                q.get("quality_tag"), q.get("orientation_angle"), q.get("tilt_angle"), q.get("mirrored"),
                q.get("document_type"), q.get("is_visible"), q.get("handwritten_area_pct"),
                detail.get("confidences") if isinstance(detail, dict) else None,
            ))
        for d in chart.pages:
            name = chart.names[int(d["page_id"])]
            for _pass, b in sorted((d.get("blank_junk") or {}).items(), key=lambda kv: int(kv[0])):
                if not (b or {}).get("is_final"):
                    continue
                original = b.get("duplicate_of_page_id")
                out["blank_junk"].append((
                    name, b.get("blank_junk_flag"), b.get("junk_subtype"), b.get("confidence"),
                    chart.names.get(int(original)) if original is not None else None,
                    jt(b.get("duplicate_confidence")), b.get("reason"),
                ))
        for name, m in chart.each("member_extraction"):
            out["member"].append((
                name, m.get("extracted_name"), m.get("extracted_dob"), m.get("extracted_member_id"),
                m.get("confidence"), m.get("page_status"), m.get("other_names"),
            ))
        for name, r in chart.each("dos"):
            out["dos"].append((
                name, r.get("date_of_service_from"), r.get("date_of_service_to"),
                r.get("date_of_service_from_doclevel"), r.get("date_of_service_to_doclevel"),
                r.get("confidence"),
            ))
        for name, pc in chart.each("classification"):
            axes = pc.get("axes")
            axes_d = axes if isinstance(axes, dict) else {}
            out["page_classification"].append((
                name, pc.get("page_subtype"), pc.get("classification_category"), pc.get("confidence"),
                jt(pc.get("page_type")), jt(axes_d.get("specialty")),
                jt(axes_d.get("specialty_confidence")), axes,
            ))
        for name, e in chart.each("encounter"):
            out["encounter_type"].append((name, e.get("encounter_type"), e.get("confidence")))
        for d in chart.pages:
            s = d.get("sequencing")
            if s:
                out["sequencing"].append((
                    chart.names[int(d["page_id"])], d["page"].get("page_number"),
                    s.get("original_page_number"), s.get("seq"), s.get("confidence"),
                ))
        for name, k in chart.each("continuity"):
            out["continuity"].append((name, k))
        for name, s in chart.each("provider_signature"):
            out["provider_signature"].append((
                name, s.get("provider_name"), s.get("signature_present"),
                jt(s.get("provider_credentials")), s.get("signers"), jt(s.get("provider_version")),
                s.get("confidence"),
            ))
        for name, f in chart.each("imaging_final"):
            out["imaging_final"].append((
                name, f.get("page_type"), f.get("classification_category"), f.get("dos_from"),
                f.get("dos_to"), jt(f.get("page_subtype")), jt(f.get("specialty")),
                jt(f.get("provider_name")), jt(f.get("signature_present")),
                jt(f.get("provider_credentials")), f.get("signers"), jt(f.get("provider_version")),
                jt(f.get("provider_confidence")), f.get("confidences"), f.get("review_fields"),
            ))
        return out

    @staticmethod
    def _section_flags(chart: _Chart) -> dict[str, bool]:
        """Which imaging sections have any result rows (the Postgres EXISTS checks)."""
        flags = R.empty_section_flags()
        if not chart.chart_doc:
            return flags
        pages = chart.pages
        has = lambda field: any(d.get(field) for d in pages)  # noqa: E731
        flags["member"] = has("member_extraction") or bool(chart.chart_doc.get("member_summary"))
        flags["dos"] = has("dos")
        flags["hw"] = flags["quality"] = flags["rotation"] = has("quality")
        flags["junk"] = has("blank_junk")
        flags["codeable"] = has("classification")
        # Unresolved visits write no row, so a finished stage counts even when every page
        # is unresolved.
        flags["encounter"] = has("encounter") or any(
            key.rsplit("|", 1)[0] == "encounter_type" and (row or {}).get("status") in _DONE
            for d in pages for key, row in (d.get("stages") or {}).items()
        )
        flags["sequencing"] = has("sequencing")
        return flags

    # -- ground truth -------------------------------------------------------

    @property
    def review(self):
        """The review store over these containers (services on Cosmos; store/cosmos/review.py)."""
        with self._lock:
            if getattr(self, "_review", None) is None:
                from app.store.cosmos.review import CosmosReviewStore

                self._review = CosmosReviewStore(self)
            return self._review

    def _truth_attacher(self, folder_id: str) -> Callable[[list[ImagingPageResult]], list[ImagingPageResult]]:
        """Attach ground truth from the ground_truth container (db-utilities and review-ui
        write it), with who last saved each page, as services/ground_truth.attach_ground_truth
        does from page_ground_truth."""
        from app.services.ground_truth import _page_id

        def attach(pages: list[ImagingPageResult]) -> list[ImagingPageResult]:
            try:
                by_page = self.review.ground_truth_by_page(folder_id)
            except Exception as exc:
                if _status(exc) != 404:
                    logger.warning("ground_truth lookup failed for %s: %s", folder_id, exc)
                return pages
            if not by_page:
                return pages
            out = []
            for page in pages:
                number = _page_id(page.fileName)
                hit = by_page.get(number) if number is not None else None
                out.append(page.model_copy(update={"groundTruth": hit}) if hit else page)
            return out

        return attach
