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

/** A name without the stray marks OCR leaves at either end ("→ Sameera T Raza", "- John
 *  Smith:", "(Mary Jones)"): letters and digits are kept, and a closing period after an
 *  initial or a credential ("Bridgett A.", "Jr.") (2026-10-28, user). */
export function trimName(value: string): string {
  return value
    .replace(/^[^\p{L}\p{N}]+/u, "")
    .replace(/[^\p{L}\p{N}.]+$/u, "")
    .replace(/(^|[^.])\.+$/u, (_match, before: string, offset: number, whole: string) => {
      // Keep one period after a short word (an initial, "Jr", "MD"); drop it after a long one.
      const last = whole.slice(0, offset + before.length).split(/\s+/).pop() ?? "";
      return last.length <= 3 ? `${before}.` : before;
    });
}

/** A person's name in proper case: "WATSON, JOYCE" -> "Watson, Joyce", "o'neil-SMITH" ->
 *  "O'Neil-Smith". Separators (",", "|") and mixed-case spellings are kept; stray marks at
 *  either end are dropped (trimName). */
export function properName(value: string): string {
  return trimName(value).replace(/[^\s,|]+/g, (word) => word.split("-").map(properPart).join("-"));
}

/** A member's name as shown, in proper case and without the comma (2026-10-27, user):
 *  "Thompson, Patricia" -> "Patricia Thompson" (Last, First -> First Last). A name with no
 *  comma, or several, keeps its order with the commas dropped. */
export function memberDisplayName(value: string): string {
  const proper = properName(value.trim());
  const parts = proper.split(",").map((part) => part.trim()).filter(Boolean);
  if (parts.length === 2) return `${parts[1]} ${parts[0]}`;
  return parts.join(" ");
}
