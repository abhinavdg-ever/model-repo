import { Fragment, useEffect, useState, type FormEvent, type MouseEvent } from "react";
import { ChevronDown, ChevronRight, Plus, Search, X } from "lucide-react";
import PageMessage from "./PageMessage";
import {
  errorDetail,
  searchValues,
  valueSearchColumns,
  valueSearchOptions,
  type ValueCondition,
  type ValueOperator,
  type ValueSearchChart,
  type ValueSearchColumn,
  type ValueSearchResult,
  type ValueSide,
} from "./api";

type Props = { chartHref: (chart: string, page?: number) => string };

const SIDE_LABEL: Record<ValueSide, string> = { prediction: "Prediction", ground_truth: "Ground Truth" };
const OP_LABEL: Record<ValueOperator, string> = { is: "is", contains: "contains", empty: "is empty" };

/** Kept while the tab is open, so Back and returning keeps the last search. */
let kept: { conditions: ValueCondition[]; run: string; batch: string; result: ValueSearchResult | null } | null = null;

function blankCondition(column = "page_type"): ValueCondition {
  return { column, side: "prediction", op: "is", value: "" };
}

function plural(n: number, word: string): string {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

function openInNewTab(event: MouseEvent<HTMLAnchorElement>, href: string) {
  event.preventDefault();
  window.open(href, "_blank");
}

/** Admins only: pages whose Prediction (Final values) or Ground Truth has columns at values. */
export default function ValueSearchPanel({ chartHref }: Props) {
  const [columns, setColumns] = useState<ValueSearchColumn[]>([]);
  const [conditions, setConditions] = useState<ValueCondition[]>(kept?.conditions ?? [blankCondition()]);
  const [run, setRun] = useState(kept?.run ?? "");
  const [batch, setBatch] = useState(kept?.batch ?? "");
  const [result, setResult] = useState<ValueSearchResult | null>(kept?.result ?? null);
  const [options, setOptions] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());

  useEffect(() => {
    valueSearchColumns()
      .then((r) => setColumns(r.columns))
      .catch((err) => setError(errorDetail(err, "Could not read the columns")));
  }, []);

  // The value picker: each column / side's values, read once.
  useEffect(() => {
    for (const c of conditions) {
      const key = `${c.column}|${c.side}`;
      if (key in options) continue;
      setOptions((prev) => ({ ...prev, [key]: [] }));
      valueSearchOptions(c.column, c.side)
        .then((r) => setOptions((prev) => ({ ...prev, [key]: r.options.map((o) => o.value) })))
        .catch(() => undefined);
    }
  }, [conditions, options]);

  function update(i: number, patch: Partial<ValueCondition>) {
    setConditions((prev) => prev.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  }

  const ready = conditions.length > 0 && conditions.every((c) => c.op === "empty" || c.value.trim());

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      const found = await searchValues({ conditions, run, batch });
      setResult(found);
      setOpen(new Set());
      kept = { conditions, run, batch, result: found };
    } catch (err) {
      setError(errorDetail(err, "Could not search the values"));
    } finally {
      setBusy(false);
    }
  }

  const allOpen = Boolean(result?.charts.length) && result!.charts.every((c) => open.has(c.chart));

  return (
    <>
      <section className="accuracy-section users-form search-form" aria-label="Search by value">
        <p className="accuracy-note">
          Finds pages whose Prediction (the Final values) or Ground Truth has a column at a value. Every condition must
          hold. Case and extra spaces do not matter.
        </p>
        <form onSubmit={submit}>
          {conditions.map((c, i) => {
            const listId = `value-options-${i}`;
            return (
              <div key={i} className="search-bar value-condition">
                <select value={c.side} aria-label="Side" onChange={(e) => update(i, { side: e.target.value as ValueSide })}>
                  {(Object.keys(SIDE_LABEL) as ValueSide[]).map((s) => (
                    <option key={s} value={s}>{SIDE_LABEL[s]}</option>
                  ))}
                </select>
                <select value={c.column} aria-label="Column" onChange={(e) => update(i, { column: e.target.value, value: "" })}>
                  {columns.map((col) => (
                    <option key={col.key} value={col.key}>{col.label}</option>
                  ))}
                </select>
                <select value={c.op} aria-label="Operator" onChange={(e) => update(i, { op: e.target.value as ValueOperator })}>
                  {(Object.keys(OP_LABEL) as ValueOperator[]).map((o) => (
                    <option key={o} value={o}>{OP_LABEL[o]}</option>
                  ))}
                </select>
                {c.op !== "empty" ? (
                  <>
                    <input value={c.value} list={listId} placeholder="Value" aria-label="Value"
                           onChange={(e) => update(i, { value: e.target.value })} />
                    <datalist id={listId}>
                      {(options[`${c.column}|${c.side}`] ?? []).map((v) => (
                        <option key={v} value={v} />
                      ))}
                    </datalist>
                  </>
                ) : null}
                {conditions.length > 1 ? (
                  <button type="button" className="search-history-forget" aria-label="Remove condition"
                          onClick={() => setConditions((prev) => prev.filter((_, j) => j !== i))}>
                    <X size={12} aria-hidden="true" />
                  </button>
                ) : null}
              </div>
            );
          })}
          <div className="search-bar">
            <button type="button" className="back-btn" disabled={conditions.length >= 6}
                    onClick={() => setConditions((prev) => [...prev, blankCondition(prev[prev.length - 1]?.column)])}>
              <Plus size={14} aria-hidden="true" />
              And
            </button>
            <input value={run} placeholder="Run (all)" aria-label="Run" className="search-narrow"
                   onChange={(e) => setRun(e.target.value)} />
            <input value={batch} placeholder="Batch (all)" aria-label="Batch" className="search-narrow"
                   onChange={(e) => setBatch(e.target.value)} />
            <button type="submit" className="landing-file-viewer-btn" disabled={busy || !ready}>
              <Search size={14} aria-hidden="true" />
              {busy ? "Searching…" : "Search"}
            </button>
          </div>
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
            {result.charts.length
              ? `${plural(result.total_pages, "page")} in ${plural(result.total_charts, "chart")}`
              : "No page matches"}
          </h2>
          {result.truncated ? (
            <p className="accuracy-note">Showing the first {result.max_pages} pages. Narrow it by run or batch to see the rest.</p>
          ) : null}
          {result.batches.length ? (
            <div className="search-batches">
              {result.batches.map((b) => (
                <span key={`${b.run}|${b.batch}`} className="search-batch-chip">
                  {b.run} / {b.batch}: {plural(b.charts, "chart")}, {plural(b.pages, "page")}
                </span>
              ))}
            </div>
          ) : null}
          {result.charts.length ? (
            <table className="playground-users search-table">
              <thead>
                <tr>
                  <th aria-label="Expand">
                    <button type="button" className="search-expand" aria-label={allOpen ? "Collapse all" : "Expand all"}
                            onClick={() => setOpen(allOpen ? new Set() : new Set(result.charts.map((c) => c.chart)))}>
                      {allOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </button>
                  </th>
                  <th>Run / Batch</th>
                  <th>Chart</th>
                  <th className="search-num">Pages in chart</th>
                  <th className="search-num">Matching pages</th>
                </tr>
              </thead>
              <tbody>
                {result.charts.map((chart) => (
                  <ChartRows key={chart.chart} chart={chart} columns={result.columns} open={open.has(chart.chart)}
                             chartHref={chartHref}
                             onToggle={() => setOpen((prev) => {
                               const next = new Set(prev);
                               if (next.has(chart.chart)) next.delete(chart.chart);
                               else next.add(chart.chart);
                               return next;
                             })} />
                ))}
              </tbody>
            </table>
          ) : null}
        </section>
      ) : null}
    </>
  );
}

function ChartRows({ chart, columns, open, onToggle, chartHref }: {
  chart: ValueSearchChart;
  columns: ValueSearchColumn[];
  open: boolean;
  onToggle: () => void;
  chartHref: (chart: string, page?: number) => string;
}) {
  const first = chart.pages.map((p) => p.page_number).filter((n): n is number => typeof n === "number");
  const firstPage = first.length ? Math.min(...first) : undefined;
  return (
    <Fragment>
      <tr className={`search-row${open ? " is-open" : ""}`} onClick={onToggle}>
        <td>
          <button type="button" className="search-expand" aria-expanded={open} aria-label={open ? "Collapse" : "Expand"}
                  onClick={(e) => { e.stopPropagation(); onToggle(); }}>
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
        </td>
        <td>{chart.run} / {chart.batch}</td>
        <td className="search-chart-name">
          <a href={chartHref(chart.chart, firstPage)} title="Open at the first matching page in a new tab"
             onClick={(e) => { e.stopPropagation(); openInNewTab(e, chartHref(chart.chart, firstPage)); }}>
            {chart.chart}
          </a>
        </td>
        <td className="search-num">{chart.total_pages ?? "—"}</td>
        <td className="search-num">{chart.pages.length}</td>
      </tr>
      {open ? (
        <tr className="search-detail">
          <td />
          <td colSpan={4}>
            <table className="playground-users search-pages">
              <thead>
                <tr>
                  <th rowSpan={2}>Page</th>
                  {columns.map((c) => <th key={c.key} colSpan={2}>{c.label}</th>)}
                </tr>
                <tr>
                  {columns.map((c) => (
                    <Fragment key={c.key}>
                      <th>Prediction</th>
                      <th>Ground Truth</th>
                    </Fragment>
                  ))}
                </tr>
              </thead>
              <tbody>
                {chart.pages.map((page) => (
                  <tr key={page.page_name}>
                    <td title={page.page_name}>
                      {page.page_number == null ? page.page_name : (
                        <a href={chartHref(chart.chart, page.page_number)} title={`Open page ${page.page_number} in a new tab`}
                           onClick={(e) => openInNewTab(e, chartHref(chart.chart, page.page_number ?? undefined))}>
                          {page.page_number}
                        </a>
                      )}
                    </td>
                    {columns.map((c) => (
                      <Fragment key={c.key}>
                        <td>{page.values[c.key]?.prediction || "—"}</td>
                        <td>{page.values[c.key]?.ground_truth || "—"}</td>
                      </Fragment>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </td>
        </tr>
      ) : null}
    </Fragment>
  );
}
