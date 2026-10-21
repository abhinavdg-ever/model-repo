import { useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, CircleMinus, Copy, Plus, Save, Trash2 } from "lucide-react";
import {
  getEncounterReview,
  saveEncounterReview,
  type EncounterReview,
  type EncounterReviewRow,
} from "./api";
import { PAGE_TYPE_CHOICES } from "./reviewChoices";

export const ROWS_PER_TABLE_PAGE = 10;

/** encounter_canon.json's settings, then the label for every other page type. */
export const ENCOUNTER_TYPE_CHOICES = [
  "Outpatient (F2F)",
  "Outpatient (Tele)",
  "Inpatient",
  "Home",
  "Not Applicable",
];

/** The slice of ``items`` on table page ``page`` (0-based), and how many pages there are. */
export function pageSlice<T>(items: T[], page: number, size = ROWS_PER_TABLE_PAGE) {
  const pages = Math.max(1, Math.ceil(items.length / size));
  const current = Math.min(Math.max(0, page), pages - 1);
  return { rows: items.slice(current * size, current * size + size), page: current, pages };
}

export function TablePager({
  page,
  pages,
  total,
  onPage,
  label,
}: {
  page: number;
  pages: number;
  total: number;
  onPage: (page: number) => void;
  label: string;
}) {
  if (pages <= 1) return null;
  const first = page * ROWS_PER_TABLE_PAGE + 1;
  const last = Math.min(total, first + ROWS_PER_TABLE_PAGE - 1);
  return (
    <div className="review-table-pager pager-nav" aria-label={`${label} pages`}>
      <button type="button" onClick={() => onPage(page - 1)} disabled={page === 0} aria-label="Previous">
        <ChevronLeft size={15} aria-hidden="true" />
      </button>
      <span className="pager-label">
        {first}–{last} of {total}
      </span>
      <button type="button" onClick={() => onPage(page + 1)} disabled={page >= pages - 1} aria-label="Next">
        <ChevronRight size={15} aria-hidden="true" />
      </button>
    </div>
  );
}

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

function savedNote(review: EncounterReview | null): string {
  if (!review || review.rows.length === 0) return "";
  const when = review.savedAt ? new Date(review.savedAt).toLocaleString() : "";
  return ["Saved", review.savedBy ? `by ${review.savedBy}` : "", when ? `on ${when}` : ""]
    .filter(Boolean)
    .join(" ");
}

/** Reviewed encounters under the pipeline's: page ranges with page type, encounter type
 * and DOS, edited in place and saved to encounter_review. Not ground truth. With nothing
 * saved, the pipeline's encounters are copied in as the starting point. */
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
  const [page, setPage] = useState(0);

  const startFrom = (saved: EncounterReview | null) => {
    const copied = !saved || saved.rows.length === 0;
    setDrafts((copied ? seed : saved.rows).map(toDraft));
    setDirty(false);
    setPage(0);
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getEncounterReview(folderId)
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
  const slice = pageSlice(drafts, page);

  const edit = (key: number, field: keyof Omit<Draft, "key">, value: string) => {
    setDrafts((rows) => rows.map((row) => (row.key === key ? { ...row, [field]: value } : row)));
    setDirty(true);
  };
  const insertAfter = (index: number, row: Draft) => {
    setDrafts((rows) => [...rows.slice(0, index + 1), row, ...rows.slice(index + 1)]);
    setPage(Math.floor((index + 1) / ROWS_PER_TABLE_PAGE));
    setDirty(true);
  };
  const remove = (key: number) => {
    setDrafts((rows) => rows.filter((row) => row.key !== key));
    setDirty(true);
  };
  const addRow = () => {
    const last = drafts[drafts.length - 1];
    const start = last ? Math.min(Number(last.endPage) + 1 || 1, Math.max(pageCount, 1)) : 1;
    insertAfter(drafts.length - 1, {
      key: nextKey++,
      startPage: String(start),
      endPage: String(start),
      pageType: "",
      encounterType: "",
      dosFrom: "",
      dosTo: "",
    });
  };

  async function save() {
    if (problem) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await saveEncounterReview(folderId, drafts.map(toRow));
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
        <div className="section-coords-title">Reviewed Encounters</div>
        <span className="encounter-review-note">
          {loading
            ? "Loading…"
            : copied
              ? "Nothing saved yet: copied from the pipeline's encounters above."
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
              {slice.rows.length === 0 ? (
                <tr>
                  <td colSpan={8} className="encounter-review-empty">
                    No encounters. Add one below.
                  </td>
                </tr>
              ) : null}
              {slice.rows.map((row, offset) => {
                const index = slice.page * ROWS_PER_TABLE_PAGE + offset;
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
          <TablePager
            page={slice.page}
            pages={slice.pages}
            total={drafts.length}
            onPage={setPage}
            label="Reviewed encounters"
          />
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
