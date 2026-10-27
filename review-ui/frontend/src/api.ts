import { groundTruthQuery, readGroundTruthSource } from "./groundTruthSource";
import { readSession, type Session } from "./session";

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
  /** Chart metadata (manifest / metadata CSV). */
  project_scope?: string | null;
  source_name?: string | null;
  measure?: string | null;
  rr_method?: string | null;
};

export type FolderListParams = {
  limit?: number | null;
  offset?: number;
  q?: string;
  status?: OcrRunStatus[];
  run?: string[];
  batch?: string[];
  /** Project Scope. */
  scope?: string[];
  source?: string[];
  measure?: string[];
  /** RR Method. */
  retrieval?: string[];
  /** Only charts last run at or after this ISO date-time (with its offset). */
  run_after?: string | null;
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
  /** Imaging Completed charts among the matches, their pages, and those with ground truth. */
  imaging_completed_charts?: number;
  imaging_completed_pages?: number;
  ground_truth_charts?: number;
  run_options: string[];
  batch_options: string[];
  scope_options?: string[];
  source_options?: string[];
  measure_options?: string[];
  retrieval_options?: string[];
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

/** Every page's extraction review for a chart, by page file name. */
export type ExtractionPagesResponse = {
  folder_id: string;
  available: boolean;
  pages: Record<string, ExtractionReviewResponse>;
};

export type OcrSectionHeader = {
  text: string;
  level: number;
  left: number;
  top: number;
  width: number;
  height: number;
  /** A heading candidate that did not pass: shown and drawn lighter. */
  candidate?: boolean;
  /** A candidate that still reads like a heading: heavier than a form label. */
  heading_like?: boolean;
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
  /** Stage 1's measured values' confidences: orientation, mirrored, tilt, visibility, handwritten_area. */
  measuredConfidences?: Record<string, number> | null;
  /** The subtype's own confidence (the deepest named level), apart from the page type's. */
  subtypeConfidence?: number | null;
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
  /** Final specialty (imaging_final): the page's own. */
  finalSpecialty?: string | null;
  /** Each Final value's confidence (imaging_final.confidences): >= 0.8 good, 0.5–0.8
   * passable, < 0.5 review. Keys: page_type, codability, specialty, dos, encounter_type,
   * member, provider, blank_junk, handwriting. */
  finalConfidences?: Record<string, number> | null;
  /** The Final values below the review line. */
  reviewFields?: string[] | null;
  blankJunkConfidence?: number | null;
  encounterConfidence?: number | null;
  sequenceConfidence?: number | null;
  /** null = not classified yet → UI shows NA; else "Yes (Blank)" | "Yes (Junk)" | "No" */
  blankOrJunk?: string | null;
  /** null = not classified → NA */
  isDuplicate?: boolean | null;
  /** 95–98% alike: a partial duplicate; the page keeps its own verdict (Main or junk). */
  isPartialDuplicate?: boolean | null;
  /** The page's highest duplicate similarity (0–1), whatever its verdict. */
  duplicateConfidence?: number | null;
  /** Page file this page was marked a duplicate of, e.g. "4.png". */
  duplicateOf?: string | null;
  /** A duplicate's content values (member, DOS, page type, provider ...) are its
   * original's (``withDuplicateValues``): the original's file name. */
  valuesFrom?: string | null;
  /** Blank/Main/Duplicate → Not Available; Invoice|Cover → type */
  pageType: string | null;
  pageTypeConfidence: number | null;
  /** Page-axes specialty; no ground truth exists for it. */
  specialty?: string | null;
  specialtyConfidence?: number | null;
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
  /** Every accepted signer, the selected one first. None on rows from before 2026-10-18. */
  providerSigners?: { name?: string | null; credentials?: string | null; signatureDate?: string | null }[] | null;
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
  /** Free text (2026-10-27, user): page_subtype and provider_specialty. */
  pageSubtype?: string | null;
  specialty?: string | null;
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

/** One encounter staged by the pipeline (staging/continuity.json → encounters). */
export type StagedEncounter = {
  seq: number;
  document?: number | null;
  page_family?: string | null;
  pages: string[];
  dos_from?: string | null;
  dos_to?: string | null;
  encounter_type?: string | null;
  encounter_label?: string | null;
  confidence?: number | null;
  decided_by?: string | null;
  reason?: string | null;
  matched_keyword?: string | null;
};

export type ImagingDocumentResponse = {
  folder_id: string;
  manifest: ImagingManifestDetails;
  verification?: ImagingVerificationDetails | null;
  verifications?: ImagingVerificationDetails[];
  pages: ImagingPageResult[];
  sectionsProcessed?: ImagingSectionsProcessed;
  /** Staged encounter grouping; empty or absent = derive it from documentSeq. */
  encounters?: StagedEncounter[];
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
  /** Playground (admins): CORE_PIPELINE_URLS is set on the server; the first is the default. */
  playground_enabled?: boolean;
  playground_targets?: { label: string; url: string }[];
  playground_container?: string;
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
  // Every request carries the signed-in user, so a ground-truth save records who made it.
  const token = readSession()?.token;
  const res = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

/** Admins only: delete a chart and its pipeline results (ground truth, manifest and files
 * on disk are kept). */
export function deleteFolder(folderId: string): Promise<{ chart_name: string; page_count: number | null }> {
  return api(`/api/folders/${encodeURIComponent(folderId)}`, { method: "DELETE" });
}

export type PlaygroundRunRequest = {
  /** One of playground_targets. */
  target: string;
  container_name: string;
  input_path: string;
  chart_list: string;
  skip_ocr: boolean;
  skip_completed: boolean;
};

export type PlaygroundRunResponse = {
  /** The core-pipeline the run went to, and its label (VM / Local). */
  target: string;
  target_label: string;
  /** What was sent to core-pipeline's batch-run, output_path derived. */
  request: Record<string, unknown> & { input_path: string; output_path: string };
  /** core-pipeline's answer (queued in the background). */
  pipeline: Record<string, unknown>;
};

/** Admins only: start a core-pipeline batch run over a blob folder. */
export function startPlaygroundRun(body: PlaygroundRunRequest): Promise<PlaygroundRunResponse> {
  return api("/api/playground/batch-run", { method: "POST", body: JSON.stringify(body) });
}

export type UserRole = "reviewer" | "admin" | "viewer";

export type UserRow = {
  username: string;
  name: string;
  role: UserRole;
  isActive: boolean;
  lastLoginAt: string | null;
};

/** Admins only: every user (no passwords). */
export function listUsers(): Promise<UserRow[]> {
  return api("/api/users");
}

/** Admins only: a new user; fails (409) when the username is taken. */
export function createUser(body: { username: string; name: string; password: string; role: UserRole }): Promise<UserRow> {
  return api("/api/users", { method: "POST", body: JSON.stringify(body) });
}

/** Admins only: a new password for an existing user; fails (404) when there is none. */
export function resetUserPassword(body: { username: string; password: string }): Promise<UserRow> {
  return api("/api/users/password", { method: "PUT", body: JSON.stringify(body) });
}

export type OcrSearchSnippet = { before: string; match: string; after: string };
export type OcrSearchPage = { page_number: number | null; page_name: string; hits: number; snippets: OcrSearchSnippet[] };
/** A section header that matches: its page, its text and the match within it. */
export type OcrSearchHeader = {
  page_number: number | null;
  page_name: string;
  text: string;
  hits: number;
  snippet: OcrSearchSnippet;
};
export type OcrSearchChart = {
  chart: string;
  run: string;
  batch: string;
  /** Every page of the chart; null when the chart has no page_list rows. */
  total_pages: number | null;
  /** Word hits in the Tesseract text, and the pages they are on. */
  hits: number;
  pages: OcrSearchPage[];
  /** Section headers that match, and how many pages they are on. */
  header_hits: number;
  header_pages: number;
  headers: OcrSearchHeader[];
};
export type OcrSearchBatch = { run: string; batch: string; charts: number; pages: number; hits: number; header_hits: number };
export type OcrSearchResult = {
  query: string;
  whole_word: boolean;
  charts: OcrSearchChart[];
  batches: OcrSearchBatch[];
  total_charts: number;
  total_pages: number;
  total_hits: number;
  total_header_hits: number;
  truncated: boolean;
  max_pages: number;
};

/** Admins only: pages whose Tesseract text has the word or phrase. */
export function searchOcr(params: { q: string; wholeWord: boolean; run?: string; batch?: string }): Promise<OcrSearchResult> {
  const qs = new URLSearchParams({ q: params.q, whole_word: String(params.wholeWord) });
  if (params.run?.trim()) qs.set("run", params.run.trim());
  if (params.batch?.trim()) qs.set("batch", params.batch.trim());
  return api(`/api/search/ocr?${qs.toString()}`);
}

/** The server's ``detail`` from a failed call's message, else the message. */
export function errorDetail(err: unknown, fallback: string): string {
  const raw = err instanceof Error ? err.message : String(err);
  try {
    const detail = (JSON.parse(raw) as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((d) => (d as { msg?: string }).msg ?? String(d)).join("; ");
    return raw || fallback;
  } catch {
    return raw || fallback;
  }
}

/** Sign in; throws with the server's message ("Invalid username or password.") on failure. */
export async function login(username: string, password: string): Promise<Session> {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    let detail = "";
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? "";
    } catch {
      /* not JSON */
    }
    throw new Error(detail || (res.status === 401 ? "Invalid username or password." : "Sign-in failed."));
  }
  return (await res.json()) as Session;
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
  for (const s of params?.scope ?? []) sp.append("scope", s);
  for (const s of params?.source ?? []) sp.append("source", s);
  for (const s of params?.measure ?? []) sp.append("measure", s);
  for (const s of params?.retrieval ?? []) sp.append("retrieval", s);
  if (params?.run_after) sp.set("run_after", params.run_after);
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
  return api<ImagingDocumentResponse>(`/api/folders/${encodeURIComponent(folderId)}/imaging${groundTruthQuery()}`)
    .then(withDuplicateValues);
}

/** What a duplicate takes from its original: every content value. Its own image values
 * (quality, orientation, handwriting), blank / junk and duplicate status, sequence and
 * ground truth stay its own. */
const DUPLICATE_COPIED: (keyof ImagingPageResult)[] = [
  "memberName", "memberDob", "memberId", "memberConfidence",
  "encounterType", "encounterConfidence",
  "dosFrom", "dosTo", "dosConfidence", "docDosFrom", "docDosTo", "dosMatch", "finalDos",
  "finalDosFrom", "finalDosTo", "finalPageType", "finalCodeable", "finalSpecialty",
  "pageType", "pageTypeConfidence", "subtypeConfidence", "specialty", "specialtyConfidence",
  "pageTypeSemantic", "isCodeable",
  "providerName", "providerCredentials", "providerSignature", "providerSignatureConfidence", "providerSigners",
];

/** 2026-10-26 (user): a duplicate page shows, and is scored on, its original's values
 * (the stages after blank / junk skip duplicates, so it has none of its own). */
/** A page with no Blank / Junk verdict whose Type (Printed/HW) is Blank reads
 * "Yes (Blank)", on the page and in the accuracy report (2026-10-27, user): the image
 * stage saw no ink, so the blank / junk stage had no text to judge. */
function withBlankFromType(page: ImagingPageResult): ImagingPageResult {
  if (String(page.blankOrJunk ?? "").trim()) return page;
  const type = String(page.documentType || page.handwrittenOrPrinted || "").trim().toLowerCase();
  return type === "blank" ? { ...page, blankOrJunk: "Yes (Blank)" } : page;
}

export function withDuplicateValues(doc: ImagingDocumentResponse): ImagingDocumentResponse {
  doc = doc.pages.some((page) => withBlankFromType(page) !== page)
    ? { ...doc, pages: doc.pages.map(withBlankFromType) }
    : doc;
  const byName = new Map(doc.pages.map((page) => [page.fileName, page]));
  let changed = false;
  const pages = doc.pages.map((page) => {
    const original = page.isDuplicate === true && page.duplicateOf ? byName.get(page.duplicateOf) : undefined;
    if (!original || original === page) return page;
    changed = true;
    const copied: Partial<ImagingPageResult> = { valuesFrom: original.fileName };
    for (const key of DUPLICATE_COPIED) (copied as Record<string, unknown>)[key] = original[key];
    const own = page.finalConfidences ?? {};
    copied.finalConfidences = {
      ...(original.finalConfidences ?? {}),
      ...Object.fromEntries(Object.entries(own).filter(([k]) => k === "blank_junk" || k === "handwriting")),
    };
    return { ...page, ...copied };
  });
  return changed ? { ...doc, pages } : doc;
}

export type AccuracyChartPayload = {
  chartId: string;
  chartName: string;
  lastUpdatedAt?: string | null;
  lastVerifiedAt?: string | null;
  runId?: string | null;
  batchId?: string | null;
  projectScope?: string | null;
  sourceName?: string | null;
  measure?: string | null;
  rrMethod?: string | null;
  metrics?: Record<string, { correct: number; wrong: number; scored: number }> | null;
  overall?: { correct: number; wrong: number; scored: number } | null;
  document?: ImagingDocumentResponse | null;
};

/** ``fresh`` skips the backend's minute-long reuse of the report (Refresh). */
export function getAccuracyReport(fresh = false): Promise<{ charts: AccuracyChartPayload[] }> {
  const query = groundTruthQuery();
  const refresh = fresh ? `${query ? "&" : "?"}refresh=true` : "";
  return api<{ charts: AccuracyChartPayload[] }>(`/api/accuracy${query}${refresh}`).then((report) => ({
    ...report,
    charts: report.charts.map((chart) =>
      chart.document ? { ...chart, document: withDuplicateValues(chart.document) } : chart,
    ),
  }));
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

/** Set the ground-truth actual sequence of the given pages; returns the chart's
 * ground-truth pages. */
export function savePageSequences(
  folderId: string,
  pages: { pageNumber: number; sourcePageId?: string | null; pageSequence: string | null }[],
): Promise<PageGroundTruth[]> {
  return api(`/api/folders/${encodeURIComponent(folderId)}/ground-truth/sequence`, {
    method: "PUT",
    body: JSON.stringify({ pages }),
  });
}

/** A reviewer's encounter: a page range and its values. Not ground truth. */
export type EncounterReviewRow = {
  seq: number;
  startPage: number;
  endPage: number;
  pageType: string | null;
  encounterType: string | null;
  dosFrom: string | null;
  dosTo: string | null;
};

export type EncounterReview = {
  rows: EncounterReviewRow[];
  savedAt: string | null;
  savedBy: string | null;
};

export function getEncounterReview(folderId: string): Promise<EncounterReview> {
  return api(`/api/folders/${encodeURIComponent(folderId)}/encounter-review`);
}

/** Replace the chart's reviewed encounters. */
export function saveEncounterReview(
  folderId: string,
  rows: EncounterReviewRow[],
): Promise<EncounterReview> {
  return api(`/api/folders/${encodeURIComponent(folderId)}/encounter-review`, {
    method: "PUT",
    body: JSON.stringify({ rows }),
  });
}

export function getFolderExtraction(
  folderId: string,
  pageFile: string,
): Promise<ExtractionReviewResponse> {
  const params = new URLSearchParams({ page: pageFile });
  return api(`/api/folders/${encodeURIComponent(folderId)}/extraction?${params}`);
}

/** Every page's extraction review in one request (once per chart, like imaging). */
export function getFolderExtractionPages(folderId: string): Promise<ExtractionPagesResponse> {
  return api(`/api/folders/${encodeURIComponent(folderId)}/extraction/pages`);
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

// Bumped by retryPageImages so every page image URL changes and the browser asks again
// instead of reusing the failed response.
let imageAttempt = 0;

export function pageImageUrl(
  folderId: string,
  pageNumber: number,
  opts?: { thumb?: boolean; corrected?: boolean },
): string {
  const base = `/api/folders/${encodeURIComponent(folderId)}/pages/${pageNumber}/image`;
  const params = new URLSearchParams();
  if (opts?.thumb) params.set("thumb", "1");
  if (opts?.corrected) params.set("corrected", "1");
  if (imageAttempt > 0) params.set("attempt", String(imageAttempt));
  const qs = params.toString();
  return qs ? `${base}?${qs}` : base;
}

/** Ask the backend to try Azure Blob again, then load every page image afresh. */
export async function retryPageImages(): Promise<string> {
  imageAttempt += 1;
  try {
    const res = await api<{ blob: string }>("/api/blob/retry", { method: "POST" });
    return res.blob;
  } catch {
    return "";
  }
}

export function blobPageImageUrl(folderId: string, pageNumber: number): string {
  return `/api/blob/${encodeURIComponent(folderId)}/pages/${pageNumber}/image`;
}

/** UNVALIDATED: matched on PDF ID + Page Number with no field to compare --
 * not evidence the fields agree. EXCLUDED: both sides call the page Blank, or both
 * Junk -- matched, not evaluated. */
export type ManualReviewStatus = "CORRECT" | "ERROR" | "MISSING" | "EXTRA" | "UNVALIDATED" | "EXCLUDED";

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
  unvalidatedCount: number;
  /** Blank/Blank + Junk/Junk pages: matched, not evaluated. */
  excludedCount?: number;
  /** CORRECT + ERROR + UNVALIDATED + EXCLUDED. Set once the comparison is done. */
  matchedCount?: number;
  /** Matched minus EXCLUDED, and CORRECT / that. Set once the comparison is done. */
  evaluatedCount?: number;
  evaluatedAccuracyRate?: number | null;
  /** Distinct PDF IDs. Set once the comparison is done. */
  groundCharts?: number;
  systemCharts?: number;
  chartsCompared?: number;
  /** Matched records that disagreed on each field, most first. Set once done. */
  fieldErrors?: { field: string; count: number; percentage: number }[];
  /** Names this finished comparison for the downloads, which then build from it
   * instead of comparing again. Absent while streaming or after a Stop. */
  reportId?: string;
  /** The fields compared this run, in report order. */
  fields: string[];
  exemptedColumns: { column: string; reason: string }[];
  /** percentage is null where the summary says N/A (nothing compared) or "-" (chart counts). */
  summary: { metric: string; count: number; percentage: number | null }[];
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
  "groundCount" | "systemCount" | "correctCount" | "errorCount" | "missingCount" | "extraCount" | "unvalidatedCount"
>;

type ManualReviewTotals = Pick<
  ManualReviewResult,
  "matchedCount" | "groundCharts" | "systemCharts" | "chartsCompared" | "fieldErrors" | "reportId"
>;

export type ManualReviewEvent =
  | ({ type: "start"; charts: number } & Pick<ManualReviewResult, "fields" | "exemptedColumns">)
  | ({ type: "chart"; pdfId: string; lastRunAt: string | null; rows: ManualReviewRow[] } & ManualReviewCounts)
  | ({ type: "done"; summary: ManualReviewResult["summary"] } & ManualReviewCounts & ManualReviewTotals)
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

/** ``reportId`` (the comparison on screen) builds the file without comparing again;
 * the inputs are sent too, for when the backend no longer has that comparison. */
function downloadForm(input: ManualReviewInput, reportId?: string): FormData {
  const form = manualReviewForm(input);
  if (reportId) form.append("report_id", reportId);
  return form;
}

export async function downloadManualReviewReport(input: ManualReviewInput, reportId?: string): Promise<Blob> {
  const res = await fetch("/api/excel-compare/report", {
    method: "POST",
    body: downloadForm(input, reportId),
  });
  if (!res.ok) throw new Error(await errorText(res));
  return res.blob();
}

/** The printable Master Summary (PDF) of the same comparison. */
export async function downloadManualReviewSummary(input: ManualReviewInput, reportId?: string): Promise<Blob> {
  const res = await fetch("/api/excel-compare/summary", {
    method: "POST",
    body: downloadForm(input, reportId),
  });
  if (!res.ok) throw new Error(await errorText(res));
  return res.blob();
}
