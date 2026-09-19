export type OcrRunStatus =
  | "QUEUED"
  | "IN_PROGRESS"
  | "COMPLETED"
  | "IMAGING_IN_PROGRESS"
  | "IMAGING_COMPLETED"
  | "FAILED";

export type FolderSummary = {
  id: string;
  name: string;
  page_count: number;
  ocr_processed: number;
  imaging_processed: number;
  ocr_status: OcrRunStatus;
  last_updated_at: string | null;
  run_id?: string | null;
  batch_id?: string | null;
};

export type PageSummary = {
  page_number: number;
  filename: string;
  image_url: string;
  has_preliminary_ocr: boolean;
  has_final1_ocr: boolean;
  has_final2_ocr: boolean;
  has_imaging?: boolean;
};

export type FolderDetail = FolderSummary & {
  pages: PageSummary[];
};

/** preliminary = Tess (_prelim), final1 = OSS/Docling (_final1.json), final2 = AzDocInt (_final2.json) */
export type OcrKind = "preliminary" | "final1" | "final2";

export type OutputMode = "ocr" | "imaging";

export type OcrSectionHeader = {
  text: string;
  level: number;
  left: number;
  top: number;
  width: number;
  height: number;
};

export type OcrTextResponse = {
  folder_id: string;
  kind: OcrKind;
  text: string;
  section_headers_by_file?: Record<string, OcrSectionHeader[]>;
};

export type ImagingPageResult = {
  pageNumber: number;
  fileName: string;
  memberName: string | null;
  memberDob: string | null;
  memberId: string | null;
  memberConfidence: number | null;
  handwrittenOrPrinted: string | null;
  handwrittenOrPrintedConfidence?: number | null;
  orientationAngle: number | null;
  tiltAngle: number | null;
  mirrored: boolean | null;
  pageQualityTag?: string | null;
  pageQualityConfidence: number | null;
  dosFrom: string | null;
  dosTo: string | null;
  dosConfidence: number | null;
  docDosFrom?: string | null;
  docDosTo?: string | null;
  /** null = not classified yet → UI shows NA; else "Yes (Blank)" | "Yes (Junk)" | "No" */
  blankOrJunk?: string | null;
  /** null = not classified → NA */
  isDuplicate?: boolean | null;
  /** Blank/Main/Duplicate → Not Available; Invoice|Cover → type */
  pageType: string | null;
  pageTypeConfidence: number | null;
};

export type ImagingManifestDetails = {
  member: string | null;
  dob: string | null;
  memberId: string | null;
};

export type ImagingVerificationDetails = {
  finalStatus: string | null;
  matchedName?: string | null;
  matchedMemberId?: string | null;
  matchedConfidence: number | null;
  pagesMatched: number | null;
  pagesChecked: number | null;
  decisionReason: string | null;
};

export type ImagingSectionsProcessed = {
  member: boolean;
  dos: boolean;
  hw: boolean;
  quality?: boolean;
  rotation: boolean;
  junk: boolean;
  verification: boolean;
};

export type ImagingDocumentResponse = {
  folder_id: string;
  manifest: ImagingManifestDetails;
  verification?: ImagingVerificationDetails | null;
  verifications?: ImagingVerificationDetails[];
  pages: ImagingPageResult[];
  sectionsProcessed?: ImagingSectionsProcessed;
};

export type BlobAuthMode = "entra" | "sas";

export type AppConfig = {
  data_mode: string;
  mode_label?: string;
  file_viewer_blob_enabled: boolean;
  blob_auth_mode: BlobAuthMode;
  blob_account_url: string;
  blob_container: string;
  blob_path_template: string;
  blob_entra_ready: boolean;
  blob_auth_required: boolean;
  blob_sas_configured: boolean;
};

export const OCR_TAB_LABELS: Record<OcrKind, string> = {
  preliminary: "Preliminary (Tess)",
  final1: "Final (OSS)",
  final2: "Final (AzDocInt)",
};

export const OCR_STATUS_LABELS: Record<OcrRunStatus, string> = {
  QUEUED: "Queued",
  IN_PROGRESS: "OCR in Progress",
  COMPLETED: "OCR Completed",
  IMAGING_IN_PROGRESS: "Imaging in Progress",
  IMAGING_COMPLETED: "Imaging Completed",
  FAILED: "Failed",
};

/** R1 / Run1 / 1 → "Run 1". Pass-through when the shape is unknown. */
export function formatRunLabel(raw: string | null | undefined): string {
  const s = (raw || "").trim();
  if (!s) return "—";
  const m = /^(?:run[_.\-\s]*)?r?(\d+)$/i.exec(s);
  return m ? `Run ${m[1]}` : s;
}

/** B1 / Batch1 / 1 → "Batch 1". Pass-through when the shape is unknown. */
export function formatBatchLabel(raw: string | null | undefined): string {
  const s = (raw || "").trim();
  if (!s) return "—";
  const m = /^(?:batch[_.\-\s]*)?b?(\d+)$/i.exec(s);
  return m ? `Batch ${m[1]}` : s;
}

async function api<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export function getAppConfig(): Promise<AppConfig> {
  return api("/api/config");
}

export function listFolders(): Promise<FolderSummary[]> {
  return api("/api/folders");
}

export function getFolder(folderId: string): Promise<FolderDetail> {
  return api(`/api/folders/${encodeURIComponent(folderId)}`);
}

export function getFolderOcr(folderId: string, kind: OcrKind): Promise<OcrTextResponse> {
  const params = new URLSearchParams({ kind });
  return api(`/api/folders/${encodeURIComponent(folderId)}/ocr?${params}`);
}

export function getFolderImaging(folderId: string): Promise<ImagingDocumentResponse> {
  return api(`/api/folders/${encodeURIComponent(folderId)}/imaging`);
}

/** URL for History “Download Imaging CSV” (optional status / search filters). */
export function imagingExportCsvUrl(opts?: {
  status?: OcrRunStatus | "ALL";
  q?: string;
}): string {
  const params = new URLSearchParams();
  if (opts?.status && opts.status !== "ALL") {
    params.set("status", opts.status);
  }
  const q = opts?.q?.trim();
  if (q) params.set("q", q);
  const qs = params.toString();
  return qs ? `/api/imaging/export.csv?${qs}` : "/api/imaging/export.csv";
}

export function pageImageUrl(folderId: string, pageNumber: number): string {
  return `/api/folders/${encodeURIComponent(folderId)}/pages/${pageNumber}/image`;
}

export function blobPageImageUrl(folderId: string, pageNumber: number): string {
  return `/api/blob/${encodeURIComponent(folderId)}/pages/${pageNumber}/image`;
}
