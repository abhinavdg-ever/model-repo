/** Admin switch between the Postgres and Cosmos stores (2026-11-14, user: "a cosmos and
 * postgres mode for admin to test"). The choice is kept in this browser and sent on every API
 * call as the X-DB-Backend header, and as the db_backend cookie for page images (an <img>
 * cannot send a header). The backend takes it only from an admin's request (app/main.py
 * DbBackendSwitch); everyone else gets the deployment's DB_BACKEND. */

export type DbStore = "postgres" | "cosmos";

export type DbBackendInfo = {
  /** DB_BACKEND of the deployment: postgres / cosmos / both. */
  configured: string;
  /** The store the request used. */
  active: string;
  /** The stores this deployment can switch between. */
  available: DbStore[];
  switchable: boolean;
};

const KEY = "review-ui.db-backend";
const COOKIE = "db_backend";

export function readDbChoice(): DbStore | null {
  try {
    const value = localStorage.getItem(KEY);
    return value === "postgres" || value === "cosmos" ? value : null;
  } catch {
    return null;
  }
}

/** Remember the choice (null = the deployment's own store) and set or clear the cookie. */
export function writeDbChoice(choice: DbStore | null): void {
  try {
    if (choice) localStorage.setItem(KEY, choice);
    else localStorage.removeItem(KEY);
  } catch {
    /* private window: the header still goes for this page's life via the cookie */
  }
  document.cookie = choice
    ? `${COOKIE}=${choice}; path=/; SameSite=Lax`
    : `${COOKIE}=; path=/; SameSite=Lax; Max-Age=0`;
}

/** The header for an API call; empty when no choice is made. */
export function dbChoiceHeader(): Record<string, string> {
  const choice = readDbChoice();
  return choice ? { "X-DB-Backend": choice } : {};
}

/** "Postgres" / "Cosmos" for a store or DB_BACKEND value. */
export function storeLabel(value: string | null | undefined): string {
  if (value === "cosmos") return "Cosmos";
  if (value === "both") return "Postgres (+ Cosmos copy)";
  return "Postgres";
}

/** The store a DB_BACKEND value reads from: both reads Postgres. */
export function readsFrom(value: string | null | undefined): DbStore {
  return value === "cosmos" ? "cosmos" : "postgres";
}
