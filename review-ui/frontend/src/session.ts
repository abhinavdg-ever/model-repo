/** The signed-in user and their session token (POST /api/auth/login), per browser tab.
 * sessionStorage: a tab opened with window.open gets a copy, so it stays signed in. */
export type SessionUser = { id: number | null; username: string; name: string; role: string };
export type Session = { token: string; user: SessionUser };

const KEY = "advantmed_imaging_session";

export function readSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Session;
    return parsed?.token && parsed.user?.username ? parsed : null;
  } catch {
    return null;
  }
}

export function writeSession(session: Session): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    // Private window or blocked storage: signed in until the page reloads.
  }
}

export function clearSession(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // Nothing stored to clear.
  }
}

/** "Simran Priya" / "simran.priya" -> "SP". */
export function initialsOf(user: SessionUser): string {
  const parts = (user.name || user.username).split(/[\s._-]+/).filter(Boolean);
  return (parts.slice(0, 2).map((p) => p[0]).join("") || "U").toUpperCase();
}
