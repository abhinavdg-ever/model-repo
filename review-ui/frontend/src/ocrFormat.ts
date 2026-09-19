/**
 * Format Final (OSS/AzDocInt) OCR for the review panel.
 *
 * Docling markdown conventions we honour:
 *   ## Heading        → section header (document-processing: ≤3 words preferred,
 *                       but Docling already decided — keep any ## / # line)
 *   <!-- image -->    → [image] placeholder
 *   | table | cells | → left as-is (monospace already renders pipes)
 */

export type OcrDisplayLine =
  | { kind: "heading"; level: number; text: string }
  | { kind: "image" }
  | { kind: "text"; text: string };

const IMAGE_RE = /<!--\s*image\s*-->/gi;
const HEADING_RE = /^(#{1,6})\s+(.+)$/;

/** Document-processing heading filter: short labels, strip trailing punctuation. */
export function isLikelySectionHeader(raw: string): boolean {
  const cleaned = raw
    .trim()
    .replace(/[:*,;.]+$/g, "")
    .replace(/\s+/g, " ");
  if (!cleaned) return false;
  const words = cleaned.split(" ");
  if (words.length > 6) return false;
  // ALL CAPS or Title Case short lines (common form headers without ##)
  const letters = cleaned.replace(/[^A-Za-z]/g, "");
  if (letters.length < 2) return false;
  const upper = letters.toUpperCase() === letters;
  return upper || words.length <= 3;
}

export function prepareOcrLines(
  text: string,
  opts: { showSectionHeaders: boolean; detectPlainHeaders?: boolean },
): OcrDisplayLine[] {
  const normalized = (text || "").replace(IMAGE_RE, "\n<!-- image -->\n");
  const out: OcrDisplayLine[] = [];

  for (const raw of normalized.split("\n")) {
    const line = raw.replace(/\s+$/g, "");
    const trimmed = line.trim();
    if (!trimmed) {
      out.push({ kind: "text", text: "" });
      continue;
    }
    if (/^<!--\s*image\s*-->$/i.test(trimmed)) {
      out.push({ kind: "image" });
      continue;
    }
    const md = HEADING_RE.exec(trimmed);
    if (md) {
      const level = md[1].length;
      const headingText = md[2].trim();
      if (opts.showSectionHeaders) {
        out.push({ kind: "heading", level, text: headingText });
      } else {
        // Flat body: keep the words, drop the markdown markers.
        out.push({ kind: "text", text: headingText });
      }
      continue;
    }
    if (
      opts.showSectionHeaders &&
      opts.detectPlainHeaders &&
      isLikelySectionHeader(trimmed)
    ) {
      out.push({ kind: "heading", level: 2, text: trimmed });
      continue;
    }
    out.push({ kind: "text", text: line });
  }
  return out;
}
