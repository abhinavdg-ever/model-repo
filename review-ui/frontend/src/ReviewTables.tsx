import { useEffect, useMemo, useState } from "react";
import { CircleMinus, Copy, ListPlus, Pencil, Plus, Save, Trash2 } from "lucide-react";
import {
  getEncounterReview,
  saveEncounterReview,
  type EncounterReview,
  type EncounterReviewRow,
} from "./api";
import { PAGE_L1_CHOICES } from "./groundTruth";
import { SearchInput } from "./SearchInput";

/** The main page type alone: "Progress Note (Wound Care Visit)" reads Progress Note
 * (2026-10-27, user). */
export function mainPageType(value: string | null | undefined): string {
  return (value ?? "").replace(/\s*\(.*\)\s*$/, "").trim();
}

/** encounter_canon.json's settings, then the label for every other page type. */
export const ENCOUNTER_TYPE_CHOICES = [
  "Outpatient (F2F)",
  "Outpatient (Tele)",
  "Inpatient",
  "Home",
  "Not Available",
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
    pageType: mainPageType(row.pageType),
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

/** What is wrong with each draft, by draft key: page numbers within the chart, start
 * before end, and no two ranges sharing a page (both rows of an overlap are marked). */
function rowProblems(drafts: Draft[], pageCount: number): Map<number, string> {
  const out = new Map<number, string>();
  const note = (key: number, message: string) => {
    if (!out.has(key)) out.set(key, message);
  };
  const ranges: { key: number; index: number; start: number; end: number }[] = [];
  for (const [index, draft] of drafts.entries()) {
    const start = Number(draft.startPage);
    const end = Number(draft.endPage);
    const name = `Encounter ${index + 1}`;
    if (!draft.startPage.trim() || !draft.endPage.trim() || !Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < 1) {
      note(draft.key, `${name}: start and end page must be page numbers.`);
      continue;
    }
    if (end < start) {
      note(draft.key, `${name}: end page ${end} is before the start page ${start}.`);
      continue;
    }
    if (pageCount > 0 && end > pageCount) {
      note(draft.key, `${name}: the chart has ${pageCount} pages.`);
      continue;
    }
    ranges.push({ key: draft.key, index, start, end });
  }
  for (const [i, a] of ranges.entries()) {
    for (const b of ranges.slice(i + 1)) {
      if (a.start <= b.end && b.start <= a.end) {
        const message =
          `Encounter ${a.index + 1} (pages ${a.start}–${a.end}) and ` +
          `Encounter ${b.index + 1} (pages ${b.start}–${b.end}) overlap.`;
        note(a.key, message);
        note(b.key, message);
      }
    }
  }
  return out;
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

function emptyDraft(): Draft {
  return {
    key: nextKey++,
    startPage: "",
    endPage: "",
    pageType: "",
    encounterType: "",
    dosFrom: "",
    dosTo: "",
  };
}

function isEmptyDraft(draft: Draft): boolean {
  return [draft.startPage, draft.endPage, draft.pageType, draft.encounterType, draft.dosFrom, draft.dosTo]
    .every((value) => !value.trim());
}

function savedNote(review: EncounterReview | null): string {
  if (!review || review.rows.length === 0) return "";
  const when = review.savedAt ? new Date(review.savedAt).toLocaleString() : "";
  return ["Saved", review.savedBy ? `by ${review.savedBy}` : "", when ? `on ${when}` : ""]
    .filter(Boolean)
    .join(" ");
}

/** Ground-truth encounters (the Encounter tab's Ground Truth subtab): page ranges with
 * page type, encounter type and DOS, saved to encounter_review. The saved rows are shown
 * read-only (empty when nothing is saved); Edit opens them as inputs, starting from the
 * Algorithm encounters when nothing is saved. A row can be inserted below any other, and
 * the rows after it move down. */
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
  const [editing, setEditing] = useState(false);
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

  const problems = useMemo(() => rowProblems(drafts, pageCount), [drafts, pageCount]);
  const problem = problems.size > 0 ? [...problems.values()][0] : null;
  const copied = !review || review.rows.length === 0;

  const edit = (key: number, field: keyof Omit<Draft, "key">, value: string) => {
    setDrafts((rows) => rows.map((row) => (row.key === key ? { ...row, [field]: value } : row)));
    setDirty(true);
  };
  const insertAfter = (index: number, row: Draft) => {
    setDrafts((rows) => [...rows.slice(0, index + 1), row, ...rows.slice(index + 1)]);
    setDirty(true);
  };
  /** An empty row below row ``index`` (-1: at the top); nothing is filled in. */
  const insertBlank = (index: number) => insertAfter(index, emptyDraft());
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
      setEditing(false);
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
            : !copied
              ? savedNote(review)
              : editing
                ? "Nothing saved yet: copied from the Algorithm encounters."
                : "Nothing saved yet."}
        </span>
        {!loading && !editing ? (
          <span className="encounter-review-bar">
            <button
              type="button"
              className="imaging-gt-discard"
              onClick={() => {
                startFrom(review);
                setError(null);
                setEditing(true);
              }}
            >
              <Pencil size={14} aria-hidden="true" />
              Edit Ground Truth
            </button>
          </span>
        ) : null}
        {!loading && editing ? (
          <span className="encounter-review-bar">
            <button
              type="button"
              className="imaging-gt-discard"
              onClick={() => {
                startFrom(review);
                setEditing(false);
              }}
              disabled={saving}
            >
              <CircleMinus size={15} aria-hidden="true" />
              Discard
            </button>
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
          </span>
        ) : null}
      </div>
      {!loading && !editing ? (
        <>
          <SavedEncounters rows={review?.rows ?? []} />
          {error ? <div className="error-banner">{error}</div> : null}
        </>
      ) : null}
      {!loading && editing ? (
        <>
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
                const rowProblem = isEmptyDraft(row) ? undefined : problems.get(row.key);
                return (
                  <tr
                    key={row.key}
                    className={rowProblem ? "encounter-review-row-error" : undefined}
                    title={rowProblem}
                  >
                    <td>{index + 1}</td>
                    {(["startPage", "endPage"] as const).map((field) => (
                      <td key={field}>
                        <input
                          className={`encounter-review-input encounter-review-page${rowProblem ? " is-invalid" : ""}`}
                          aria-invalid={rowProblem ? true : undefined}
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
                      {/* Our L1s, searched as in Page Details: two letters, any text kept. */}
                      <SearchInput
                        className="encounter-review-input"
                        value={row.pageType}
                        options={["Junk", ...PAGE_L1_CHOICES]}
                        ariaLabel={`Encounter ${index + 1} page type`}
                        warning="New Type"
                        onChange={(value) => edit(row.key, "pageType", value)}
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
          {problems.size > 0 ? (
            <ul className="encounter-review-problem encounter-review-problems">
              {[...new Set(problems.values())].map((message) => (
                <li key={message}>{message}</li>
              ))}
            </ul>
          ) : null}
          {error ? <div className="error-banner">{error}</div> : null}
          <div className="encounter-review-bar">
            <button type="button" className="imaging-gt-discard" onClick={addRow} disabled={saving}>
              <Plus size={15} aria-hidden="true" />
              Add row
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}

/** The saved ground-truth encounters, read-only. */
function SavedEncounters({ rows }: { rows: EncounterReviewRow[] }) {
  return (
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
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <tr>
            <td colSpan={7} className="encounter-review-empty">
              No ground truth saved for this chart.
            </td>
          </tr>
        ) : null}
        {rows.map((row, index) => (
          <tr key={`${row.seq}-${row.startPage}`}>
            <td>{index + 1}</td>
            <td>{row.startPage}</td>
            <td>{row.endPage}</td>
            <td>{mainPageType(row.pageType)}</td>
            <td>{row.encounterType ?? ""}</td>
            <td>{row.dosFrom ?? ""}</td>
            <td>{row.dosTo ?? ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
