import { createContext } from "react";

/** The client's copy of the UI (CLIENT_VIEW=true in /api/config): Page Details shows only
 * the Final values, headed Algorithm (2026-10-27, user). */
export const ClientViewContext = createContext(false);

const CHOICE_KEY = "advantmed_accuracy_final_only";
let byDefault = false;
let admin = false;

/** Accuracy on the Final values alone, or the better of Final and Extracted (2026-10-27,
 * user). The client's copy defaults to Final only, ours to the better of the two; an admin
 * can switch it in the profile menu (remembered in this browser). Set from /api/config
 * before any chart is shown. */
export function setScoreFinalOnly(on: boolean): void {
  byDefault = on;
}

export function setScoringAdmin(isAdmin: boolean): void {
  admin = isAdmin;
}

function choice(): boolean | null {
  try {
    const stored = localStorage.getItem(CHOICE_KEY);
    return stored === "yes" ? true : stored === "no" ? false : null;
  } catch {
    return null;
  }
}

export function setFinalOnlyChoice(on: boolean): void {
  try {
    localStorage.setItem(CHOICE_KEY, on === byDefault ? "" : on ? "yes" : "no");
  } catch {
    /* storage blocked: the default stays */
  }
}

export function scoreFinalOnly(): boolean {
  const chosen = admin ? choice() : null;
  return chosen ?? byDefault;
}
