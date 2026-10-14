import { groundTruthQuery, readGroundTruthSource } from "./groundTruthSource";

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
  ground_truth_available?: boolean;
  on_disk?: boolean;
};

export type FolderListParams = {
  limit?: number | null;
  offset?: number;
  q?: string;
  status?: OcrRunStatus[];
  run?: string[];
  batch?: string[];
  sort?: "filename" | "pages" | "updated";
  sort_dir?: "asc" | "desc";
};

export type FileViewerFolder = {
  id: string;
  name: string;
  page_count: number;
  has_corrected: boolean;
};

export type FileViewerListResponse = {
  items: FileViewerFolder[];
  total: number;
};

export type FileViewerFolderDetail = FileViewerFolder & {
  pages: PageSummary[];
};

export type FolderListResponse = {
  items: FolderSummary[];
  total: number;
  page_count_sum: number;
  ocr_processed_sum: number;
  run_options: string[];
  batch_options: string[];
  limit: number | null;
  offset: number;
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
  /** From manifest_member_list (SQL) or metadata CSV — available with folder shell. */
  manifest?: ImagingManifestDetails | null;
};

/** preliminary = Tess (_prelim), final1 = OSS/Docling (_final1.json), final2 = AzDocInt (_final2.json) */
export type OcrKind = "preliminary" | "final1" | "final2";

export type OutputMode = "ocr" | "imaging";

export type ExtractionFieldRow = {
  id: string;
  label: string;
  extracted: string;
  processed: string;
  confidence: number | null;
  ground_truth: string;
};

export type ExtractionReviewResponse = {
  folder_id: string;
  available: boolean;
  model_version: string;
  page_file: string;
  fields: ExtractionFieldRow[];
  section_headers: OcrSectionHeader[];
};

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
  /** Printed | Handwritten | Form | Visual | Blank | Uncertain */
  documentType?: string | null;
  /** Page-tag model only. */
  isVisible?: boolean | null;
  /** Page-tag model only, 0–100. */
  handwrittenAreaPct?: number | null;
  orientationAngle: number | null;
  tiltAngle: number | null;
  mirrored: boolean | null;
  pageQualityTag?: string | null;
  pageQualityConfidence: number | null;
  /** Encounter classification — logic TBD; UI shows Yet to Process when absent. */
  encounterType?: string | null;
  dosFrom: string | null;
  dosTo: string | null;
  dosConfidence: number | null;
  docDosFrom?: string | null;
  docDosTo?: string | null;
  /** DOS resolver label. `span` only on runs from before the continuity stage. */
  dosMatch?: string | null;
  /** The page's own date; `continuation` on runs from before the continuity stage. */
  finalDos?: string | null;
  /** Document continuity: which document the page belongs to. */
  documentSeq?: number | null;
  /** single | first | continue | last */
  documentPosition?: string | null;
  /** "First (Doc 3)", "Continue (Doc 3)", "Last (Doc 3)", "Single (Doc 3)" */
  documentLabel?: string | null;
  /** new_document | continue | unknown */
  continuityRelation?: string | null;
  continuityDecidedBy?: string | null;
  continuityEvidence?: string | null;
  /** Final values (imaging_final). Inside a document, its first page's. */
  finalPageType?: string | null;
  /** Codable | Non-Codable | Discharge — follows finalPageType. */
  finalCodeable?: string | null;
  finalDosFrom?: string | null;
  finalDosTo?: string | null;
  /** null = not classified yet → UI shows NA; else "Yes (Blank)" | "Yes (Junk)" | "No" */
  blankOrJunk?: string | null;
  /** null = not classified → NA */
  isDuplicate?: boolean | null;
  /** Page file this page was marked a duplicate of, e.g. "4.png". */
  duplicateOf?: string | null;
  /** Blank/Main/Duplicate → Not Available; Invoice|Cover → type */
  pageType: string | null;
  pageTypeConfidence: number | null;
  /** Accuracy report: semantic similarity of the ground-truth page type (0–1). */
  pageTypeSemantic?: number | null;
  /** Codable | Non-Codable | Discharge (older runs: Codeable / Non Codeable). */
  isCodeable?: string | null;
  /** Pipeline / file order (1-based). */
  currentSequence?: number | null;
  /** Reordered sequence — logic TBD. */
  actualSequence?: number | null;
  /** Name, credentials, and signature from the provider-signature CSV. */
  providerName?: string | null;
  providerCredentials?: string | null;
  providerSignature?: string | null;
  providerSignatureConfidence?: number | null;
  groundTruth?: PageGroundTruth | null;
};

export type PageGroundTruth = {
  pageNumber: number;
  sourcePageId?: string | null;
  memberName?: string | null;
  memberDob?: string | null;
  memberId?: string | null;
  dosFrom?: string | null;
  dosTo?: string | null;
  encounterType?: string | null;
  pageType?: string | null;
  codeable?: string | null;
  blankPage?: string | null;
  junkPage?: string | null;
  isInvoice?: string | null;
  pageSequence?: string | null;
  rotation?: string | null;
  isVisible?: string | null;
  renderingProvider?: string | null;
  /** Yes / No. Older rows hold credentials here. */
  providerSignature?: string | null;
  providerCredentials?: string | null;
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
  codeable?: boolean;
  encounter?: boolean;
  sequencing?: boolean;
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
  /** The mode this browser is in: its Local / DB switch, else DATA_MODE. */
  data_mode: string;
  /** A database is configured, so the DB side of the switch is offered. */
  db_available?: boolean;
  default_mode?: string;
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

/** R1 / Run1 / 1 → "Run 1". Blank (no RunN in the folder path) is "Test". */
export function formatRunLabel(raw: string | null | undefined): string {
  const s = (raw || "").trim();
  if (!s || s.toLowerCase() === "test") return "Test";
  const m = /^(?:run[_.\-\s]*)?r?(\d+)$/i.exec(s);
  return m ? `Run ${m[1]}` : s;
}

/** B1 / Batch1 / 1 → "Batch 1". Blank (no BatchN in the folder path) is "Test". */
export function formatBatchLabel(raw: string | null | undefined): string {
  const s = (raw || "").trim();
  if (!s || s.toLowerCase() === "test") return "Test";
  const m = /^(?:batch[_.\-\s]*)?b?(\d+)$/i.exec(s);
  return m ? `Batch ${m[1]}` : s;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export function getAppConfig(): Promise<AppConfig> {
  return api("/api/config");
}

export function listFolders(params?: FolderListParams): Promise<FolderListResponse> {
  const sp = new URLSearchParams();
  if (params?.limit != null && params.limit > 0) {
    sp.set("limit", String(params.limit));
  }
  if (params?.offset != null && params.offset > 0) {
    sp.set("offset", String(params.offset));
  }
  if (params?.q?.trim()) sp.set("q", params.q.trim());
  for (const s of params?.status ?? []) sp.append("status", s);
  for (const s of params?.run ?? []) sp.append("run", s);
  for (const s of params?.batch ?? []) sp.append("batch", s);
  if (params?.sort) sp.set("sort", params.sort);
  if (params?.sort_dir) sp.set("sort_dir", params.sort_dir);
  const qs = sp.toString();
  return api(`/api/folders${qs ? `?${qs}` : ""}`);
}

export function listFileViewerFolders(): Promise<FileViewerListResponse> {
  return api("/api/file-viewer/folders");
}

export function getFileViewerFolder(folderId: string): Promise<FileViewerFolderDetail> {
  return api(`/api/file-viewer/folders/${encodeURIComponent(folderId)}`);
}

export function fileViewerPageImageUrl(
  folderId: string,
  pageNumber: number,
  corrected = false,
): string {
  const base = `/api/file-viewer/folders/${encodeURIComponent(folderId)}/pages/${pageNumber}/image`;
  return corrected ? `${base}?corrected=1` : base;
}

export function getFolder(folderId: string): Promise<FolderDetail> {
  return api(`/api/folders/${encodeURIComponent(folderId)}`);
}

export function getFolderOcr(folderId: string, kind: OcrKind): Promise<OcrTextResponse> {
  const params = new URLSearchParams({ kind });
  return api(`/api/folders/${encodeURIComponent(folderId)}/ocr?${params}`);
}

export function getFolderImaging(folderId: string): Promise<ImagingDocumentResponse> {
  return api(`/api/folders/${encodeURIComponent(folderId)}/imaging${groundTruthQuery()}`);
}

export type AccuracyChartPayload = {
  chartId: string;
  chartName: string;
  lastUpdatedAt?: string | null;
  lastVerifiedAt?: string | null;
  metrics?: Record<string, { correct: number; wrong: number; scored: number }> | null;
  overall?: { correct: number; wrong: number; scored: number } | null;
  document?: ImagingDocumentResponse | null;
};

/** ``fresh`` skips the backend's minute-long reuse of the report (Refresh). */
export function getAccuracyReport(fresh = false): Promise<{ charts: AccuracyChartPayload[] }> {
  const query = groundTruthQuery();
  const refresh = fresh ? `${query ? "&" : "?"}refresh=true` : "";
  return api(`/api/accuracy${query}${refresh}`);
}


export function savePageGroundTruth(
  folderId: string,
  body: PageGroundTruth,
): Promise<PageGroundTruth> {
  return api(`/api/folders/${encodeURIComponent(folderId)}/ground-truth`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getFolderExtraction(
  folderId: string,
  pageFile: string,
): Promise<ExtractionReviewResponse> {
  const params = new URLSearchParams({ page: pageFile });
  return api(`/api/folders/${encodeURIComponent(folderId)}/extraction?${params}`);
}

/** URL for History bulk imaging export (server-side stream; Landing uses client progress). */
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

export function pageImageUrl(
  folderId: string,
  pageNumber: number,
  opts?: { thumb?: boolean; corrected?: boolean },
): string {
  const base = `/api/folders/${encodeURIComponent(folderId)}/pages/${pageNumber}/image`;
  const params = new URLSearchParams();
  if (opts?.thumb) params.set("thumb", "1");
  if (opts?.corrected) params.set("corrected", "1");
  const qs = params.toString();
  return qs ? `${base}?${qs}` : base;
}

export function blobPageImageUrl(folderId: string, pageNumber: number): string {
  return `/api/blob/${encodeURIComponent(folderId)}/pages/${pageNumber}/image`;
}

export type ManualReviewStatus = "CORRECT" | "ERROR" | "MISSING" | "EXTRA";

export type ManualReviewRow = {
  pdfId: string;
  pageNumber: string;
  /** Field -> the value, or "GT: … | Algo: …" when the two differ. */
  values: Record<string, string>;
  mismatched: string[];
  status: ManualReviewStatus;
  reason: string;
};

export type ManualReviewResult = {
  groundCount: number;
  systemCount: number;
  correctCount: number;
  errorCount: number;
  missingCount: number;
  extraCount: number;
  /** The fields compared this run, in report order. */
  fields: string[];
  exemptedColumns: { column: string; reason: string }[];
  summary: { metric: string; count: number; percentage: number }[];
  rows: ManualReviewRow[];
  /** Chart -> its last pipeline run (ISO); null when never run. Streamed compare only. */
  lastRun?: Record<string, string | null>;
};

/** Each side is an uploaded sheet or the database. Database ground truth follows the
 * user menu's manual-edits choice and covers the system file's PDF IDs (every chart
 * with ground truth when the system side is the database too). Database system output
 * covers the ground truth's PDF IDs. */
export type ManualReviewInput = {
  ground: File | null;
  groundSource: "upload" | "db";
  system: File | null;
  systemSource: "db" | "upload";
};

export type ManualReviewColumns = {
  required: { column: string; aliases: string[] }[];
  compared: { column: string; aliases: string[] }[];
  exempt: string[];
};

function manualReviewForm({ ground, groundSource, system, systemSource }: ManualReviewInput): FormData {
  const form = new FormData();
  form.append("ground_source", groundSource);
  if (groundSource === "upload" && ground) form.append("ground_file", ground);
  if (groundSource === "db") form.append("ground_truth", readGroundTruthSource());
  form.append("system_source", systemSource);
  if (systemSource === "upload" && system) form.append("system_file", system);
  return form;
}

export function getManualReviewColumns(): Promise<ManualReviewColumns> {
  return api("/api/excel-compare/columns");
}

/** FastAPI's {"detail": "..."} as plain text. */
async function errorText(res: Response): Promise<string> {
  const body = await res.text().catch(() => "");
  try {
    const detail = (JSON.parse(body) as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  } catch {
    // not JSON
  }
  return body || `${res.status} ${res.statusText}`;
}

/** Multipart, so not api(): that helper sends JSON. */
export async function compareManualReview(input: ManualReviewInput): Promise<ManualReviewResult> {
  const res = await fetch("/api/excel-compare", { method: "POST", body: manualReviewForm(input) });
  if (!res.ok) throw new Error(await errorText(res));
  return res.json() as Promise<ManualReviewResult>;
}

type ManualReviewCounts = Pick<
  ManualReviewResult,
  "groundCount" | "systemCount" | "correctCount" | "errorCount" | "missingCount" | "extraCount"
>;

export type ManualReviewEvent =
  | ({ type: "start"; charts: number } & Pick<ManualReviewResult, "fields" | "exemptedColumns">)
  | ({ type: "chart"; pdfId: string; lastRunAt: string | null; rows: ManualReviewRow[] } & ManualReviewCounts)
  | ({ type: "done"; summary: ManualReviewResult["summary"] } & ManualReviewCounts)
  | { type: "error"; pdfId: string; detail: string };

/** The comparison one chart at a time (NDJSON), handed to ``onEvent`` as each
 * line arrives. Input errors reject before any event. */
export async function streamManualReview(
  input: ManualReviewInput,
  onEvent: (event: ManualReviewEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch("/api/excel-compare/stream", {
    method: "POST",
    body: manualReviewForm(input),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(await errorText(res));
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffered = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (value) buffered += value;
    const lines = buffered.split("\n");
    buffered = done ? "" : (lines.pop() ?? "");
    for (const line of lines) {
      if (line.trim()) onEvent(JSON.parse(line) as ManualReviewEvent);
    }
    if (done) return;
  }
}

export async function downloadManualReviewReport(input: ManualReviewInput): Promise<Blob> {
  const res = await fetch("/api/excel-compare/report", {
    method: "POST",
    body: manualReviewForm(input),
  });
  if (!res.ok) throw new Error(await errorText(res));
  return res.blob();
}
