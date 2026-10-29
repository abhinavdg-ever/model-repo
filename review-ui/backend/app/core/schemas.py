from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    # Marked Reviewed in History (chart_review): its ground truth is frozen.
    reviewed: bool = False
    reviewed_by: str | None = None
    reviewed_by_id: int | None = None
    reviewed_at: datetime | None = None
    # True when the chart folder exists under the review-ui data root.
    on_disk: bool = False
    # Chart metadata (manifest_member_list / metadata CSV): Scope, Source, Measure, Retrieval.
    project_scope: str | None = None
    source_name: str | None = None
    measure: str | None = None
    rr_method: str | None = None


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
    # Imaging Completed charts among the matches, their pages, and those with ground truth.
    imaging_completed_charts: int = 0
    imaging_completed_pages: int = 0
    ground_truth_charts: int = 0
    run_options: list[str] = Field(default_factory=list)
    batch_options: list[str] = Field(default_factory=list)
    scope_options: list[str] = Field(default_factory=list)
    source_options: list[str] = Field(default_factory=list)
    measure_options: list[str] = Field(default_factory=list)
    retrieval_options: list[str] = Field(default_factory=list)
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
    # A heading candidate that did not pass (drawn lighter); False for a heading.
    candidate: bool = False
    # A candidate that still reads like a heading: drawn heavier than a form label.
    heading_like: bool = False


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
    # Yes / No. Older rows hold credentials here (before providerCredentials was saved).
    providerSignature: str | None = None
    providerCredentials: str | None = None
    # Free text, any L2 / specialty (2026-10-27, user): page_subtype and provider_specialty
    # (the client sheet's Provider Specialty column).
    pageSubtype: str | None = None
    specialty: str | None = None
    # The page's encounter type (page_ground_truth.encounter_label, 2026-10-27), tilt and
    # quality (High / Medium / Low).
    encounterLabel: str | None = None
    tilt: str | None = None
    quality: str | None = None
    # Mirrored (Yes / No), Type (Printed, Handwritten, ...) and Is Duplicate (Yes /
    # Partial / No), 2026-10-27.
    mirrored: str | None = None
    printedOrHandwritten: str | None = None
    isDuplicate: str | None = None
    handwrittenPct: str | None = None
    # The page's last manual save (page_ground_truth_entry): who, and when (UTC ISO; the
    # UI shows IST). Read only: a save ignores them.
    reviewedBy: str | None = None
    reviewedAt: str | None = None
    bulkUploadedAt: str | None = None  # the page's last spreadsheet load (UTC ISO)


class PageSequenceLabel(BaseModel):
    """One page's ground-truth actual sequence (page_ground_truth.page_sequence)."""

    pageNumber: int
    sourcePageId: str | None = None
    pageSequence: str | None = None


class PageSequenceSave(BaseModel):
    pages: list[PageSequenceLabel]


class EncounterReviewRow(BaseModel):
    """One reviewed encounter: a page range and its values (encounter_review)."""

    seq: int = 0
    startPage: int
    endPage: int
    pageType: str | None = None
    encounterType: str | None = None
    dosFrom: str | None = None
    dosTo: str | None = None


class EncounterReview(BaseModel):
    rows: list[EncounterReviewRow] = []
    savedAt: str | None = None
    savedBy: str | None = None


class ChartReview(BaseModel):
    """A chart marked Reviewed (chart_review): its ground truth is frozen while marked."""

    reviewed: bool = False
    reviewedBy: str | None = None
    reviewedById: int | None = None  # who marked it: they (or an admin) may clear the mark
    reviewedAt: str | None = None  # UTC ISO; the UI shows IST


class ReviewLogEntry(BaseModel):
    """One ground-truth save by a user (page_ground_truth_entry, manual_entry), or a
    chart-level action (chart_review_entry; pageNumber None): Reviewed marked or cleared,
    member decision, encounters. ``fields`` are the page_ground_truth columns the
    save changed, each with its value before and after."""

    chartName: str
    pageNumber: int | None = None
    at: str
    user: str | None = None
    action: str = "edit"  # edit | reviewed | review_cleared | member_decision | encounters
    fields: list[dict[str, str | None]] = []


class ReviewLogResponse(BaseModel):
    items: list[ReviewLogEntry]
    total: int = 0
    users: list[dict[str, str | int]] = []


class EncounterReviewSave(BaseModel):
    rows: list[EncounterReviewRow]


class GroundTruthUpdate(BaseModel):
    """Who last changed one part of a chart's ground truth, and when: a manual save
    (by = the user) or, when none was made since, the spreadsheet load (bulk)."""

    bulk: bool = False
    by: str | None = None
    at: str | None = None  # UTC ISO; the UI shows IST


class ChartGroundTruthUpdates(BaseModel):
    """Last update of each ground-truth tab: Page Details, Encounter, Sequencing and
    Rejection Rules (the member decision)."""

    page: GroundTruthUpdate = GroundTruthUpdate()
    encounter: GroundTruthUpdate = GroundTruthUpdate()
    sequencing: GroundTruthUpdate = GroundTruthUpdate()
    member: GroundTruthUpdate = GroundTruthUpdate()


class ChartGroundTruth(BaseModel):
    """The reviewer's chart-level ground truth: the member decision (2026-10-28)."""

    memberDecision: str | None = None  # accept / reject
    memberDecisionReason: str | None = None
    savedAt: str | None = None
    savedBy: str | None = None


class ImagingPageResult(BaseModel):
    """Per-page imaging fields from pipeline CSVs / Postgres (no fabricated dummy values)."""

    pageNumber: int
    fileName: str
    memberName: str | None = None
    memberDob: str | None = None
    memberId: str | None = None
    memberConfidence: float | None = None
    # The member stage's page verdict (verified / wrong_member / not_verified) and the people
    # the page names who are not the manifest member (2026-10-27).
    memberPageStatus: str | None = None
    memberOtherNames: list[str] | None = None
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
    # Stage 1's measured values' confidences (2026-10-26): orientation, mirrored, tilt,
    # visibility, handwritten_area (ocr_quality_results.quality_detail.confidences).
    measuredConfidences: dict[str, float] | None = None
    # The subtype's own confidence (the deepest named level, conditional on its parent).
    subtypeConfidence: float | None = None
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
    continuityRelation: str | None = None  # new_document | continue
    continuityDecidedBy: str | None = None
    continuityEvidence: str | None = None
    # Final values (imaging_final / <chart>_final.csv). On a page inside a
    # document these are its first page's; pageType / isCodeable / dosFrom-To
    # stay the page's own (Extracted).
    finalPageType: str | None = None
    finalCodeable: str | None = None
    finalSpecialty: str | None = None
    finalDosFrom: str | None = None
    finalDosTo: str | None = None
    # Each Final value's confidence on the one scale (imaging_final.confidences, 2026-10-25:
    # >= 0.80 good, 0.50-0.80 passable, < 0.50 review), keyed page_type / codability /
    # specialty / dos / encounter_type / member / provider / blank_junk / handwriting,
    # and the keys below the review line.
    finalConfidences: dict[str, float] | None = None
    reviewFields: list[str] | None = None
    # The stages' own confidences that have no other field.
    blankJunkConfidence: float | None = None
    encounterConfidence: float | None = None
    sequenceConfidence: float | None = None
    # None = classification not run → UI shows NA
    # Values: "Yes (Blank)" | "Yes (Junk)" | "No" (a duplicate reads "Yes (Junk)")
    blankOrJunk: str | None = None
    # None = not run → NA; True/False when classified. True: ≥ 98% alike (or
    # contained) -- the page is a duplicate (Blank / Junk "Yes (Junk)").
    isDuplicate: bool | None = None
    # True: 95-98% alike -- a partial duplicate; the page keeps its own verdict (Main or junk).
    isPartialDuplicate: bool | None = None
    # Page file the duplicate (or partial duplicate) was matched to, e.g. "4.png".
    duplicateOf: str | None = None
    # The page's highest duplicate similarity against its neighbors (0-1), whatever its
    # verdict; the Duplicate confidence comes from it. None when never compared.
    duplicateConfidence: float | None = None
    # Blank / Main / Duplicate → "Not Available"; Invoice|Cover → that label
    pageType: str | None = None
    pageTypeConfidence: float | None = None
    # Page-axes specialty (page_classification.axes); no ground truth exists.
    specialty: str | None = None
    specialtyConfidence: float | None = None
    # Accuracy report only: best semantic similarity (0-1) between the
    # ground-truth page type and the predicted page type / sub-type.
    pageTypeSemantic: float | None = None
    # Codable | Non-Codable | Discharge (from page_subtype CSV)
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
    # Every accepted signer, the selected one first: [{"name", "credentials", "signatureDate"}].
    # Rows from before the list (2026-10-18) have none; providerName / Credentials then hold
    # the one provider. With a list, providerName is the first signer and
    # providerCredentials every signer's credentials (" | ").
    providerSigners: list[dict[str, str | None]] | None = None
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


class StagedEncounter(BaseModel):
    """One encounter from ``staging/continuity.json`` (the encounter_type stage)."""

    seq: int
    document: int | None = None
    page_family: str | None = None
    pages: list[str] = Field(default_factory=list)
    dos_from: str | None = None
    dos_to: str | None = None
    encounter_type: str | None = None
    encounter_label: str | None = None
    confidence: float | None = None
    decided_by: str | None = None
    reason: str | None = None
    matched_keyword: str | None = None


class ImagingDocumentResponse(BaseModel):
    folder_id: str
    manifest: ImagingManifestDetails = Field(default_factory=ImagingManifestDetails)
    verification: ImagingVerificationDetails | None = None
    verifications: list[ImagingVerificationDetails] = Field(default_factory=list)
    pages: list[ImagingPageResult] = Field(default_factory=list)
    sectionsProcessed: ImagingSectionsProcessed = Field(
        default_factory=ImagingSectionsProcessed
    )
    # Staged encounter grouping; empty = derive it from the pages' document_seq.
    encounters: list[StagedEncounter] = Field(default_factory=list)


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


class ExtractionPagesResponse(BaseModel):
    """Every page's extraction review for a chart, by page file name: loaded once per chart
    like the imaging results, not once per page."""

    folder_id: str
    available: bool
    pages: dict[str, ExtractionReviewResponse] = Field(default_factory=dict)


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
    # Run / Batch and chart metadata, for the Accuracy View filters.
    runId: str | None = None
    batchId: str | None = None
    projectScope: str | None = None
    sourceName: str | None = None
    measure: str | None = None
    rrMethod: str | None = None
    metrics: dict[str, AccuracyTally] | None = None
    overall: AccuracyTally | None = None
    document: ImagingDocumentResponse | None = None


class AccuracyReportResponse(BaseModel):
    charts: list[AccuracyChartPayload] = Field(default_factory=list)


class LoginRequest(BaseModel):
    username: str
    password: str


class SignedInUserResponse(BaseModel):
    id: int | None = None
    username: str
    name: str
    role: str = "reviewer"


class LoginResponse(BaseModel):
    token: str
    user: SignedInUserResponse


class HealthResponse(BaseModel):
    status: str
    data_mode: str
    mode_label: str = "Local Mode"


class AppConfigResponse(BaseModel):
    # The mode this browser is in (its Local / DB switch, else DATA_MODE).
    data_mode: str
    mode_label: str = "Local Mode"
    # A database is configured, so the DB side of the switch can be offered.
    db_available: bool = False
    # DATA_MODE: what a browser that never chose sees.
    default_mode: str = "local"
    file_viewer_blob_enabled: bool = False
    blob_auth_mode: BlobAuthMode = "entra"
    blob_account_url: str = ""
    blob_container: str = ""
    blob_path_template: str = "{folder}/pages/{filename}"
    blob_entra_ready: bool = False
    blob_auth_required: bool = False
    blob_sas_configured: bool = False
    # Playground (admins only): the core-pipeline APIs a run can go to (first = default)
    # and the default container.
    playground_enabled: bool = False
    playground_targets: list[dict[str, str]] = Field(default_factory=list)
    playground_container: str = ""
    # CLIENT_VIEW: Page Details shows Final only, headed Algorithm.
    client_view: bool = False


class PlaygroundRun(BaseModel):
    """A batch run from the Playground. The output folder is derived, not sent."""

    model_config = ConfigDict(extra="forbid")

    # One of CORE_PIPELINE_URLS, by URL or label (VM / Local). Required.
    target: str
    container_name: str = ""
    input_path: str
    # Required: 1 to playground.MAX_CHARTS (10) chart names.
    chart_list: list[str] | str
    skip_ocr: bool = True
    skip_completed: bool = False


class NewUser(BaseModel):
    """An admin creates a user; the username must not exist yet (any case)."""

    model_config = ConfigDict(extra="forbid")

    username: str
    name: str = ""
    password: str
    role: Literal["reviewer", "admin", "viewer"] = "reviewer"


class PasswordReset(BaseModel):
    """An admin sets a new password for an existing user."""

    model_config = ConfigDict(extra="forbid")

    username: str
    password: str
