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

/** Page Details row id -> the qa_ground_truth column shown in its QA Ground Truth cell. */
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
