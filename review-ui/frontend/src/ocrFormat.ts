/**
 * Format Final (OSS/AzDocInt) OCR for the review panel.
 *
 * Docling markdown conventions we honour:
 *   ## Heading        → section header
 *   <!-- image -->    → stripped (noise)
 *   empty |---| tables → stripped
 */

export type OcrDisplayLine =
  | { kind: "heading"; level: number; text: string }
  | { kind: "text"; text: string };

const IMAGE_RE = /<!--\s*image\s*-->/gi;
const HEADING_RE = /^(#{1,6})\s+(.+)$/;
const EMPTY_TABLE_ROW = /^\|?[\s\-:|]+\|?$/;
const SKIP_MESSAGES = [
  "Skipped for High Quality Images",
  "Skipped for Blank/Junk",
];

/** Document-processing heading filter: short labels, strip trailing punctuation. */
export function isLikelySectionHeader(raw: string): boolean {
  const cleaned = raw
    .trim()
    .replace(/[:*,;.]+$/g, "")
    .replace(/\s+/g, " ");
  if (!cleaned) return false;
  const words = cleaned.split(" ");
  if (words.length > 6) return false;
  const letters = cleaned.replace(/[^A-Za-z]/g, "");
  if (letters.length < 2) return false;
  const upper = letters.toUpperCase() === letters;
  return upper || words.length <= 3;
}

/** Strip Docling noise for display (also applied server-side for new runs). */
export function cleanOcrDisplayText(text: string): string {
  if (!text) return "";
  if (SKIP_MESSAGES.some((m) => text.trim() === m)) return text.trim();
  let out = text.replace(IMAGE_RE, "");
  out = out.replace(/^\s*\[image\]\s*$/gim, "");
  const lines: string[] = [];
  for (const raw of out.split("\n")) {
    const stripped = raw.trim();
    if (!stripped) {
      lines.push("");
      continue;
    }
    if (EMPTY_TABLE_ROW.test(stripped)) continue;
    if (stripped.startsWith("|")) {
      const cells = stripped
        .replace(/^\|/, "")
        .replace(/\|$/, "")
        .split("|")
        .map((c) => c.trim());
      if (cells.every((c) => !c)) continue;
    }
    lines.push(raw.replace(/\s+$/g, ""));
  }
  const collapsed: string[] = [];
  let blank = false;
  for (const line of lines) {
    if (!line.trim()) {
      if (blank) continue;
      collapsed.push("");
      blank = true;
    } else {
      collapsed.push(line);
      blank = false;
    }
  }
  return collapsed.join("\n").trim();
}

function normKey(s: string): string {
  return s
    .trim()
    .toLowerCase()
    .replace(/[:*\-–—|/]+$/g, "")
    .replace(/\s+/g, " ");
}

export function prepareOcrLines(
  text: string,
  opts: {
    showSectionHeaders: boolean;
    detectPlainHeaders?: boolean;
    /** Known section-header texts from Final1 JSON (matched_canonical / text). */
    knownHeaders?: string[];
  },
): OcrDisplayLine[] {
  const cleaned = cleanOcrDisplayText(text || "");
  if (!cleaned) return [];
  if (SKIP_MESSAGES.some((m) => cleaned === m)) {
    return [{ kind: "text", text: cleaned }];
  }

  const known = new Set((opts.knownHeaders || []).map(normKey).filter(Boolean));
  const out: OcrDisplayLine[] = [];

  for (const raw of cleaned.split("\n")) {
    const line = raw.replace(/\s+$/g, "");
    const trimmed = line.trim();
    if (!trimmed) {
      out.push({ kind: "text", text: "" });
      continue;
    }
    const md = HEADING_RE.exec(trimmed);
    if (md) {
      const level = md[1].length;
      const headingText = md[2].trim();
      if (opts.showSectionHeaders) {
        out.push({ kind: "heading", level, text: headingText });
      } else {
        out.push({ kind: "text", text: headingText });
      }
      continue;
    }
    const isKnown = known.has(normKey(trimmed));
    const isPlain =
      opts.detectPlainHeaders && isLikelySectionHeader(trimmed);
    if (opts.showSectionHeaders && (isKnown || isPlain)) {
      out.push({ kind: "heading", level: 2, text: trimmed });
      continue;
    }
    out.push({ kind: "text", text: line });
  }
  return out;
}
