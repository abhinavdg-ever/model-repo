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


class PageSummary(BaseModel):
    page_number: int
    filename: str
    image_url: str
    has_preliminary_ocr: bool = False
    has_final1_ocr: bool = False
    has_final2_ocr: bool = False
    has_imaging: bool = False


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
    pages: list[PageSummary] = Field(default_factory=list)


class OcrTextResponse(BaseModel):
    folder_id: str
    kind: OcrKind
    text: str


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
    orientationAngle: float | None = None
    tiltAngle: float | None = None
    mirrored: bool | None = None
    pageQualityConfidence: float | None = None
    dosFrom: str | None = None
    dosTo: str | None = None
    dosConfidence: float | None = None
    docDosFrom: str | None = None
    docDosTo: str | None = None
    # None = classification not run → UI shows NA
    # Values: "Yes (Blank)" | "Yes (Junk)" | "No"
    blankOrJunk: str | None = None
    # None = not run → NA; True/False when classified
    isDuplicate: bool | None = None
    # Blank / Main / Duplicate → "Not Available"; Invoice|Cover → that label
    pageType: str | None = None
    pageTypeConfidence: float | None = None

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


class ImagingManifestDetails(BaseModel):
    """Expected manifest identity for the chart.

    Local Mode → data/metadata/metadata_R*_B*.csv
    Production Mode → manifest_member_list
    """

    member: str | None = None
    dob: str | None = None
    memberId: str | None = None


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
    rotation: bool = False
    junk: bool = False
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
