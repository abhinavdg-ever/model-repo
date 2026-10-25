import { Fragment, useState, type FormEvent } from "react";
import { ArrowLeft, ChevronDown, ChevronRight, History, Search, X } from "lucide-react";
import PageMessage from "./PageMessage";
import { errorDetail, searchOcr, type OcrSearchChart, type OcrSearchResult, type OcrSearchSnippet } from "./api";

type Props = {
  onBack: () => void;
  /** Open a chart in the viewer (imaging output). */
  /** The chart's viewer, at ``page`` (its page_number) when given. */
  chartHref: (chart: string, page?: number) => string;
};

/** One search: what was asked, and its answer while this tab is open. */
type Searched = {
  id: string;
  q: string;
  wholeWord: boolean;
  run: string;
  batch: string;
  at: string;
  /** Totals, kept with the history so it reads without the answer. */
  charts: number;
  hits: number;
  headerHits: number;
};

const HISTORY_KEY = "review-ui.search-history";
const HISTORY_SIZE = 20;

/** Answers stay in memory only (they hold page text), for as long as the tab is open,
 * so going Back and returning to Search keeps them. */
const answers = new Map<string, OcrSearchResult>();

/** The history, newest first: queries and totals only, never page text. */
function readHistory(): Searched[] {
  try {
    const raw = window.localStorage.getItem(HISTORY_KEY);
    const parsed = raw ? (JSON.parse(raw) as Searched[]) : [];
    return Array.isArray(parsed) ? parsed.slice(0, HISTORY_SIZE) : [];
  } catch {
    return [];
  }
}

function writeHistory(history: Searched[]): void {
  try {
    window.localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, HISTORY_SIZE)));
  } catch {
    // Private window or blocked storage: the history lasts until the tab closes.
  }
}

function sameSearch(a: Pick<Searched, "q" | "wholeWord" | "run" | "batch">, b: typeof a): boolean {
  return a.q.toLowerCase() === b.q.toLowerCase() && a.wholeWord === b.wholeWord && a.run === b.run && a.batch === b.batch;
}

function plural(n: number, word: string): string {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

function fmtAt(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ""
    : new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: true }).format(d);
}

function Snippet({ s }: { s: OcrSearchSnippet }) {
  return (
    <>
      {s.before}
      <mark>{s.match}</mark>
      {s.after}
    </>
  );
}

/** Admins only: find a word or phrase in every page's preliminary OCR (Tesseract) text
 * and in the pages' section headers, one table row per chart. */
export default function SearchView({ onBack, chartHref }: Props) {
  const [history, setHistory] = useState<Searched[]>(readHistory);
  const [selected, setSelected] = useState<string | null>(() => history.find((h) => answers.has(h.id))?.id ?? null);
  const [query, setQuery] = useState(() => history.find((h) => h.id === selected)?.q ?? "");
  const [wholeWord, setWholeWord] = useState(true);
  const [run, setRun] = useState("");
  const [batch, setBatch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());

  const current = selected ? history.find((h) => h.id === selected) ?? null : null;
  const result = selected ? answers.get(selected) ?? null : null;

  async function runSearch(asked: Pick<Searched, "q" | "wholeWord" | "run" | "batch">) {
    setBusy(true);
    setError(null);
    try {
      const found = await searchOcr({ q: asked.q, wholeWord: asked.wholeWord, run: asked.run, batch: asked.batch });
      const entry: Searched = {
        ...asked,
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        at: new Date().toISOString(),
        charts: found.total_charts,
        hits: found.total_hits,
        headerHits: found.total_header_hits,
      };
      answers.set(entry.id, found);
      // The same search again replaces its older entry; every other one stays.
      const next = [entry, ...history.filter((h) => !sameSearch(h, asked))].slice(0, HISTORY_SIZE);
      for (const old of history) if (!next.includes(old)) answers.delete(old.id);
      setHistory(next);
      writeHistory(next);
      setSelected(entry.id);
      setOpen(new Set());
    } catch (err) {
      setError(errorDetail(err, "Could not search the OCR text"));
    } finally {
      setBusy(false);
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (query.trim().length < 2 || busy) return;
    void runSearch({ q: query.trim(), wholeWord, run: run.trim(), batch: batch.trim() });
  }

  /** Show an earlier search: its kept answer, or (after a reload) ask again. */
  function recall(entry: Searched) {
    setQuery(entry.q);
    setWholeWord(entry.wholeWord);
    setRun(entry.run);
    setBatch(entry.batch);
    if (answers.has(entry.id)) {
      setSelected(entry.id);
      setOpen(new Set());
    } else {
      void runSearch(entry);
    }
  }

  function forget(entry: Searched) {
    const next = history.filter((h) => h.id !== entry.id);
    answers.delete(entry.id);
    setHistory(next);
    writeHistory(next);
    if (selected === entry.id) setSelected(null);
  }

  function toggle(chart: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(chart)) next.delete(chart);
      else next.add(chart);
      return next;
    });
  }

  // A chart or a page opens in a new tab, so the search and its open rows stay put. The
  // chart opens at its first page with a hit (a word or a section header).
  function chartLink(chart: OcrSearchChart) {
    const hits = [...chart.pages, ...chart.headers]
      .map((p) => p.page_number)
      .filter((n): n is number => typeof n === "number");
    const first = hits.length ? Math.min(...hits) : undefined;
    return (
      <a
        href={chartHref(chart.chart, first)}
        target="_blank"
        rel="noopener noreferrer"
        title={first ? `Open at page ${first}, the first hit, in a new tab` : "Open in a new tab"}
        onClick={(e) => e.stopPropagation()}
      >
        {chart.chart}
      </a>
    );
  }

  function pageLink(chart: string, pageNumber: number | null | undefined, pageName: string) {
    if (pageNumber == null) return <>{pageName}</>;
    return (
      <a href={chartHref(chart, pageNumber)} target="_blank" rel="noopener noreferrer" title={`Open page ${pageNumber} in a new tab`}>
        {pageNumber}
      </a>
    );
  }

  const allOpen = Boolean(result?.charts.length) && result!.charts.every((c) => open.has(c.chart));

  return (
    <div className="workspace accuracy-view playground-view">
      <div className="workspace-header">
        <div className="workspace-header-start">
          <button type="button" className="back-btn" onClick={onBack}>
            <ArrowLeft size={15} aria-hidden="true" />
            Back
          </button>
          <div className="workspace-title">
            <h1>Search</h1>
          </div>
        </div>
      </div>

      <section className="accuracy-section users-form search-form" aria-label="Search the OCR">
        <p className="accuracy-note">
          Finds a word or phrase in every page's preliminary OCR (Tesseract) text and in its section headers, one row
          per chart. Case does not matter; a phrase may run across line breaks.
        </p>
        <form className="search-bar" onSubmit={submit}>
          <input
            value={query}
            autoFocus
            placeholder="e.g. hypertension, date of service"
            aria-label="Word or phrase"
            onChange={(e) => setQuery(e.target.value)}
          />
          <input value={run} placeholder="Run (all)" aria-label="Run" className="search-narrow"
                 onChange={(e) => setRun(e.target.value)} />
          <input value={batch} placeholder="Batch (all)" aria-label="Batch" className="search-narrow"
                 onChange={(e) => setBatch(e.target.value)} />
          <label className="search-check">
            <input type="checkbox" checked={wholeWord} onChange={(e) => setWholeWord(e.target.checked)} />
            Whole word
          </label>
          <button type="submit" className="landing-file-viewer-btn" disabled={busy || query.trim().length < 2}>
            <Search size={14} aria-hidden="true" />
            {busy ? "Searching…" : "Search"}
          </button>
        </form>

        {history.length ? (
          <div className="search-history" aria-label="Earlier searches">
            <span className="search-history-label">
              <History size={13} aria-hidden="true" />
              History
            </span>
            {history.map((h) => (
              <span key={h.id} className={`search-history-chip${h.id === selected ? " is-active" : ""}`}>
                <button
                  type="button"
                  className="search-history-open"
                  title={`${h.wholeWord ? "Whole word" : "Part of a word"}${h.run ? ` · run ${h.run}` : ""}${h.batch ? ` · batch ${h.batch}` : ""} · ${fmtAt(h.at)}`}
                  onClick={() => recall(h)}
                  disabled={busy}
                >
                  “{h.q}”{h.run || h.batch ? ` (${[h.run, h.batch].filter(Boolean).join(" / ")})` : ""}
                  <span className="search-history-count">
                    {plural(h.charts, "chart")} · {h.hits} words · {h.headerHits} headers
                  </span>
                </button>
                <button type="button" className="search-history-forget" aria-label={`Remove “${h.q}”`} onClick={() => forget(h)}>
                  <X size={12} aria-hidden="true" />
                </button>
              </span>
            ))}
          </div>
        ) : null}
      </section>

      {error ? (
        <PageMessage kind="error" onClose={() => setError(null)}>
          {error}
        </PageMessage>
      ) : null}

      {result && current ? (
        <section className="accuracy-section users-list" aria-label="Results">
          <h2>
            {result.charts.length
              ? `“${result.query}”: ${plural(result.total_hits, "word hit")} on ${plural(result.total_pages, "page")}, ` +
                `${plural(result.total_header_hits, "header hit")}, in ${plural(result.total_charts, "chart")}`
              : `No page's Tesseract text or section header has “${result.query}”`}
          </h2>
          {result.truncated ? (
            <p className="accuracy-note">
              Showing the first {result.max_pages} pages. Narrow it by run or batch to see the rest.
            </p>
          ) : null}

          {result.batches.length ? (
            <div className="search-batches">
              {result.batches.map((b) => (
                <span key={`${b.run}|${b.batch}`} className="search-batch-chip">
                  {b.run} / {b.batch}: {plural(b.charts, "chart")}, {b.hits} words on {plural(b.pages, "page")},{" "}
                  {b.header_hits} headers
                </span>
              ))}
            </div>
          ) : null}

          {result.charts.length ? (
            <table className="playground-users search-table">
              <thead>
                <tr>
                  <th aria-label="Expand">
                    <button
                      type="button"
                      className="search-expand"
                      aria-label={allOpen ? "Collapse all" : "Expand all"}
                      onClick={() => setOpen(allOpen ? new Set() : new Set(result.charts.map((c) => c.chart)))}
                    >
                      {allOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </button>
                  </th>
                  <th>Run / Batch</th>
                  <th>Chart</th>
                  <th className="search-num">Pages in chart</th>
                  <th className="search-num">Pages with word hits</th>
                  <th className="search-num">Word hits</th>
                  <th className="search-num">Pages with header hits</th>
                  <th className="search-num">Header hits</th>
                </tr>
              </thead>
              <tbody>
                {result.charts.map((chart) => (
                  <ChartRows
                    key={chart.chart}
                    chart={chart}
                    open={open.has(chart.chart)}
                    onToggle={() => toggle(chart.chart)}
                    link={chartLink(chart)}
                    pageLink={(n, name) => pageLink(chart.chart, n, name)}
                  />
                ))}
              </tbody>
            </table>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

function ChartRows({
  chart,
  open,
  onToggle,
  link,
  pageLink,
}: {
  chart: OcrSearchChart;
  open: boolean;
  onToggle: () => void;
  link: JSX.Element;
  pageLink: (pageNumber: number | null | undefined, pageName: string) => JSX.Element;
}) {
  return (
    <Fragment>
      <tr className={`search-row${open ? " is-open" : ""}`} onClick={onToggle}>
        <td>
          <button type="button" className="search-expand" aria-expanded={open} aria-label={open ? "Collapse" : "Expand"}
                  onClick={(e) => { e.stopPropagation(); onToggle(); }}>
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
        </td>
        <td>
          {chart.run} / {chart.batch}
        </td>
        <td className="search-chart-name">{link}</td>
        <td className="search-num">{chart.total_pages ?? "—"}</td>
        <td className="search-num">{chart.pages.length}</td>
        <td className="search-num">{chart.hits}</td>
        <td className="search-num">{chart.header_pages}</td>
        <td className="search-num">{chart.header_hits}</td>
      </tr>
      {open ? (
        <tr className="search-detail">
          <td />
          <td colSpan={7}>
            <div className="search-detail-grid">
              <div>
                <h3>Word hits ({chart.hits} on {plural(chart.pages.length, "page")})</h3>
                {chart.pages.length ? (
                  <table className="playground-users search-pages">
                    <thead>
                      <tr>
                        <th>Page</th>
                        <th>Hits</th>
                        <th>Where</th>
                      </tr>
                    </thead>
                    <tbody>
                      {chart.pages.map((page) => (
                        <tr key={page.page_name}>
                          <td title={page.page_name}>{pageLink(page.page_number, page.page_name)}</td>
                          <td>{page.hits}</td>
                          <td>
                            {page.snippets.map((s, i) => (
                              <div key={i} className="search-snippet">
                                <Snippet s={s} />
                              </div>
                            ))}
                            {page.hits > page.snippets.length ? (
                              <div className="accuracy-note">and {page.hits - page.snippets.length} more on this page</div>
                            ) : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <p className="accuracy-note">Not in the Tesseract text of any page.</p>
                )}
              </div>
              <div>
                <h3>Section headers ({chart.header_hits} on {plural(chart.header_pages, "page")})</h3>
                {chart.headers.length ? (
                  <table className="playground-users search-pages">
                    <thead>
                      <tr>
                        <th>Page</th>
                        <th>Header</th>
                      </tr>
                    </thead>
                    <tbody>
                      {chart.headers.map((h, i) => (
                        <tr key={`${h.page_name}|${i}`}>
                          <td title={h.page_name}>{pageLink(h.page_number, h.page_name)}</td>
                          <td className="search-snippet">
                            <Snippet s={h.snippet} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <p className="accuracy-note">No section header matches.</p>
                )}
              </div>
            </div>
          </td>
        </tr>
      ) : null}
    </Fragment>
  );
}
