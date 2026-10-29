import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, CheckCircle2, ChevronLeft, ChevronRight, RefreshCw, RotateCcw, Search } from "lucide-react";
import PageMessage from "./PageMessage";
import { errorDetail, getReviewLog, type ReviewLogEntry, type ReviewLogResponse } from "./api";
import { formatIstPlain } from "./istTime";

type Props = {
  onBack: () => void;
  isAdmin: boolean;
  chartHref: (folderId: string, page?: number) => string;
};

/** page_ground_truth columns as Page Details labels them. The page type is saved in both
 * page_type and encounter_type; Blank / Junk across three columns. */
const FIELD_LABELS: Record<string, string> = {
  member_name: "Member Name",
  member_dob: "Member DOB",
  member_id: "Member ID",
  dos_from: "DOS From",
  dos_to: "DOS To",
  encounter_type: "Page Type",
  page_type: "Page Type",
  page_subtype: "Page Subtype",
  provider_specialty: "Specialty",
  codeable: "Codable / Non-Codable",
  blank_page: "Blank or Junk",
  junk_page: "Blank or Junk",
  is_invoice: "Blank or Junk",
  page_sequence: "Page Sequence",
  rotation: "Text Orientation",
  is_visible: "Visibility",
  rendering_provider: "Provider Name",
  provider_signature: "Provider Signature",
  provider_credentials: "Provider Credentials",
  encounter_label: "Encounter Type",
  tilt: "Tilt",
  quality: "Quality",
  mirrored: "Mirrored",
  printed_or_handwritten: "Type (Printed/HW)",
  is_duplicate: "Is Duplicate",
  handwritten_pct: "Handwritten %",
  member_decision: "Member Decision",
  member_decision_reason: "Decision Reason",
  encounter: "Encounter",
};

type Shown = { label: string; before: string | null; after: string | null };

/** The fields a save changed, one per label (Page Type is two columns, shown once). */
function shownFields(entry: ReviewLogEntry): Shown[] {
  const seen = new Map<string, Shown>();
  for (const f of entry.fields) {
    const label = FIELD_LABELS[f.field] ?? f.field;
    if (!seen.has(label)) seen.set(label, { label, before: f.before, after: f.after });
  }
  return [...seen.values()];
}

const PAGE_SIZE = 20;

/** First, last, and the pages around ``page``: 1 … 4 5 6 … 12. */
function pageWindow(page: number, totalPages: number): number[] {
  const out = new Set([1, totalPages, page - 1, page, page + 1]);
  return [...out].filter((n) => n >= 1 && n <= totalPages).sort((a, b) => a - b);
}

const SELF = "self";
const EVERYONE = "all";

/** Every user's own history of ground-truth saves (2026-10-28, user): chart, page, when
 * (IST), and the fields changed. Admins can read another user's, or everyone's. */
export default function ReviewLogView({ onBack, isAdmin, chartHref }: Props) {
  const [who, setWho] = useState<string>(SELF);
  const [data, setData] = useState<ReviewLogResponse | null>(null);
  const [users, setUsers] = useState<{ id: number; name: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [reload, setReload] = useState(0);
  const [page, setPage] = useState(1);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const opts =
      who === SELF ? {} : who === EVERYONE ? { allUsers: true } : { userId: Number(who) };
    getReviewLog({ ...opts, limit: 2000 })
      .then((res) => {
        if (cancelled) return;
        setData(res);
        if (res.users.length) setUsers(res.users);
      })
      .catch((err) => {
        if (!cancelled) setError(errorDetail(err, "Could not load the review log"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [who, reload]);

  // The user list for the picker comes with an everyone's-log request; ask once.
  useEffect(() => {
    if (!isAdmin || users.length) return;
    getReviewLog({ allUsers: true, limit: 1 })
      .then((res) => setUsers(res.users))
      .catch(() => undefined);
  }, [isAdmin, users.length]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const items = data?.items ?? [];
    if (!q) return items;
    return items.filter(
      (item) =>
        item.chartName.toLowerCase().includes(q) ||
        String(item.pageNumber ?? "").includes(q) ||
        (item.user ?? "").toLowerCase().includes(q) ||
        shownFields(item).some((f) => f.label.toLowerCase().includes(q)),
    );
  }, [data, query]);

  // A new search, user or refresh starts again at the first page.
  useEffect(() => setPage(1), [query, who, reload]);
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const current = Math.min(page, totalPages);
  const shownRows = rows.slice((current - 1) * PAGE_SIZE, current * PAGE_SIZE);

  const showUser = who === EVERYONE;
  const pages = new Set(rows.filter((r) => r.action === "edit").map((r) => `${r.chartName}|${r.pageNumber}`)).size;

  return (
    <div className="workspace accuracy-view review-log-view">
      <div className="workspace-header">
        <div className="workspace-header-start">
          <button type="button" className="back-btn" onClick={onBack}>
            <ArrowLeft size={15} aria-hidden="true" />
            Back
          </button>
          <div className="workspace-title">
            <h1>Review Log</h1>
          </div>
        </div>
      </div>

      {error ? (
        <PageMessage kind="error" onClose={() => setError(null)}>
          {error}
        </PageMessage>
      ) : null}

      <section className="accuracy-section" aria-label="Review log">
        <div className="review-log-toolbar">
          {isAdmin ? (
            <label className="landing-select-wrap">
              <span>User</span>
              <select value={who} onChange={(e) => setWho(e.target.value)}>
                <option value={SELF}>Me</option>
                <option value={EVERYONE}>All users</option>
                {users.map((u) => (
                  <option key={u.id} value={String(u.id)}>
                    {u.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <label className="landing-search review-log-search">
            <Search size={14} aria-hidden="true" />
            <input
              type="search"
              placeholder="Search chart, page, field…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="Search the review log"
            />
          </label>
          <span className="review-log-count">
            {rows.length} {rows.length === 1 ? "save" : "saves"} · {pages} {pages === 1 ? "page" : "pages"}
          </span>
          <button
            type="button"
            className="action-icon-btn"
            onClick={() => setReload((n) => n + 1)}
            disabled={loading}
            title="Refresh"
            aria-label="Refresh the review log"
          >
            <RefreshCw size={14} aria-hidden="true" />
          </button>
        </div>
        <p className="accuracy-note">
          Every ground-truth save, member decision, encounter save and Reviewed mark (set or cleared), newest first. Times are IST. A field shows its value before → after the save.
        </p>

        <div className="landing-history-scroll">
          <table className="landing-history-table review-log-table">
            <thead>
              <tr>
                <th>Time (IST)</th>
                {showUser ? <th>User</th> : null}
                <th>Chart ID</th>
                <th>Page</th>
                <th>Fields Edited</th>
              </tr>
            </thead>
            <tbody>
              {loading && !data ? (
                <tr>
                  <td colSpan={showUser ? 5 : 4} className="landing-history-empty">
                    Loading…
                  </td>
                </tr>
              ) : rows.length === 0 ? (
                <tr>
                  <td colSpan={showUser ? 5 : 4} className="landing-history-empty">
                    {query.trim() ? "No saves match the search." : "No ground-truth saves yet."}
                  </td>
                </tr>
              ) : (
                shownRows.map((item, index) => {
                  const fields = shownFields(item);
                  return (
                    <tr key={`${item.at}-${item.chartName}-${item.pageNumber}-${index}`}>
                      <td className="landing-col-updated">{formatIstPlain(item.at)}</td>
                      {showUser ? <td>{item.user ?? "—"}</td> : null}
                      <td className="landing-col-name" title={item.chartName}>
                        <a href={chartHref(item.chartName)}>{item.chartName}</a>
                      </td>
                      <td className="landing-col-num">
                        {item.pageNumber != null ? (
                          <a href={chartHref(item.chartName, item.pageNumber)}>{item.pageNumber}</a>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td>
                        {item.action === "reviewed" ? (
                          <span className="review-log-reviewed">
                            <CheckCircle2 size={13} aria-hidden="true" /> Chart marked Reviewed (ground truth frozen)
                          </span>
                        ) : item.action === "review_cleared" ? (
                          <span className="review-log-cleared">
                            <RotateCcw size={13} aria-hidden="true" /> Reviewed mark cleared (review enabled)
                          </span>
                        ) : fields.length === 0 ? (
                          <span className="imaging-gt-copied">No change</span>
                        ) : (
                          <ul className="review-log-fields">
                            {fields.map((f) => (
                              <li key={f.label}>
                                <strong>{f.label}</strong>{" "}
                                <span className="review-log-before">{f.before ?? "—"}</span>
                                {" → "}
                                <span className="review-log-after">{f.after ?? "—"}</span>
                              </li>
                            ))}
                          </ul>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
        {rows.length > PAGE_SIZE ? (
          <div className="landing-pagination" aria-label="Review log pagination">
            <span className="landing-pagination-meta">
              {(current - 1) * PAGE_SIZE + 1}–{Math.min(current * PAGE_SIZE, rows.length)} of {rows.length}
            </span>
            <div className="landing-pagination-controls">
              <button
                type="button"
                className="landing-page-btn"
                disabled={current <= 1}
                onClick={() => setPage(current - 1)}
                aria-label="Previous page"
              >
                <ChevronLeft size={14} />
              </button>
              {pageWindow(current, totalPages).map((n, idx, all) => (
                <span key={n} className="landing-page-num-wrap">
                  {idx > 0 && n - all[idx - 1] > 1 ? <span className="landing-page-ellipsis">…</span> : null}
                  <button
                    type="button"
                    className={`landing-page-num${n === current ? " active" : ""}`}
                    onClick={() => setPage(n)}
                    aria-label={`Page ${n}`}
                    aria-current={n === current ? "page" : undefined}
                  >
                    {n}
                  </button>
                </span>
              ))}
              <button
                type="button"
                className="landing-page-btn"
                disabled={current >= totalPages}
                onClick={() => setPage(current + 1)}
                aria-label="Next page"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}
