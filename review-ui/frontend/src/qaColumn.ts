import { createContext } from "react";
import { readSession } from "./session";

/** The username that sees the QA Ground Truth columns (2026-11-25, user): History's Yes / No and
 * the chart view's values beside Ground Truth. */
export const QA_COLUMN_USER = "testing-user";

export function isQaUser(): boolean {
  return (readSession()?.user.username ?? "").trim().toLowerCase() === QA_COLUMN_USER;
}

/** One page's QA ground truth (qa_ground_truth columns), or null: no QA column at all. */
export type QaPage = Record<string, string | null | undefined>;
export const QaTruthContext = createContext<QaPage | null>(null);

/** Page Details row id -> its qa_ground_truth column. Every other row (Orientation, Quality,
 * Encounter Type, Provider Name ...) is kept under its own id (``qaKey``), 2026-11-25 (user:
 * the QA column has every Ground Truth field). */
export const QA_FIELD: Record<string, string> = {
  name: "member_name",
  dob: "member_dob",
  member_id: "member_id",
  handwriting: "printed_or_handwritten",
  dos_from: "dos_from",
  dos_to: "dos_to",
  blank_junk: "is_blank_or_junk",
  duplicate: "is_duplicate",
  page_type: "page_type",
  page_subtype: "page_subtype",
  codeable: "codeable",
  provider_credentials: "provider_credentials",
};

/** Where a row's QA value is kept: its qa_ground_truth column, else the row id itself. */
export function qaKey(rowId: string): string {
  return QA_FIELD[rowId] ?? rowId;
}

/** Accuracy scored on QA ground truth (2026-11-25, user): the testing user's choice, this
 * browser only. */
const TRUTH_KEY = "advantmed_accuracy_truth";
export type AccuracyTruth = "client" | "qa";

export function accuracyTruth(): AccuracyTruth {
  if (!isQaUser()) return "client";
  try {
    return localStorage.getItem(TRUTH_KEY) === "qa" ? "qa" : "client";
  } catch {
    return "client";
  }
}

export function setAccuracyTruth(next: AccuracyTruth): void {
  try {
    localStorage.setItem(TRUTH_KEY, next);
  } catch {
    // Blocked storage: the choice lasts until the page reloads.
  }
}

