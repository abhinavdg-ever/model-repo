/**
 * Split a full-document OCR text file into per-page chunks keyed by filename.
 *
 * Markers match the page image name exactly, e.g.:
 *   ===== 1.jpg =====
 *   ===== 2.jpg =====
 *   ===== page_0001.jpg =====
 */
const FILE_HEADER_RE = /(?:^|\n)\s*={3,}\s*([^\n=]+?\.(?:jpe?g|png|webp|tif{1,2}))\s*={3,}\s*/gi;

export function splitOcrByFilename(fullText: string): Map<string, string> {
  const text = fullText.replace(/\r\n/g, "\n");
  const pages = new Map<string, string>();

  if (!text.trim()) return pages;

  const matches = [...text.matchAll(FILE_HEADER_RE)];
  if (matches.length === 0) {
    return pages;
  }

  for (let i = 0; i < matches.length; i++) {
    const match = matches[i];
    const filename = (match[1] || "").trim();
    if (!filename) continue;

    const contentStart = (match.index ?? 0) + match[0].length;
    const contentEnd =
      i + 1 < matches.length ? (matches[i + 1].index ?? text.length) : text.length;
    const body = text.slice(contentStart, contentEnd).trim();
    pages.set(filename, body);
    // Also index case-insensitively for lookups
    pages.set(filename.toLowerCase(), body);
  }

  return pages;
}

export function ocrTextForFilename(fullText: string, filename: string): string {
  if (!fullText.trim() || !filename) return "";
  const pages = splitOcrByFilename(fullText);
  return pages.get(filename) ?? pages.get(filename.toLowerCase()) ?? "";
}
