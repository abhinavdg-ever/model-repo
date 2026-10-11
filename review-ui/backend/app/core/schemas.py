from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

OcrKind = Literal["preliminary", "final1", "final2"]
OcrRunStatus = Literal[
    "QUEUED",
    "IN_PROGRESS",
    "COMPLETED",
    "IMAGING_IN_PROGRESS",
    "IMAGING_COMPLETED",
    "FAILED",
]
BlobAuthMode = Literal["entra", "sas"]


class FolderSummary(BaseModel):
    id: str
    name: str
    page_count: int = 0
    ocr_processed: int = 0
    imaging_processed: int = 0
    ocr_status: OcrRunStatus = "QUEUED"
    last_updated_at: datetime | None = None
    run_id: str | None = None
    batch_id: str | None = None
    ground_truth_available: bool = False
    # True when the chart folder exists under the review-ui data root.
    on_disk: bool = False


class FileViewerFolder(BaseModel):
    """A chart directory under data/folders that has a pages/ folder."""

    id: str
    name: str
    page_count: int = 0
    has_corrected: bool = False


class FileViewerListResponse(BaseModel):
    items: list[FileViewerFolder]
    total: int = 0


class FolderListResponse(BaseModel):
    """Paginated landing list. ``limit`` null/omitted on the request → all matches."""

    items: list[FolderSummary]
    total: int = 0
    page_count_sum: int = 0
    ocr_processed_sum: int = 0
    run_options: list[str] = Field(default_factory=list)
    batch_options: list[str] = Field(default_factory=list)
    limit: int | None = None
    offset: int = 0


class PageSummary(BaseModel):
    page_number: int
    filename: str
    image_url: str
    has_preliminary_ocr: bool = False
    has_final1_ocr: bool = False
    has_final2_ocr: bool = False
    has_imaging: bool = False


class FileViewerFolderDetail(FileViewerFolder):
    pages: list[PageSummary] = Field(default_factory=list)


class ImagingManifestDetails(BaseModel):
    """Expected manifest identity for the chart.

    Local Mode → manifest_member_list when DATABASE_URL is set, else
    data/metadata/metadata_R*_B*.csv. Production Mode → manifest_member_list.
    """

    member: str | None = None
    dob: str | None = None
    memberId: str | None = None


class FolderDetail(BaseModel):
    id: str
    name: str
    page_count: int
    ocr_processed: int
    imaging_processed: int = 0
    ocr_status: OcrRunStatus = "QUEUED"
    last_updated_at: datetime | None = None
    run_id: str | None = None
    batch_id: str | None = None
    # Expected member identity (manifest_member_list / metadata CSV) — cheap SQL
    # on folder open so the UI can show Manifest Details before /imaging loads.
    manifest: ImagingManifestDetails | None = None
    pages: list[PageSummary] = Field(default_factory=list)


class OcrSectionHeader(BaseModel):
    """Normalized header box for the page-image overlay (fractions 0–1)."""

    text: str
    level: int = 2
    left: float
    top: float
    width: float
    height: float


class OcrTextResponse(BaseModel):
    folder_id: str
    kind: OcrKind
    text: str
    # fileName → header boxes (Final1 Docling). Empty when unavailable.
    section_headers_by_file: dict[str, list[OcrSectionHeader]] = Field(
        default_factory=dict
    )


class PageGroundTruth(BaseModel):
    """Client labels for one page. Member name, DOB, member id, and provider signature are Yes or No."""

    pageNumber: int
    sourcePageId: str | None = None
    memberName: str | None = None
    memberDob: str | None = None
    memberId: str | None = None
    dosFrom: str | None = None
    dosTo: str | None = None
    encounterType: str | None = None
    pageType: str | None = None
    codeable: str | None = None
    blankPage: str | None = None
    junkPage: str | None = None
    isInvoice: str | None = None
    pageSequence: str | None = None
    rotation: str | None = None
    isVisible: str | None = None
    renderingProvider: str | None = None
    providerSignature: str | None = None


class ImagingPageResult(BaseModel):
    """Per-page imaging fields from pipeline CSVs / Postgres (no fabricated dummy values)."""

    pageNumber: int
    fileName: str
    memberName: str | None = None
    memberDob: str | None = None
    memberId: str | None = None
    memberConfidence: float | None = None
    handwrittenOrPrinted: str | None = None
    handwrittenOrPrintedConfidence: float | None = None
    # Printed | Handwritten | Form | Visual | Blank | Uncertain
    documentType: str | None = None
    # isVisible and handwrittenAreaPct (0–100) come from the page-tag model only.
    isVisible: bool | None = None
    handwrittenAreaPct: float | None = None
    orientationAngle: float | None = None
    tiltAngle: float | None = None
    mirrored: bool | None = None
    pageQualityTag: str | None = None
    pageQualityConfidence: float | None = None
    dosFrom: str | None = None
    dosTo: str | None = None
    dosConfidence: float | None = None
    docDosFrom: str | None = None
    docDosTo: str | None = None
    # Runs before continuity stopped writing spans: dosMatch "span" and
    # finalDos "continuation". New runs carry the page's own date in finalDos.
    dosMatch: str | None = None
    finalDos: str | None = None
    # Document continuity (page_continuity_results / <chart>_continuity.csv).
    documentSeq: int | None = None
    documentPosition: str | None = None  # single | first | continue | last
    documentLabel: str | None = None  # "First (Doc 3)", "Continue (Doc 3)", "Last (Doc 3)"
    continuityRelation: str | None = None  # new_document | continue | unknown
    continuityDecidedBy: str | None = None
    continuityEvidence: str | None = None
    continuityReview: bool | None = None
    # Final values (imaging_final / <chart>_final.csv). On a page inside a
    # document these are its first page's; pageType / isCodeable / dosFrom-To
    # stay the page's own (Extracted).
    finalPageType: str | None = None
    finalCodeable: str | None = None
    finalDosFrom: str | None = None
    finalDosTo: str | None = None
    # None = classification not run → UI shows NA
    # Values: "Yes (Blank)" | "Yes (Junk)" | "No"
    blankOrJunk: str | None = None
    # None = not run → NA; True/False when classified
    isDuplicate: bool | None = None
    # Page file the duplicate was matched to, e.g. "4.png".
    duplicateOf: str | None = None
    # Blank / Main / Duplicate → "Not Available"; Invoice|Cover → that label
    pageType: str | None = None
    pageTypeConfidence: float | None = None
    # Codeable | Non-Codeable | Discharge (from page_subtype CSV)
    isCodeable: str | None = None
    # Outpatient (F2F) | Outpatient (Tele) | Inpatient | Home
    encounterType: str | None = None
    # File/page order (1-based) vs suggested sequence from page_sequencing
    currentSequence: int | None = None
    actualSequence: int | None = None
    # From imaging/<chart>_provider_signature.csv.
    # providerName drops the suffix; providerCredentials is that suffix.
    # providerSignature is signature_present: Yes or No.
    providerName: str | None = None
    providerCredentials: str | None = None
    providerSignature: str | None = None
    providerSignatureConfidence: float | None = None
    groundTruth: PageGroundTruth | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_single_dos(cls, data: Any) -> Any:
        """Accept older imaging JSON with a single `dos` field."""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        legacy = out.get("dos")
        if legacy and not out.get("dosFrom") and not out.get("dosTo"):
            out["dosFrom"] = legacy
            out["dosTo"] = legacy
        return out


class ImagingVerificationDetails(BaseModel):
    """Doc-level member_verification_summary fields."""

    finalStatus: str | None = None
    matchedName: str | None = None
    matchedMemberId: str | None = None
    matchedConfidence: float | None = None
    pagesMatched: int | None = None
    pagesChecked: int | None = None
    decisionReason: str | None = None


class ImagingSectionsProcessed(BaseModel):
    """True when pipeline CSV data exists for this chart (section was run)."""

    member: bool = False
    dos: bool = False
    hw: bool = False
    quality: bool = False
    rotation: bool = False
    junk: bool = False
    codeable: bool = False
    encounter: bool = False
    sequencing: bool = False
    verification: bool = False


class ImagingDocumentResponse(BaseModel):
    folder_id: str
    manifest: ImagingManifestDetails = Field(default_factory=ImagingManifestDetails)
    verification: ImagingVerificationDetails | None = None
    verifications: list[ImagingVerificationDetails] = Field(default_factory=list)
    pages: list[ImagingPageResult] = Field(default_factory=list)
    sectionsProcessed: ImagingSectionsProcessed = Field(
        default_factory=ImagingSectionsProcessed
    )


class ExtractionFieldRow(BaseModel):
    """One staged extraction field. Processed is the extracted value until post-processing exists."""

    id: str
    label: str
    extracted: str = ""
    processed: str = ""
    confidence: float | None = None
    ground_truth: str = ""


class AnnotationRow(BaseModel):
    folder_id: str
    page_number: int
    page_file: str
    field_id: str
    verdict: str
    value: str = ""
    saved_at: str = ""


class AnnotationSaveRequest(BaseModel):
    folder_id: str
    page_number: int
    page_file: str
    field_id: str
    verdict: str
    value: str = ""


class AnnotationListResponse(BaseModel):
    rows: list[AnnotationRow] = Field(default_factory=list)


class ExtractionReviewResponse(BaseModel):
    folder_id: str
    available: bool
    model_version: str = ""
    page_file: str = ""
    fields: list[ExtractionFieldRow] = Field(default_factory=list)
    section_headers: list[OcrSectionHeader] = Field(default_factory=list)


class AccuracyTally(BaseModel):
    correct: int = 0
    wrong: int = 0
    scored: int = 0


class AccuracyChartPayload(BaseModel):
    """One chart. A cached row has metrics; a stale row has the slim document to score."""

    chartId: str
    chartName: str
    lastUpdatedAt: datetime | None = None
    lastVerifiedAt: datetime | None = None
    metrics: dict[str, AccuracyTally] | None = None
    overall: AccuracyTally | None = None
    document: ImagingDocumentResponse | None = None


class AccuracyReportResponse(BaseModel):
    charts: list[AccuracyChartPayload] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    data_mode: str
    mode_label: str = "Local Mode"


class AppConfigResponse(BaseModel):
    data_mode: str
    mode_label: str = "Local Mode"
    file_viewer_blob_enabled: bool = False
    blob_auth_mode: BlobAuthMode = "entra"
    blob_account_url: str = ""
    blob_container: str = ""
    blob_path_template: str = "{folder}/pages/{filename}"
    blob_entra_ready: bool = False
    blob_auth_required: bool = False
    blob_sas_configured: bool = False
