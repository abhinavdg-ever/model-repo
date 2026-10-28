/** Every time the UI shows is IST (2026-10-28, user). The server runs on GMT: a timestamp
 * it sends without a zone ("2026-10-28T09:15:00") is GMT, not the browser's local time. */

const HAS_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i;

export function serverTime(iso: string | null | undefined): Date | null {
  const text = (iso ?? "").trim();
  if (!text) return null;
  // A date and time with no zone is the server's GMT; a bare date stays as it is.
  const withZone = text.includes("T") && !HAS_ZONE.test(text) ? `${text}Z` : text;
  const d = new Date(withZone);
  return Number.isNaN(d.getTime()) ? null : d;
}

const IST = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: true,
});

const IST_SHORT = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: true,
});

/** "28 Oct 2026, 02:45 pm IST"; ``empty`` when there is no time. */
export function formatIst(iso: string | null | undefined, empty = "—"): string {
  const d = serverTime(iso);
  return d ? `${IST.format(d)} IST` : empty;
}

/** "28 Oct, 02:45 pm" (no year, no suffix) for tight columns. */
export function formatIstShort(iso: string | null | undefined, empty = ""): string {
  const d = serverTime(iso);
  return d ? IST_SHORT.format(d) : empty;
}

/** The IST time without the suffix, for columns headed "(IST)". */
export function formatIstPlain(iso: string | null | undefined, empty = "—"): string {
  const d = serverTime(iso);
  return d ? IST.format(d) : empty;
}
