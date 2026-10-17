/** Generational suffixes that stay in capitals. */
const ROMAN = new Set(["ii", "iii", "iv", "vi", "vii", "viii"]);

function properPart(part: string): string {
  const letters = part.replace(/[^A-Za-z]/g, "");
  if (!letters) return part;
  if (ROMAN.has(letters.toLowerCase())) return part.toUpperCase();
  // Mixed case is someone's own spelling (McDonald, DeLeon): keep it.
  if (letters !== letters.toUpperCase() && letters !== letters.toLowerCase()) return part;
  return part
    .toLowerCase()
    .replace(/(^|['’.])([a-z])/g, (_, before: string, first: string) => before + first.toUpperCase());
}

/** A person's name in proper case: "WATSON, JOYCE" -> "Watson, Joyce", "o'neil-SMITH" ->
 *  "O'Neil-Smith". Separators (",", "|") and mixed-case spellings are kept. */
export function properName(value: string): string {
  return value.replace(/[^\s,|]+/g, (word) => word.split("-").map(properPart).join("-"));
}
