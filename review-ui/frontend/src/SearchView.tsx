import { useState, type FormEvent } from "react";
import { ArrowLeft, Search } from "lucide-react";
import PageMessage from "./PageMessage";
import { errorDetail, searchOcr, type OcrSearchResult } from "./api";

type Props = {
  onBack: () => void;
  /** Open a chart in the viewer (imaging output). */
  onOpenChart: (chart: string) => void;
  chartHref: (chart: string) => string;
};

/** Admins only: find a word or phrase in every page's preliminary OCR (Tesseract) text. */
export default function SearchView({ onBack, onOpenChart, chartHref }: Props) {
  const [query, setQuery] = useState("");
  const [wholeWord, setWholeWord] = useState(true);
  const [run, setRun] = useState("");
  const [batch, setBatch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<OcrSearchResult | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (query.trim().length < 2 || busy) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await searchOcr({ q: query.trim(), wholeWord, run, batch }));
    } catch (err) {
      setResult(null);
      setError(errorDetail(err, "Could not search the OCR text"));
    } finally {
      setBusy(false);
    }
  }

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
          Finds a word or phrase in every page's preliminary OCR (Tesseract) text, grouped by batch and chart.
          Case does not matter; a phrase may run across line breaks.
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
      </section>

      {error ? (
        <PageMessage kind="error" onClose={() => setError(null)}>
          {error}
        </PageMessage>
      ) : null}

      {result ? (
        <section className="accuracy-section users-list" aria-label="Results">
          <h2>
            {result.total_hits
              ? `“${result.query}”: ${result.total_hits} hit${result.total_hits === 1 ? "" : "s"} on ${result.total_pages} page${result.total_pages === 1 ? "" : "s"} in ${result.total_charts} chart${result.total_charts === 1 ? "" : "s"}`
              : `No page's Tesseract text has “${result.query}”`}
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
                  {b.run} / {b.batch}: {b.charts} chart{b.charts === 1 ? "" : "s"}, {b.pages} page
                  {b.pages === 1 ? "" : "s"}, {b.hits} hit{b.hits === 1 ? "" : "s"}
                </span>
              ))}
            </div>
          ) : null}

          {result.charts.map((chart) => (
            <div key={chart.chart} className="search-chart">
              <div className="search-chart-head">
                <a
                  href={chartHref(chart.chart)}
                  onClick={(e) => {
                    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                    e.preventDefault();
                    onOpenChart(chart.chart);
                  }}
                >
                  {chart.chart}
                </a>
                <span className="accuracy-note">
                  {chart.run} / {chart.batch} · {chart.hits} hit{chart.hits === 1 ? "" : "s"} on {chart.pages.length} page
                  {chart.pages.length === 1 ? "" : "s"}
                </span>
              </div>
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
                    <tr key={`${chart.chart}|${page.page_name}`}>
                      <td title={page.page_name}>{page.page_number ?? page.page_name}</td>
                      <td>{page.hits}</td>
                      <td>
                        {page.snippets.map((s, i) => (
                          <div key={i} className="search-snippet">
                            {s.before}
                            <mark>{s.match}</mark>
                            {s.after}
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
            </div>
          ))}
        </section>
      ) : null}
    </div>
  );
}
