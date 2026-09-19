import type { AppConfig } from "./api";

const BLOB_SAS_SESSION_KEY = "advantmed_blob_sas";

export function normalizeSas(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "";
  return trimmed.startsWith("?") ? trimmed.slice(1) : trimmed;
}

export function readSessionSas(): string {
  try {
    return normalizeSas(sessionStorage.getItem(BLOB_SAS_SESSION_KEY) || "");
  } catch {
    return "";
  }
}

export function writeSessionSas(sas: string): void {
  const value = normalizeSas(sas);
  try {
    if (value) sessionStorage.setItem(BLOB_SAS_SESSION_KEY, value);
    else sessionStorage.removeItem(BLOB_SAS_SESSION_KEY);
  } catch {
    /* ignore */
  }
}

export function clearSessionSas(): void {
  try {
    sessionStorage.removeItem(BLOB_SAS_SESSION_KEY);
  } catch {
    /* ignore */
  }
}

export function buildBlobObjectUrl(
  config: AppConfig,
  folderId: string,
  filename: string,
  pageNumber: number,
  sas: string,
): string {
  const account = config.blob_account_url.replace(/\/+$/, "");
  const container = config.blob_container.replace(/^\/+|\/+$/g, "");
  if (!account || !container) return "";

  const key = (config.blob_path_template || "{folder}/pages/{filename}")
    .replace(/\{folder\}/g, folderId)
    .replace(/\{filename\}/g, filename)
    .replace(/\{page\}/g, String(pageNumber))
    .replace(/^\/+/, "");

  const query = normalizeSas(sas);
  const base = `${account}/${container}/${key}`;
  return query ? `${base}?${query}` : base;
}

/** True when Blob mode can be used without further UI prompts. */
export function isBlobReady(config: AppConfig): boolean {
  if (!config.file_viewer_blob_enabled) return false;
  if (config.blob_auth_mode === "entra") return config.blob_entra_ready;
  if (config.blob_sas_configured) return true;
  return Boolean(readSessionSas());
}

export function hasBlobSessionAuth(config: AppConfig): boolean {
  return isBlobReady(config);
}
