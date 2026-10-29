/** Does the pipeline's member name or DOB belong to the manifest member? Member accuracy
 * counts a page's name as found only when it does (2026-10-27, user): a name read off the
 * page that is someone else is not the member. */

import pageTaxonomy from "./pageTaxonomy.json";

/** Each first name -> the names that are the same person (Bill = William), from
 * member_name_variants_canon.json via pageTaxonomy.json (2026-10-27, user). */
const VARIANTS: Map<string, Set<string>> = (() => {
  const out = new Map<string, Set<string>>();
  for (const group of (pageTaxonomy as { name_variants?: string[][] }).name_variants ?? []) {
    for (const name of group) {
      const same = out.get(name) ?? new Set<string>();
      group.forEach((other) => same.add(other));
      out.set(name, same);
    }
  }
  return out;
})();

const TITLES = new Set(["mr", "mrs", "ms", "miss", "dr", "jr", "sr", "ii", "iii", "iv", "md", "do", "np", "pa"]);

function words(value: string | null | undefined): string[] {
  return (value ?? "")
    .toLowerCase()
    .split(/[^a-z]+/)
    .filter((word) => word && !TITLES.has(word));
}

/** Edits (insert, delete, change) between two short words. */
function distance(a: string, b: string): number {
  const row = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i += 1) {
    let previous = row[0];
    row[0] = i;
    for (let j = 1; j <= b.length; j += 1) {
      const kept = row[j];
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, previous + (a[i - 1] === b[j - 1] ? 0 : 1));
      previous = kept;
    }
  }
  return row[b.length];
}

/** The same name, allowing one letter of OCR error in a name of five letters or more. */
function sameName(a: string, b: string): boolean {
  if (a === b) return true;
  if (VARIANTS.get(a)?.has(b)) return true;
  return a.length >= 5 && b.length >= 5 && distance(a, b) <= 1;
}

/** Words side by side that are one name of ``against`` written as one: "Miller-Cook"
 * (miller, cook) beside a manifest "MillerCook", or "Le Blanc" beside "LeBlanc". */
function mergeCompounds(tokens: string[], against: string[]): string[] {
  const out = [...tokens];
  for (let i = 0; i < out.length; i += 1) {
    for (const span of [3, 2]) {
      if (i + span > out.length) continue;
      const joined = out.slice(i, i + span).join("");
      if (!against.includes(out[i]) && against.some((name) => sameName(joined, name))) {
        out.splice(i, span, joined);
        break;
      }
    }
  }
  return out;
}

/** The manifest member in any order, as the member rules read it: every name (two of three
 * when the manifest has three), one of two may be an initial; a different full name beside
 * a partial match is someone else. */
export function nameMatchesManifest(found: string | null | undefined, manifest: string | null | undefined): boolean {
  // A hyphenated or spaced name part matches it written as one, either way round.
  const seen = mergeCompounds(words(found), words(manifest));
  const expected = [...new Set(mergeCompounds(words(manifest), seen))];
  if (expected.length === 0 || seen.length === 0) return false;
  const full = expected.filter((name) => seen.some((word) => word.length > 1 && sameName(word, name)));
  const initials = expected.filter(
    (name) => !full.includes(name) && seen.some((word) => word.length === 1 && word === name[0]),
  );
  const extra = seen.filter((word) => word.length > 1 && !expected.some((name) => sameName(word, name)));
  if (full.length === expected.length) return true;
  if (extra.length > 0) return false;
  if (expected.length >= 3) return full.length >= 2;
  if (expected.length === 2) return full.length === 1 && initials.length === 1;
  return false;
}

function isoDay(value: string | null | undefined): string | null {
  const text = (value ?? "").trim();
  let m = /^(\d{4})-(\d{1,2})-(\d{1,2})/.exec(text);
  if (m) return `${m[1]}-${m[2].padStart(2, "0")}-${m[3].padStart(2, "0")}`;
  m = /^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$/.exec(text);
  if (m) return `${m[3]}-${m[1].padStart(2, "0")}-${m[2].padStart(2, "0")}`;
  return null;
}

/** The manifest DOB, written either way. */
export function dobMatchesManifest(found: string | null | undefined, manifest: string | null | undefined): boolean {
  const a = isoDay(found);
  return a != null && a === isoDay(manifest);
}
