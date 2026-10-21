import { useEffect, useMemo, useState } from "react";
import { CircleMinus, Copy, ListPlus, Plus, Save, Trash2 } from "lucide-react";
import {
  getEncounterReview,
  saveEncounterReview,
  type EncounterReview,
  type EncounterReviewRow,
} from "./api";
import { PAGE_TYPE_CHOICES } from "./reviewChoices";

/** encounter_canon.json's settings, then the label for every other page type. */
export const ENCOUNTER_TYPE_CHOICES = [
  "Outpatient (F2F)",
  "Outpatient (Tele)",
  "Inpatient",
  "Home",
  "Not Applicable",
];

/** A pipeline encounter offered as the starting point when nothing was reviewed yet. */
export type EncounterSeed = Omit<EncounterReviewRow, "seq">;

type Draft = {
  key: number;
  startPage: string;
  endPage: string;
  pageType: string;
  encounterType: string;
  dosFrom: string;
  dosTo: string;
};

let nextKey = 1;

function toDraft(row: EncounterSeed): Draft {
  return {
    key: nextKey++,
    startPage: String(row.startPage),
    endPage: String(row.endPage),
    pageType: row.pageType ?? "",
    encounterType: row.encounterType ?? "",
    dosFrom: isoDate(row.dosFrom),
    dosTo: isoDate(row.dosTo),
  };
}

/** A date input takes YYYY-MM-DD only; anything else starts empty. */
function isoDate(value: string | null | undefined): string {
  const text = (value ?? "").trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(text) ? text : "";
}

function toRow(draft: Draft, index: number): EncounterReviewRow {
  const text = (value: string) => value.trim() || null;
  return {
    seq: index + 1,
    startPage: Number(draft.startPage),
    endPage: Number(draft.endPage),
    pageType: text(draft.pageType),
    encounterType: text(draft.encounterType),
    dosFrom: text(draft.dosFrom),
    dosTo: text(draft.dosTo),
  };
}

/** What is wrong with the drafts, or null: whole page numbers within the chart, start
 * before end, no two ranges sharing a page. */
function draftProblem(drafts: Draft[], pageCount: number): string | null {
  const ranges: [number, number][] = [];
  for (const [index, draft] of drafts.entries()) {
    const start = Number(draft.startPage);
    const end = Number(draft.endPage);
    const name = `Encounter ${index + 1}`;
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < 1) {
      return `${name}: start and end page must be page numbers.`;
    }
    if (end < start) return `${name}: end page is before the start page.`;
    if (pageCount > 0 && end > pageCount) return `${name}: the chart has ${pageCount} pages.`;
    ranges.push([start, end]);
  }
  ranges.sort((a, b) => a[0] - b[0]);
  for (let i = 1; i < ranges.length; i += 1) {
    if (ranges[i][0] <= ranges[i - 1][1]) {
      return `Pages ${ranges[i - 1][0]}–${ranges[i - 1][1]} and ${ranges[i][0]}–${ranges[i][1]} overlap.`;
    }
  }
  return null;
}

// One load per chart: reopening the Encounter tab reuses it, and the chart viewer
// starts it as soon as the imaging results are in.
const reviewCache = new Map<string, Promise<EncounterReview>>();

/** The chart's saved encounters, fetched once per chart (a failed load is retried). */
export function loadEncounterReview(folderId: string): Promise<EncounterReview> {
  let pending = reviewCache.get(folderId);
  if (!pending) {
    pending = getEncounterReview(folderId);
    pending.catch(() => reviewCache.delete(folderId));
    reviewCache.set(folderId, pending);
  }
  return pending;
}

function emptyDraft(startPage: number): Draft {
  return {
    key: nextKey++,
    startPage: String(startPage),
    endPage: String(startPage),
    pageType: "",
    encounterType: "",
    dosFrom: "",
    dosTo: "",
  };
}

function savedNote(review: EncounterReview | null): string {
  if (!review || review.rows.length === 0) return "";
  const when = review.savedAt ? new Date(review.savedAt).toLocaleString() : "";
  return ["Saved", review.savedBy ? `by ${review.savedBy}` : "", when ? `on ${when}` : ""]
    .filter(Boolean)
    .join(" ");
}

/** Ground-truth encounters (the Encounter tab's Ground Truth subtab): page ranges with
 * page type, encounter type and DOS, edited in place and saved to encounter_review. With
 * nothing saved, the Algo encounters are copied in as the starting point. Every row is
 * shown; a row can be inserted below any other, and the rows after it move down. */
export function EncounterReviewEditor({
  folderId,
  seed,
  pageCount,
}: {
  folderId: string;
  seed: EncounterSeed[];
  pageCount: number;
}) {
  const [review, setReview] = useState<EncounterReview | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const startFrom = (saved: EncounterReview | null) => {
    const copied = !saved || saved.rows.length === 0;
    setDrafts((copied ? seed : saved.rows).map(toDraft));
    setDirty(false);
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    loadEncounterReview(folderId)
      .then((saved) => {
        if (cancelled) return;
        setReview(saved);
        startFrom(saved);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setReview(null);
        startFrom(null);
        setError(err instanceof Error ? err.message : "Could not load the reviewed encounters");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // seed changes with every imaging reload; only a new chart reloads the review.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [folderId]);

  const problem = useMemo(() => draftProblem(drafts, pageCount), [drafts, pageCount]);
  const copied = !review || review.rows.length === 0;

  const edit = (key: number, field: keyof Omit<Draft, "key">, value: string) => {
    setDrafts((rows) => rows.map((row) => (row.key === key ? { ...row, [field]: value } : row)));
    setDirty(true);
  };
  const insertAfter = (index: number, row: Draft) => {
    setDrafts((rows) => [...rows.slice(0, index + 1), row, ...rows.slice(index + 1)]);
    setDirty(true);
  };
  /** A blank row below row ``index`` (-1: at the top), starting on the next page. */
  const insertBlank = (index: number) => {
    const before = drafts[index];
    const next = before ? Number(before.endPage) + 1 || 1 : 1;
    insertAfter(index, emptyDraft(pageCount > 0 ? Math.min(next, pageCount) : next));
  };
  const remove = (key: number) => {
    setDrafts((rows) => rows.filter((row) => row.key !== key));
    setDirty(true);
  };
  const addRow = () => insertBlank(drafts.length - 1);

  async function save() {
    if (problem) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await saveEncounterReview(folderId, drafts.map(toRow));
      reviewCache.set(folderId, Promise.resolve(saved));
      setReview(saved);
      startFrom(saved);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the encounters");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="section-coords-panel encounter-review">
      <div className="encounter-review-head">
        <span className="encounter-review-note">
          {loading
            ? "Loading…"
            : copied
              ? "Nothing saved yet: copied from the Algo Encounters."
              : savedNote(review)}
        </span>
      </div>
      {!loading ? (
        <>
          <datalist id="encounter-review-page-types">
            {PAGE_TYPE_CHOICES.map((choice) => (
              <option key={choice} value={choice} />
            ))}
          </datalist>
          <datalist id="encounter-review-encounter-types">
            {ENCOUNTER_TYPE_CHOICES.map((choice) => (
              <option key={choice} value={choice} />
            ))}
          </datalist>
          <table className="imaging-summary-table imaging-documents-table encounter-review-table">
            <thead>
              <tr>
                <th scope="col">Encounter #</th>
                <th scope="col">Start Page</th>
                <th scope="col">End Page</th>
                <th scope="col">Page Type</th>
                <th scope="col">Encounter Type</th>
                <th scope="col">DOS From</th>
                <th scope="col">DOS To</th>
                <th scope="col" aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {drafts.length === 0 ? (
                <tr>
                  <td colSpan={8} className="encounter-review-empty">
                    No encounters. Add one below.
                  </td>
                </tr>
              ) : null}
              {drafts.map((row, index) => {
                return (
                  <tr key={row.key}>
                    <td>{index + 1}</td>
                    {(["startPage", "endPage"] as const).map((field) => (
                      <td key={field}>
                        <input
                          className="encounter-review-input encounter-review-page"
                          type="number"
                          min={1}
                          max={pageCount || undefined}
                          value={row[field]}
                          aria-label={`Encounter ${index + 1} ${field === "startPage" ? "start" : "end"} page`}
                          onChange={(e) => edit(row.key, field, e.target.value)}
                        />
                      </td>
                    ))}
                    <td>
                      <input
                        className="encounter-review-input"
                        list="encounter-review-page-types"
                        value={row.pageType}
                        aria-label={`Encounter ${index + 1} page type`}
                        onChange={(e) => edit(row.key, "pageType", e.target.value)}
                      />
                    </td>
                    <td>
                      <input
                        className="encounter-review-input"
                        list="encounter-review-encounter-types"
                        value={row.encounterType}
                        aria-label={`Encounter ${index + 1} encounter type`}
                        onChange={(e) => edit(row.key, "encounterType", e.target.value)}
                      />
                    </td>
                    {(["dosFrom", "dosTo"] as const).map((field) => (
                      <td key={field}>
                        <input
                          className="encounter-review-input"
                          type="date"
                          value={row[field]}
                          aria-label={`Encounter ${index + 1} DOS ${field === "dosFrom" ? "from" : "to"}`}
                          onChange={(e) => edit(row.key, field, e.target.value)}
                        />
                      </td>
                    ))}
                    <td className="encounter-review-actions">
                      <button
                        type="button"
                        title="Insert a blank row below this one (the rows after it move down)"
                        aria-label={`Insert a row after encounter ${index + 1}`}
                        onClick={() => insertBlank(index)}
                      >
                        <ListPlus size={13} aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        title="Copy this row below it"
                        aria-label={`Copy encounter ${index + 1}`}
                        onClick={() => insertAfter(index, { ...row, key: nextKey++ })}
                      >
                        <Copy size={13} aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        title="Delete this row"
                        aria-label={`Delete encounter ${index + 1}`}
                        onClick={() => remove(row.key)}
                      >
                        <Trash2 size={13} aria-hidden="true" />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {problem && dirty ? <div className="encounter-review-problem">{problem}</div> : null}
          {error ? <div className="error-banner">{error}</div> : null}
          <div className="encounter-review-bar">
            <button type="button" className="imaging-gt-discard" onClick={addRow} disabled={saving}>
              <Plus size={15} aria-hidden="true" />
              Add row
            </button>
            <span className="encounter-review-bar-gap" />
            {dirty ? (
              <button
                type="button"
                className="imaging-gt-discard"
                onClick={() => startFrom(review)}
                disabled={saving}
              >
                <CircleMinus size={15} aria-hidden="true" />
                Discard
              </button>
            ) : null}
            <button
              type="button"
              className="imaging-gt-save"
              onClick={() => void save()}
              disabled={saving || Boolean(problem) || (!dirty && !copied)}
              title={problem ?? undefined}
            >
              <Save size={15} aria-hidden="true" />
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}
