/**
 * Split a full-document OCR text file into per-page chunks keyed by filename.
 *
 * Markers match the page image name exactly, e.g.:
 *   ===== 1.jpg =====
 *   ===== 2.jpg =====
 *   ===== page_0001.jpg =====
 */
// One marker per line. Spaces around it, never newlines: a page with no text (a blank page)
// is a marker followed straight by the next one, and a \s* that ate the newline hid the next
// marker, so the blank page showed the next page's text and that page showed none (2026-11-12).
const FILE_HEADER_RE = /^[ \t]*={3,}[ \t]*([^\n=]+?\.(?:jpe?g|png|webp|tif{1,2}))[ \t]*={3,}[ \t]*$/gim;

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

// Full-chart OCR text is split once per text, not once per lookup. The viewer
// looks up the current page for every engine on every page flip, and each
// split is a regex pass over the whole chart. One entry per OCR kind in play.
const SPLIT_CACHE_MAX = 6;
const splitCache = new Map<string, Map<string, string>>();

function cachedSplit(fullText: string): Map<string, string> {
  const hit = splitCache.get(fullText);
  if (hit) return hit;
  const pages = splitOcrByFilename(fullText);
  if (splitCache.size >= SPLIT_CACHE_MAX) {
    const oldest = splitCache.keys().next().value;
    if (oldest !== undefined) splitCache.delete(oldest);
  }
  splitCache.set(fullText, pages);
  return pages;
}

export function ocrTextForFilename(fullText: string, filename: string): string {
  if (!fullText.trim() || !filename) return "";
  const pages = cachedSplit(fullText);
  return pages.get(filename) ?? pages.get(filename.toLowerCase()) ?? "";
}
