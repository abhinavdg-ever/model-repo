import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  ArrowLeft,
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Filter,
  Info,
  Loader2,
  RefreshCw,
  Search,
} from "lucide-react";
import { formatBatchLabel, formatRunLabel, getAccuracyReport } from "./api";
import { saveAccuracyOrder } from "./chartSequence";
import { useViewState } from "./viewState";
import {
  ClearAllFilters,
  ColumnFilterButton,
  BLANK_META,
  META_FILTERS,
  MetaFilterBar,
  TopCheckFilter,
  metaFilterCount,
  type MetaFilterKey,
  type MetaFilters,
} from "./columnFilter";
import { formatRunAfter as fmtRunAfter, ranAfter, RunAfterForm } from "./runAfter";
import { readGroundTruthSource } from "./groundTruthSource";
import { scoreFinalOnly } from "./clientView";
import {
  LIVE_METRICS,
  METRICS,
  MIN_LAST_RUN,
  MIN_LAST_RUN_LABEL,
  accuracyPercent,
  credMatchPercent,
  f1Percent,
  precisionPercent,
  recallPercent,
  formatCredMatch,
  formatDistribution,
  formatF1,
  formatPrecision,
  formatRecall,
  bandCounts,
  MATCH_BANDS,
  matchBand,
  matchCounts,
  OVERALL_STREAMS,
  scoreChart,
  sumRates,
  sumTallies,
  type ChartAccuracy,
  type ClassRates,
  type MetricId,
  type Tally,
} from "./accuracy";

type Props = {
  onBack: () => void;
  onOpenChart: (folderId: string) => void;
  /** The chart's URL, so it can open in a new tab. */
  chartHref: (folderId: string) => string;
};

function percentLabel(tally: Tally): string {
  const pct = accuracyPercent(tally);
  return pct == null ? "—" : `${pct}%`;
}

function fmtUpdated(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  }).format(d);
}

/** Date on one line, time under it, so the column stays narrow. */
export function DateLines({ iso }: { iso: string | null | undefined }) {
  const text = fmtUpdated(iso);
  const cut = text.lastIndexOf(", ");
  if (cut < 0) return <>{text}</>;
  return (
    <>
      <span>{text.slice(0, cut)}</span>
      <span>{text.slice(cut + 2)}</span>
    </>
  );
}

/** An ⓘ that shows what a number means: on hover, and on click / tap. */
/** One titled block of an info popover; each line is drawn on its own. */
type InfoSection = { title?: string; lines: string[] };

/** "A. B. C." -> ["A.", "B.", "C."]: one sentence per line. */
function sentences(text: string): string[] {
  return text
    .split(/(?<=\.)\s+(?=[A-Z("'])/)
    .map((line) => line.trim())
    .filter(Boolean);
}

/** Split on "; " outside brackets: "A (x; y); B" -> ["A (x; y)", "B"]. */
function measures(text: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "(") depth += 1;
    else if (ch === ")") depth = Math.max(0, depth - 1);
    else if (ch === ";" && depth === 0) {
      out.push(text.slice(start, i));
      start = i + 1;
    }
  }
  out.push(text.slice(start));
  return out.map((line) => line.trim().replace(/\.$/, "")).filter(Boolean);
}

/** A metric's rule as sections: how it is scored, then its "Under X = Yes (…): A; B; C"
 * block with one measure per line. */
function ruleSections(title: string, rule: string): InfoSection[] {
  const at = rule.search(/\bUnder [^:]+:/);
  const head = at >= 0 ? rule.slice(0, at) : rule;
  const out: InfoSection[] = [{ title, lines: sentences(head) }];
  if (at >= 0) {
    const under = rule.slice(at);
    const colon = under.indexOf(":");
    out.push({
      title: under.slice(0, colon).trim(),
      lines: measures(under.slice(colon + 1)),
    });
  }
  return out;
}

const POP_WIDTH = 360;
const POP_MARGIN = 8;

/** ⓘ with a popover kept inside the window: fixed-position from the button, clamped
 * left and right, opening upwards when there is more room above, scrolling when long.
 * Rendered into document.body: .workspace's backdrop-filter would otherwise make
 * "fixed" relative to it, not to the window. */
function InfoTip({ text, className }: { text: string | InfoSection[]; className?: string }) {
  const [place, setPlace] = useState<{ top?: number; bottom?: number; left: number; width: number; maxHeight: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const blocks: InfoSection[] = typeof text === "string" ? [{ lines: sentences(text) }] : text;

  useEffect(() => {
    if (!place) return;
    const inside = (target: EventTarget | null) =>
      target instanceof Node && (popRef.current?.contains(target) || buttonRef.current?.contains(target));
    const close = (event: Event) => {
      if (!inside(event.target)) setPlace(null);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPlace(null);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
    // Scrolling the page leaves the popover behind its button; scrolling inside it does not.
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [place]);

  function toggle() {
    if (place) {
      setPlace(null);
      return;
    }
    const rect = buttonRef.current?.getBoundingClientRect();
    if (!rect) return;
    const width = Math.min(POP_WIDTH, window.innerWidth - 2 * POP_MARGIN);
    const centre = rect.left + rect.width / 2;
    const left = Math.max(POP_MARGIN, Math.min(centre - width / 2, window.innerWidth - width - POP_MARGIN));
    const below = window.innerHeight - rect.bottom - POP_MARGIN - 6;
    const above = rect.top - POP_MARGIN - 6;
    setPlace(
      below >= 240 || below >= above
        ? { top: rect.bottom + 6, left, width, maxHeight: below }
        : { bottom: window.innerHeight - rect.top + 6, left, width, maxHeight: above },
    );
  }

  return (
    <span className={className ? `accuracy-info ${className}` : "accuracy-info"}>
      <button
        ref={buttonRef}
        type="button"
        className="accuracy-info-btn"
        aria-label="How this is calculated"
        aria-expanded={place !== null}
        onClick={toggle}
      >
        <Info size={13} aria-hidden="true" />
      </button>
      {place ? createPortal(
        <div
          ref={popRef}
          className="accuracy-info-pop"
          role="tooltip"
          style={{
            top: place.top,
            bottom: place.bottom,
            left: place.left,
            width: place.width,
            maxHeight: place.maxHeight,
          }}
        >
          {blocks.map((block, index) => (
            <section key={index} className="accuracy-info-section">
              {block.title ? <h4>{block.title}</h4> : null}
              {block.lines.length > 1 ? (
                <ul>
                  {block.lines.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              ) : (
                <p>{block.lines[0]}</p>
              )}
            </section>
          ))}
        </div>,
        document.body,
      ) : null}
    </span>
  );
}

/** "Match n · Partial n · No Match n", wrapping only between the parts. */
function Distribution({ tally, binary = false }: { tally: Tally; binary?: boolean }) {
  return (
    <>
      {formatDistribution(tally, binary)
        .split(" · ")
        .map((part, index) => (
          <Fragment key={part}>
            {index > 0 ? " · " : null}
            <span>{part}</span>
          </Fragment>
        ))}
    </>
  );
}

/** Class 1 pages in the ground truth: each is a TP or an FN (a rotated page
 * found at the wrong angle is an FN too). */
function classCases(rates: ClassRates): number {
  // Provider Signature: every page either side calls signed.
  if (hasCredMatch(rates)) return rates.tp + rates.fp + rates.fn;
  return rates.tp + rates.fn;
}

/** Provider Signature carries credMatch: its third figure is Cred. Match, not F1. */
function hasCredMatch(rates: ClassRates): boolean {
  return rates.credMatch != null;
}

/** Precision / Recall / F1 (Cred. Match for Provider Signature) for Class 1, label above value. */
function RatesGrid({ rates, compact = false }: { rates: ClassRates; compact?: boolean }) {
  return (
    <dl className="accuracy-class-grid">
      {[
        ["Precision", formatPrecision(rates)],
        ["Recall", formatRecall(rates)],
        // The table's narrow cells say "Cred M%"; the donut card spells it out.
        hasCredMatch(rates) ? [compact ? "Cred M%" : "Cred. M", formatCredMatch(rates)] : ["F1", formatF1(rates)],
      ].map(([name, value]) => (
        <div key={name}>
          <dt>{name}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Table columns drawn narrower / wider than the rest (styles.css .accuracy-cell-narrow /
 * -wide / -member). */
const NARROW_METRICS = new Set(["pageType", "codeable"]);
const WIDE_METRICS = new Set(["signature"]);
const MEMBER_METRICS = new Set(["member"]);

/** One table cell: the donut card's numbers, compact. */
function CompactScore({
  tally,
  binary = false,
  rates,
  narrow = false,
  wide = false,
  member = false,
}: {
  tally: Tally;
  binary?: boolean;
  rates?: ClassRates;
  narrow?: boolean;
  wide?: boolean;
  /** Member Verification: the widest column. */
  member?: boolean;
}) {
  if (tally.scored === 0) return <>—</>;
  const size = narrow ? " accuracy-cell-narrow" : wide ? " accuracy-cell-wide" : member ? " accuracy-cell-member" : "";
  return (
    <div className={`accuracy-cell${size}`}>
      <strong>{percentLabel(tally)}</strong>
      <p className="accuracy-distribution">
        <Distribution tally={tally} binary={binary} />
      </p>
      {rates ? <RatesGrid rates={rates} compact /> : null}
    </div>
  );
}

const SCORE_INFO =
  "Accuracy score = (Match + 0.5 × Partial) / Total, where Total = Match + Partial + No Match. Only Page Type has a Partial.";
const OVERALL_SCORE_INFO =
  "Accuracy score = pages with all seven streams matching (7/7) / all scored pages.";
const RATES_INFO =
  "The accuracy above counts every labelled page, Yes and No. Below it, Class 1 (the Yes pages) only; cases = the pages the ground truth calls Class 1. " +
  "Precision = TP / (TP + FP): share of the pipeline's Class 1 calls that were right. " +
  "Recall = TP / (TP + FN): share of the real Class 1 pages the pipeline caught. " +
  "F1 = 2 × Precision × Recall / (Precision + Recall). " +
  "Provider Signature shows Cred. M (credentials match) instead of F1: of the pages the ground truth gives credentials for, the share whose credentials match the ground truth's. " +
  "These matter because Class 1 pages are rare: the overall accuracy can be high while they are missed.";

function Donut({
  label,
  detail,
  tally,
  rates,
  ratesLabel,
  binary = false,
}: {
  label: string;
  detail: string;
  tally: Tally;
  /** Match or No Match only. */
  binary?: boolean;
  /** Class 1 counts: Precision, Recall and F1 are shown for them. */
  rates?: ClassRates;
  /** What Class 1 is ("Blank / Junk = Yes"). */
  ratesLabel?: string;
}) {
  const radius = 36;
  const cases = rates ? classCases(rates) : 0;
  const circumference = 2 * Math.PI * radius;
  const segments = [
    { key: "yes", count: tally.yes, tone: "accuracy-donut-yes", opacity: 1 },
    { key: "maybe", count: tally.maybe, tone: "accuracy-donut-maybe", opacity: 1 },
    { key: "no", count: tally.no, tone: "accuracy-donut-no", opacity: 1 },
  ];
  let offset = 0;
  const arcs = segments.flatMap((segment) => {
    if (tally.scored === 0 || segment.count === 0) return [];
    const length = (segment.count / tally.scored) * circumference;
    const arc = (
      <circle
        key={segment.key}
        className={`accuracy-donut-seg ${segment.tone}`}
        cx="50"
        cy="50"
        r={radius}
        strokeOpacity={segment.opacity < 1 ? segment.opacity : undefined}
        strokeDasharray={length >= circumference - 0.01 ? undefined : `${length} ${circumference - length}`}
        strokeDashoffset={-offset}
      />
    );
    offset += length;
    return [arc];
  });
  return (
    <figure className="accuracy-donut-card">
      <svg className="accuracy-donut" viewBox="0 0 100 100" role="img" aria-label={rates ? `${label} ${percentLabel(tally)}. ${formatDistribution(tally, binary)}. Precision ${formatPrecision(rates)}. Recall ${formatRecall(rates)}. ${hasCredMatch(rates) ? `Cred. M ${formatCredMatch(rates)}` : `F1 ${formatF1(rates)}`}` : `${label} ${percentLabel(tally)}. ${formatDistribution(tally, binary)}`}>
        <circle className="accuracy-donut-track" cx="50" cy="50" r={radius} />
        {arcs}
        <text className="accuracy-donut-pct" x="50" y={tally.scored > 0 ? 51 : 54}>
          {percentLabel(tally)}
        </text>
        {tally.scored > 0 ? (
          <text className="accuracy-donut-caption" x="50" y="64">
            Accuracy
          </text>
        ) : null}
      </svg>
      <InfoTip
        className="accuracy-info-corner"
        text={[
          ...ruleSections(label, detail),
          { title: "Accuracy score", lines: sentences(tally.fullOnly ? OVERALL_SCORE_INFO : SCORE_INFO) },
          ...(rates ? [{ title: "Precision, Recall, F1", lines: sentences(RATES_INFO) }] : []),
        ]}
      />
      <figcaption>{label}</figcaption>
      <p className="accuracy-distribution">
        <Distribution tally={tally} binary={binary} />
      </p>
      {rates ? (
        <div className="accuracy-class">
          {ratesLabel ? (
            <div className="accuracy-class-head">
              {ratesLabel} ({cases.toLocaleString()} {cases === 1 ? "case" : "cases"})
            </div>
          ) : null}
          <RatesGrid rates={rates} />
        </div>
      ) : null}
    </figure>
  );
}

const OVERALL_RULE =
  "Seven streams per page: Member, DOS, Page Type (exact match only; a Partial is not a match), Provider Signature, Codability, Blank / Junk and Orientation. " +
  "Each page counts how many of the seven match, 0 to 7; a stream with no ground-truth label does not count against the page. " +
  "Accuracy = pages with 7 of 7 / all scored pages. Good = 5 to 7 matching, Fair = 3 or 4, Bad = 0 to 2.";

function pct(count: number, total: number): string {
  return total > 0 ? `${Math.round((100 * count) / total)}%` : "—";
}

/** Overall: one ring split by how many of the seven streams match (7 .. 0), each
 * slice labelled with its k, its pages and share on hover, and a 0..7 table beside it
 * (the chip colours are Good / Fair / Bad). */
function OverallDonut({ tally }: { tally: Tally }) {
  const radius = 36;
  const circumference = 2 * Math.PI * radius;
  const counts = matchCounts(tally);
  const total = tally.scored;
  const bands = bandCounts(tally);
  let offset = 0;
  const slices = counts.flatMap(([k, pages]) => {
    if (total === 0 || pages === 0) return [];
    const length = (pages / total) * circumference;
    const middle = ((offset + length / 2) / circumference) * 2 * Math.PI - Math.PI / 2;
    const slice = { k, pages, length, start: offset, x: 50 + radius * Math.cos(middle), y: 50 + radius * Math.sin(middle) };
    offset += length;
    return [slice];
  });
  return (
    <figure className="accuracy-donut-card accuracy-overall-card">
      <InfoTip
        className="accuracy-info-corner"
        text={[...ruleSections("Overall", OVERALL_RULE), { title: "Accuracy score", lines: sentences(OVERALL_SCORE_INFO) }]}
      />
      <div className="accuracy-overall-body">
        <div className="accuracy-overall-ring">
        <svg
          className="accuracy-donut accuracy-overall-donut"
          viewBox="0 0 100 100"
          role="img"
          aria-label={`Overall ${percentLabel(tally)}: ${counts.map(([k, pages]) => `${k} of ${OVERALL_STREAMS} matching ${pages}`).join(", ")}`}
        >
          <circle className="accuracy-donut-track" cx="50" cy="50" r={radius} />
          {slices.map((slice) => (
            <circle
              key={slice.k}
              className={`accuracy-donut-seg accuracy-match-${matchBand(slice.k)} accuracy-match-k${slice.k}`}
              cx="50"
              cy="50"
              r={radius}
              strokeDasharray={slice.length >= circumference - 0.01 ? undefined : `${slice.length} ${circumference - slice.length}`}
              strokeDashoffset={-slice.start}
            >
              <title>{`${slice.k}/${OVERALL_STREAMS} matching: ${slice.pages.toLocaleString()} ${slice.pages === 1 ? "page" : "pages"} (${pct(slice.pages, total)})`}</title>
            </circle>
          ))}
          {slices
            .filter((slice) => slice.length / circumference >= 0.05)
            .map((slice) => (
              <text key={`l${slice.k}`} className="accuracy-overall-callout" x={slice.x} y={slice.y + 1.6}>
                {slice.k}
              </text>
            ))}
          <text className="accuracy-donut-pct" x="50" y={total > 0 ? 51 : 54}>
            {percentLabel(tally)}
          </text>
          {total > 0 ? (
            <text className="accuracy-donut-caption" x="50" y="64">
              {OVERALL_STREAMS}/{OVERALL_STREAMS}
            </text>
          ) : null}
        </svg>
          <figcaption>Overall</figcaption>
        </div>
        <table className="accuracy-overall-legend">
          <thead>
            <tr>
              <th scope="col">Matching</th>
              <th scope="col">Pages</th>
              <th scope="col">%</th>
            </tr>
          </thead>
          <tbody>
            {counts.map(([k, pages]) => (
              <tr key={k} title={`${k}/${OVERALL_STREAMS} matching: ${pages.toLocaleString()} pages (${pct(pages, total)})`}>
                <td>
                  <span className={`accuracy-overall-chip accuracy-match-${matchBand(k)} accuracy-match-k${k}`} aria-hidden="true" />
                  {k}/{OVERALL_STREAMS}
                </td>
                <td>{pages.toLocaleString()}</td>
                <td>{pct(pages, total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="accuracy-overall-bands">
        {MATCH_BANDS.map((item) => (
          <div key={item.id} className={`accuracy-overall-band accuracy-band-${item.id}`} title={`${item.label}: ${item.min}–${item.max} of ${OVERALL_STREAMS} matching`}>
            <span>
              {item.label} ({item.min}–{item.max})
            </span>
            <strong>{pct(bands[item.id], total)}</strong>
            <small>{bands[item.id].toLocaleString()} pages</small>
          </div>
        ))}
      </div>
    </figure>
  );
}

/** Shown while the report loads: the scoring reads every chart with ground truth. */
function CalculatingPanel() {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <div className="accuracy-calculating" role="status" aria-live="polite">
      <Loader2 className="accuracy-calculating-spin" size={28} aria-hidden="true" />
      <p className="accuracy-calculating-title">Please wait while we calculate accuracy…</p>
      <p className="accuracy-calculating-detail">
        Comparing every page that has ground truth with the pipeline&apos;s results.
        {seconds >= 3 ? ` ${seconds}s` : ""}
      </p>
    </div>
  );
}

const CHARTS_PER_PAGE = 10;

type SortKey = "name" | "run" | "verified" | "pages" | "overall" | MetricId;
type SortDir = "asc" | "desc";

/** A score column: one metric, or Overall. */
type ScoreColumn = MetricId | "overall";

/** Inclusive percent range; a row with no score is hidden while a range is set. */
type Range = { min: number; max: number };

/** What a column can be filtered on: its Accuracy, and Precision / Recall / F1 of Class Yes
 * (Cred. Match in place of F1 for Provider Signature). */
type Measure = "accuracy" | "precision" | "recall" | "f1" | "credMatch";
type ColumnRanges = Partial<Record<Measure, Range>>;
const RATE_MEASURES = [
  { key: "precision", label: "Precision", percent: precisionPercent },
  { key: "recall", label: "Recall", percent: recallPercent },
  { key: "f1", label: "F1", percent: f1Percent },
  { key: "credMatch", label: "Cred. M", percent: credMatchPercent },
] as const;

/** The rate measures a column offers: F1, or Cred. Match on Provider Signature. */
function measuresFor(column: ScoreColumn) {
  return RATE_MEASURES.filter((measure) =>
    column === "signature" ? measure.key !== "f1" : measure.key !== "credMatch",
  );
}

/** Filters keyed by column, then by measure. */
type Filters = Partial<Record<ScoreColumn, ColumnRanges>>;

function columnTally(row: ChartAccuracy, column: ScoreColumn): Tally {
  return column === "overall" ? row.overall : row.metrics[column];
}

function inRange(value: number | null, range: Range | undefined): boolean {
  if (!range) return true;
  return value !== null && value >= range.min && value <= range.max;
}

function passesFilters(row: ChartAccuracy, filters: Filters): boolean {
  for (const [column, f] of Object.entries(filters) as [ScoreColumn, Filters[ScoreColumn]][]) {
    if (!f) continue;
    if (!inRange(accuracyPercent(columnTally(row, column)), f.accuracy)) return false;
    const rates = column === "overall" ? undefined : rowRatesFor(row, column);
    for (const measure of RATE_MEASURES) {
      if (f[measure.key] && !inRange(rates ? measure.percent(rates) : null, f[measure.key])) return false;
    }
  }
  return true;
}

/** One 0–100 bar with a Min and a Max handle: two range inputs stacked on one
 * track, where only the handles take the pointer. */
/** A typed percent for one end of a range: applied on Enter or leaving the box, clamped to
 * 0-100; Escape or a non-number puts the current value back. */
function PercentBox({
  label,
  value,
  onCommit,
}: {
  label: string;
  value: number;
  onCommit: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  function commit() {
    const typed = Number(draft.trim().replace(/%$/, ""));
    if (draft.trim() === "" || Number.isNaN(typed)) {
      setDraft(String(value));
      return;
    }
    const next = Math.round(Math.min(100, Math.max(0, typed)));
    setDraft(String(next));
    if (next !== value) onCommit(next);
  }
  return (
    <label className="accuracy-range-box">
      <input
        type="text"
        inputMode="numeric"
        value={draft}
        aria-label={label}
        onFocus={(e) => e.target.select()}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") {
            e.stopPropagation();
            setDraft(String(value));
          }
        }}
      />
      %
    </label>
  );
}

function RangeSliders({
  label,
  range,
  onChange,
}: {
  label: string;
  range: Range;
  onChange: (range: Range) => void;
}) {
  return (
    <fieldset className="accuracy-filter-range">
      <legend>
        {/* The range only once it is narrowed: an untouched slider reads just its name. */}
        {label}
        {range.min > 0 || range.max < 100 ? (
          <>
            : <strong>{range.min}%–{range.max}%</strong>
          </>
        ) : null}
      </legend>
      <div className="accuracy-range">
        <div className="accuracy-range-track" aria-hidden="true">
          <div
            className="accuracy-range-fill"
            style={{ left: `${range.min}%`, right: `${100 - range.max}%` }}
          />
        </div>
        <input
          type="range"
          min={0}
          max={100}
          value={range.min}
          aria-label={`${label} minimum`}
          // When both handles meet at the top, the Min handle must stay grabbable.
          className={range.min > 50 ? "on-top" : undefined}
          onChange={(e) => onChange({ min: Math.min(Number(e.target.value), range.max), max: range.max })}
        />
        <input
          type="range"
          min={0}
          max={100}
          value={range.max}
          aria-label={`${label} maximum`}
          onChange={(e) => onChange({ min: range.min, max: Math.max(Number(e.target.value), range.min) })}
        />
      </div>
      {/* Or click and type: each box takes 0-100 and keeps Min at or under Max. */}
      <div className="accuracy-range-scale">
        <PercentBox
          label={`${label} minimum`}
          value={range.min}
          onCommit={(value) => onChange({ min: Math.min(value, range.max), max: range.max })}
        />
        <PercentBox
          label={`${label} maximum`}
          value={range.max}
          onCommit={(value) => onChange({ min: range.min, max: Math.max(value, range.min) })}
        />
      </div>
    </fieldset>
  );
}

const FULL_RANGE: Range = { min: 0, max: 100 };

/** The filter button at the right of a score column's header, and its slider popover. */
function ColumnFilter({
  label,
  value,
  classLabel,
  column,
  onChange,
}: {
  label: string;
  column: ScoreColumn;
  value: ColumnRanges | undefined;
  /** Set on a column with a Class Yes block ("Name & DOB = Yes"): also filter on its
   * Precision, Recall and F1. */
  classLabel?: string;
  onChange: (value: ColumnRanges | undefined) => void;
}) {
  const active = isActive(value);

  function set(part: Measure, range: Range) {
    const full = range.min === 0 && range.max === 100;
    const next: ColumnRanges = { ...value, [part]: full ? undefined : range };
    onChange(Object.values(next).some(Boolean) ? next : undefined);
  }

  return (
    <ColumnFilterButton label={label} active={active} icon={<Filter size={11} aria-hidden="true" />}>
      {() => (
        <>
          <RangeSliders label="Accuracy" range={value?.accuracy ?? FULL_RANGE} onChange={(r) => set("accuracy", r)} />
          {classLabel
            ? measuresFor(column).map((measure) => (
                <RangeSliders
                  key={measure.key}
                  label={`${measure.label} (${classLabel})`}
                  range={value?.[measure.key] ?? FULL_RANGE}
                  onChange={(r) => set(measure.key, r)}
                />
              ))
            : null}
          <button type="button" className="column-filter-clear" disabled={!active} onClick={() => onChange(undefined)}>
            Clear
          </button>
        </>
      )}
    </ColumnFilterButton>
  );
}

function isActive(value: ColumnRanges | undefined): boolean {
  return Boolean(value && Object.values(value).some(Boolean));
}

/** The filter button on the Last Run At header: keep files last run at or after a date and
 * time. Same button and popover as the score columns; the picker is History's. */
function RunAfterColumnFilter({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (next: string | null) => void;
}) {
  return (
    <ColumnFilterButton
      label="Last Run At"
      active={Boolean(value)}
      icon={<CalendarClock size={11} aria-hidden="true" />}
      width={288}
      align="start"
      popoverClassName="run-after-popover"
    >
      {(close) => (
        <RunAfterForm
          value={value}
          onApply={(next) => {
            onChange(next);
            close();
          }}
        />
      )}
    </ColumnFilterButton>
  );
}

/** A chart's value for one metadata filter. */
function metaValue(row: ChartAccuracy, key: MetaFilterKey): string | null | undefined {
  if (key === "scope") return row.projectScope;
  return key === "source" ? row.sourceName : key === "measure" ? row.measure : row.rrMethod;
}

function passesMeta(row: ChartAccuracy, filters: MetaFilters): boolean {
  return META_FILTERS.every(({ key }) => {
    const wanted = filters[key];
    if (!wanted?.length) return true;
    return wanted.includes(metaValue(row, key) || BLANK_META);
  });
}

/** Milliseconds for sorting; a missing date sorts last whichever way. */
function timeOf(value: string | null | undefined): number | null {
  const t = value ? new Date(value).getTime() : NaN;
  return Number.isNaN(t) ? null : t;
}

/** One file's Class 1 counts for a metric, where the metric has a Class 1. */
function rowRatesFor(row: ChartAccuracy, id: MetricId): ClassRates | undefined {
  if (id === "blankJunk") return row.blankJunkRates;
  if (id === "member") return row.memberRates;
  if (id === "orientation") return row.orientationRates;
  if (id === "signature") return row.signatureRates;
  if (id === "dos") return row.dosRates;
  return undefined;
}

/** The number a column sorts on: a time, a page count or an accuracy percent. */
function sortValue(row: ChartAccuracy, key: Exclude<SortKey, "name">): number | null {
  if (key === "run") return timeOf(row.lastUpdatedAt);
  if (key === "verified") return timeOf(row.lastVerifiedAt);
  if (key === "pages") return row.overall.scored;
  return accuracyPercent(columnTally(row, key));
}

/** A missing value sorts last whichever way; ties fall back to the chart name. */
function compareRows(a: ChartAccuracy, b: ChartAccuracy, key: SortKey, dir: SortDir): number {
  const sign = dir === "asc" ? 1 : -1;
  const byName = a.chartName.localeCompare(b.chartName, undefined, { numeric: true });
  if (key === "name") return sign * byName;
  const va = sortValue(a, key);
  const vb = sortValue(b, key);
  if (va === null || vb === null) {
    if (va === vb) return byName;
    return va === null ? 1 : -1;
  }
  return sign * (va - vb) || byName;
}

/** The last scored rows, kept while review-ui is open so reopening the page (or
 * Back from a chart) shows them at once while a fresh copy loads behind them. */
let lastRows: { source: string; rows: ChartAccuracy[] } | null = null;

/** The cache holds rows scored one way: the ground-truth source and Final only or not. */
function scoringKey(): string {
  return `${readGroundTruthSource()}|${scoreFinalOnly() ? "final" : "better"}`;
}

export default function AccuracyView({ onBack, onOpenChart, chartHref }: Props) {
  const cached = lastRows?.source === scoringKey() ? lastRows.rows : null;
  const [allRows, setRows] = useState<ChartAccuracy[]>(cached ?? []);
  // Nothing to show yet: the full Calculating panel. Cached rows on screen: a small note.
  const [loading, setLoading] = useState(cached === null);
  const [refreshing, setRefreshing] = useState(false);
  const [reloads, setReloads] = useState(0);
  const [error, setError] = useState<string | null>(null);
  // Search, column filters, sort and page survive opening a chart and coming Back.
  // Newest run first, like the home page.
  const [view, setView] = useViewState<{
    query: string;
    page: number;
    sortKey: SortKey;
    sortDir: SortDir;
    filters: Filters;
    /** ISO date-time (India offset): only files last run at or after it; null = all. */
    runAfter: string | null;
    /** Source / Measure / Retrieval. */
    meta: MetaFilters;
    runs: string[];
    batches: string[];
  }>("accuracy", {
    query: "", page: 1, sortKey: "run", sortDir: "desc", filters: {}, runAfter: null, meta: {}, runs: [], batches: [],
  });
  const { query, page, sortKey, sortDir, filters } = view;
  const runAfter = view.runAfter ?? null;
  const meta = useMemo(() => view.meta ?? {}, [view.meta]);
  const runs = useMemo(() => view.runs ?? [], [view.runs]);
  const batches = useMemo(() => view.batches ?? [], [view.batches]);
  // The chart-level filters (Run after, Run, Batch, Source, Measure, Retrieval).
  const rows = useMemo(
    () =>
      allRows.filter(
        (row) =>
          ranAfter(row.lastUpdatedAt, runAfter) &&
          passesMeta(row, meta) &&
          (!runs.length || (row.runId != null && runs.includes(row.runId))) &&
          (!batches.length || (row.batchId != null && batches.includes(row.batchId))),
      ),
    [allRows, runAfter, meta, runs, batches],
  );
  const byNumber = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true });
  const runOptions = useMemo(
    () => [...new Set(allRows.map((row) => row.runId).filter((v): v is string => Boolean(v)))].sort(byNumber),
    [allRows],
  );
  // Batches of the chosen run(s), or every batch when no run is chosen.
  const batchOptions = useMemo(
    () =>
      [
        ...new Set(
          allRows
            .filter((row) => !runs.length || (row.runId != null && runs.includes(row.runId)))
            .map((row) => row.batchId)
            .filter((v): v is string => Boolean(v)),
        ),
      ].sort(byNumber),
    [allRows, runs],
  );
  const metaOptions = useMemo(() => {
    const out = {} as Record<MetaFilterKey, string[]>;
    for (const { key } of META_FILTERS) {
      const values = new Set<string>();
      let blank = false;
      for (const row of allRows) {
        const value = metaValue(row, key);
        if (value) values.add(value);
        else blank = true;
      }
      // A chart with no value is offered as "(Blank)", last.
      out[key] = [...values].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
      if (blank) out[key].push(BLANK_META);
    }
    return out;
  }, [allRows]);
  const setQuery = (value: string) => setView({ query: value });
  const setPage = (value: number) => setView({ page: value });

  function setFilter(column: ScoreColumn, value: Filters[ScoreColumn]) {
    const next = { ...filters };
    if (value) next[column] = value;
    else delete next[column];
    setView({ filters: next, page: 1 });
  }

  // Same behaviour as the home page: click again to flip; name starts A→Z, numbers highest first.
  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setView({ sortDir: sortDir === "asc" ? "desc" : "asc", page: 1 });
    } else {
      setView({ sortKey: key, sortDir: key === "name" ? "asc" : "desc", page: 1 });
    }
  }

  function sortIndicator(key: SortKey): string {
    if (sortKey !== key) return "";
    return sortDir === "asc" ? " ↑" : " ↓";
  }

  function SortHeader({
    sort,
    label,
    filter,
    runFilter = false,
  }: {
    sort: SortKey;
    label: string;
    filter?: ScoreColumn;
    runFilter?: boolean;
  }) {
    const rates = filter && filter !== "overall" ? ratesFor(filter) : undefined;
    const filtered = (filter ? isActive(filters[filter]) : false) || (runFilter && Boolean(runAfter));
    return (
      <th
        scope="col"
        className={filtered ? "is-filtered" : undefined}
        aria-sort={sortKey === sort ? (sortDir === "asc" ? "ascending" : "descending") : "none"}
      >
        <span className="column-th">
          <button
            type="button"
            className={`th-sort${sortKey === sort ? " active" : ""}`}
            onClick={() => toggleSort(sort)}
          >
            {label}
            {sortIndicator(sort)}
          </button>
          {filter ? (
            <ColumnFilter
              label={label}
              column={filter}
              value={filters[filter]}
              classLabel={rates && filter !== "overall" ? ratesLabelFor(filter) : undefined}
              onChange={(value) => setFilter(filter, value)}
            />
          ) : null}
          {runFilter ? (
            <RunAfterColumnFilter value={runAfter} onChange={(next) => setView({ runAfter: next, page: 1 })} />
          ) : null}
        </span>
      </th>
    );
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const showing = lastRows?.source === scoringKey();
      if (showing) setRefreshing(true);
      else setLoading(true);
      setError(null);
      try {
        // Refresh (reloads > 0) skips the backend's minute-long reuse.
        const report = await getAccuracyReport(reloads > 0);
        if (cancelled) return;
        const scored: ChartAccuracy[] = [];
        for (const chart of report.charts) {
          if (!chart.document) continue;
          const row = scoreChart(chart.chartId, chart.chartName, chart.document);
          row.lastUpdatedAt = chart.lastUpdatedAt ?? null;
          row.lastVerifiedAt = chart.lastVerifiedAt ?? null;
          row.runId = chart.runId ?? null;
          row.batchId = chart.batchId ?? null;
          row.projectScope = chart.projectScope ?? null;
          row.sourceName = chart.sourceName ?? null;
          row.measure = chart.measure ?? null;
          row.rrMethod = chart.rrMethod ?? null;
          // Runs before MIN_LAST_RUN used older rules; leave them out.
          const ranAt = row.lastUpdatedAt ? new Date(row.lastUpdatedAt) : null;
          const recent = ranAt != null && !Number.isNaN(ranAt.getTime()) && ranAt >= new Date(`${MIN_LAST_RUN}T00:00:00+05:30`);
          if (row.overall.scored > 0 && recent) scored.push(row);
        }
        scored.sort((a, b) => a.chartName.localeCompare(b.chartName));
        if (!cancelled) {
          lastRows = { source: scoringKey(), rows: scored };
          setRows(scored);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Could not load accuracy");
      } finally {
        if (!cancelled) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloads]);

  // "By chart": search by file / chart name and the column filters, 10 per page. The
  // donuts stay on all files.
  const filterCount = Object.keys(filters).length;
  const allFilterCount =
    filterCount + (runAfter ? 1 : 0) + metaFilterCount(meta) + (runs.length ? 1 : 0) + (batches.length ? 1 : 0);
  const clearAll = () => setView({ filters: {}, runAfter: null, meta: {}, runs: [], batches: [], page: 1 });
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matched = rows.filter(
      (row) => (!needle || row.chartName.toLowerCase().includes(needle)) && passesFilters(row, filters),
    );
    return matched.sort((a, b) => compareRows(a, b, sortKey, sortDir));
  }, [rows, query, filters, sortKey, sortDir]);
  // The donuts score the charts the table lists: every filter and the search apply.
  const overallByMetric = useMemo(() => {
    const out = {} as Record<MetricId, Tally>;
    for (const metric of METRICS) {
      out[metric.id] = sumTallies(filtered.map((row) => row.metrics[metric.id]));
    }
    return out;
  }, [filtered]);

  const overall = useMemo(() => sumTallies(filtered.map((row) => row.overall)), [filtered]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / CHARTS_PER_PAGE));
  const currentPage = Math.min(page, totalPages);
  const pageRows = filtered.slice(
    (currentPage - 1) * CHARTS_PER_PAGE,
    currentPage * CHARTS_PER_PAGE,
  );
  const rangeStart = filtered.length === 0 ? 0 : (currentPage - 1) * CHARTS_PER_PAGE + 1;
  const rangeEnd = Math.min(currentPage * CHARTS_PER_PAGE, filtered.length);
  const pageNumbers = useMemo(() => {
    const maxButtons = 7;
    if (totalPages <= maxButtons) return Array.from({ length: totalPages }, (_, i) => i + 1);
    const pages = new Set<number>([1, totalPages, currentPage]);
    for (let d = 1; pages.size < maxButtons - 1; d++) {
      if (currentPage - d >= 1) pages.add(currentPage - d);
      if (currentPage + d <= totalPages) pages.add(currentPage + d);
    }
    return [...pages].sort((a, b) => a - b);
  }, [currentPage, totalPages]);

  const blankJunkRates = useMemo(
    () => sumRates(filtered.map((row) => row.blankJunkRates)),
    [filtered],
  );
  const memberRates = useMemo(() => sumRates(filtered.map((row) => row.memberRates)), [filtered]);
  const signatureRates = useMemo(
    () => sumRates(filtered.map((row) => row.signatureRates)),
    [filtered],
  );
  const dosRates = useMemo(() => sumRates(filtered.map((row) => row.dosRates)), [filtered]);
  const orientationRates = useMemo(
    () => sumRates(filtered.map((row) => row.orientationRates)),
    [filtered],
  );
  /** Class 1 counts across all files. */
  const ratesFor = (id: MetricId) =>
    id === "blankJunk"
      ? blankJunkRates
      : id === "member"
        ? memberRates
        : id === "orientation"
          ? orientationRates
          : id === "signature"
            ? signatureRates
            : id === "dos"
              ? dosRates
              : undefined;
  const ratesLabelFor = (id: MetricId) =>
    id === "member"
      ? "Name & DOB = Yes"
      : id === "orientation"
        ? "Rotated (not 0°) = Yes"
        : id === "signature"
          ? "Signature = Yes"
          : id === "dos"
            ? "Date = Yes"
            : "Blank/Junk = Yes";

  return (
    <div className="workspace accuracy-view">
      <div className="workspace-header">
        <div className="workspace-header-start">
          <button type="button" className="back-btn" onClick={onBack}>
            <ArrowLeft size={15} aria-hidden="true" />
            Back
          </button>
          <div className="workspace-title">
            <h1>Accuracy View</h1>
          </div>
        </div>
        {!loading ? (
          <div className="accuracy-refresh">
            {refreshing ? (
              <span className="accuracy-refresh-note">
                <Loader2 size={13} className="accuracy-calculating-spin" aria-hidden="true" />
                Updating…
              </span>
            ) : null}
            <button
              type="button"
              className="back-btn"
              disabled={refreshing}
              title="Recalculate from the latest pipeline results and ground truth"
              onClick={() => setReloads((n) => n + 1)}
            >
              <RefreshCw size={14} aria-hidden="true" />
              Refresh
            </button>
          </div>
        ) : null}
      </div>

      {error ? <div className="error-banner">{error}</div> : null}


      {loading ? (
        <CalculatingPanel />
      ) : allRows.length === 0 ? (
        <p className="accuracy-status">
          No pages with ground truth to score from runs on or after {MIN_LAST_RUN_LABEL}.
        </p>
      ) : (
        <>
          <section className="accuracy-section" aria-label="Accuracy by metric">
            <h2>By metric</h2>
            <p className="accuracy-compared">
              {runAfter ? <>Run after <strong>{fmtRunAfter(runAfter)}</strong> · </> : null}
              {allFilterCount || query.trim() ? <>Filtered · </> : null}
              Comparison: <strong>{filtered.length}</strong> {filtered.length === 1 ? "Chart" : "Charts"},{" "}
              <strong>{overall.scored}</strong> {overall.scored === 1 ? "Page" : "Pages"}
            </p>
            <p className="accuracy-note">
              Match counts as 1 and No Match as 0. Only Page Type has a Partial (0.5). Overall = pages with all seven streams matching (7/7).
            </p>
            <div className="accuracy-donuts">
              <OverallDonut tally={overall} />
              {METRICS.map((metric) =>
                metric.comingSoon ? (
                  <figure key={metric.id} className="accuracy-donut-card accuracy-coming-soon" aria-label={`${metric.label}: coming soon`}>
                    <svg className="accuracy-donut" viewBox="0 0 100 100" aria-hidden="true">
                      <circle className="accuracy-donut-track" cx="50" cy="50" r="36" />
                    </svg>
                    <figcaption>{metric.label}</figcaption>
                    <p>Coming soon</p>
                  </figure>
                ) : (
                <Donut
                  key={metric.id}
                  label={metric.label}
                  detail={metric.rule}
                  tally={overallByMetric[metric.id]}
                  rates={ratesFor(metric.id)}
                  ratesLabel={ratesLabelFor(metric.id)}
                  binary={metric.binary}
                />
                ),
              )}
            </div>
          </section>

          <section className="accuracy-section" aria-label="Accuracy by chart">
            <div className="accuracy-chart-head">
              {/* The count of charts the filters and search leave, as History shows its total. */}
              <div className="landing-history-title">
                <h2>By chart</h2>
                <span
                  className="landing-history-count"
                  title={allFilterCount || query.trim() ? `Charts matching the filters, of ${allRows.length}` : "Charts"}
                >
                  {filtered.length}
                </span>
              </div>
              {/* Search, then Run, Batch, Source, Measure, Retrieval beside it: they narrow
                  the table and the donuts above it. */}
              <div className="top-filter-bar accuracy-chart-filters" aria-label="Chart filters">
                <label className="landing-search accuracy-search">
                  <Search size={14} aria-hidden="true" />
                  <input
                    type="search"
                    value={query}
                    onChange={(e) => {
                      setQuery(e.target.value);
                      setPage(1);
                    }}
                    placeholder="Search file / chart…"
                    aria-label="Search files"
                  />
                </label>

                <TopCheckFilter
                  label="Run"
                  options={runOptions}
                  selected={runs}
                  formatOption={formatRunLabel}
                  onChange={(next) => setView({ runs: next, batches: next.length ? batches : [], page: 1 })}
                />
                <TopCheckFilter
                  label="Batch"
                  options={batchOptions}
                  selected={batches}
                  formatOption={formatBatchLabel}
                  onChange={(next) => setView({ batches: next, page: 1 })}
                />
                <MetaFilterBar options={metaOptions} filters={meta} onChange={(next) => setView({ meta: next, page: 1 })} />
                <ClearAllFilters count={allFilterCount} onClear={clearAll} />
              </div>
            </div>
            <div className="accuracy-table-scroll">
            <table className="landing-history-table accuracy-table">
              <thead>
                <tr>
                  {SortHeader({ sort: "name", label: "Chart" })}
                  {SortHeader({ sort: "run", label: "Last Run At", runFilter: true })}
                  {SortHeader({ sort: "verified", label: "Last Verified At" })}
                  {SortHeader({ sort: "pages", label: "Pages" })}
                  {LIVE_METRICS.map((metric) => (
                    <Fragment key={metric.id}>
                      {SortHeader({ sort: metric.id, label: metric.tableLabel ?? metric.label, filter: metric.id })}
                    </Fragment>
                  ))}
                  {SortHeader({ sort: "overall", label: "Overall", filter: "overall" })}
                </tr>
              </thead>
              <tbody>
                {pageRows.length === 0 ? (
                  <tr>
                    <td colSpan={LIVE_METRICS.length + 5} className="accuracy-empty">
                      {query.trim()
                        ? `No file matches “${query.trim()}”${filterCount ? " and the column filters" : ""}.`
                        : "No file matches the column filters."}
                    </td>
                  </tr>
                ) : null}
                {pageRows.map((row) => (
                  <tr key={row.chartId}>
                    <th scope="row">
                      <a
                        className="accuracy-chart-link"
                        href={chartHref(row.chartId)}
                        onClick={(event) => {
                          // Cmd / Ctrl / Shift / middle click keep the browser's new-tab behaviour.
                          if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
                          event.preventDefault();
                          saveAccuracyOrder(filtered.map((r) => ({ id: r.chartId, name: r.chartName })));
                          onOpenChart(row.chartId);
                        }}
                      >
                        {row.chartName}
                      </a>
                      <button
                        type="button"
                        className="accuracy-chart-newtab"
                        title="Open in new tab"
                        aria-label={`Open ${row.chartName} in a new tab`}
                        // window.open (not a target=_blank link) so the new tab keeps this tab's sign-in.
                        onClick={() => {
                          saveAccuracyOrder(filtered.map((r) => ({ id: r.chartId, name: r.chartName })));
                          window.open(chartHref(row.chartId), "_blank");
                        }}
                      >
                        <ExternalLink size={12} aria-hidden="true" />
                      </button>
                    </th>
                    <td className="landing-col-updated accuracy-col-date">
                      <DateLines iso={row.lastUpdatedAt} />
                    </td>
                    <td className="landing-col-updated accuracy-col-date">
                      <DateLines iso={row.lastVerifiedAt} />
                    </td>
                    <td className="landing-col-num">{row.overall.scored}</td>
                    {LIVE_METRICS.map((metric) => (
                      <td key={metric.id}>
                        <CompactScore
                          tally={row.metrics[metric.id]}
                          binary={metric.binary}
                          rates={rowRatesFor(row, metric.id)}
                          narrow={NARROW_METRICS.has(metric.id)}
                          wide={WIDE_METRICS.has(metric.id)}
                          member={MEMBER_METRICS.has(metric.id)}
                        />
                      </td>
                    ))}
                    <td>
                      <CompactScore tally={row.overall} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
            {filtered.length > 0 ? (
              <div className="landing-pagination" aria-label="Files pagination">
                <span className="landing-pagination-meta">
                  {rangeStart}–{rangeEnd} of {filtered.length}
                  {query.trim() || filterCount ? ` (of ${rows.length})` : ""}
                </span>
                <div className="landing-pagination-controls">
                  <button
                    type="button"
                    className="landing-page-btn"
                    disabled={currentPage <= 1}
                    onClick={() => setPage(Math.max(1, currentPage - 1))}
                    aria-label="Previous page"
                  >
                    <ChevronLeft size={14} />
                  </button>
                  {pageNumbers.map((n, idx) => {
                    const prev = pageNumbers[idx - 1];
                    return (
                      <span key={n} className="landing-page-num-wrap">
                        {prev != null && n - prev > 1 ? (
                          <span className="landing-page-ellipsis">…</span>
                        ) : null}
                        <button
                          type="button"
                          className={`landing-page-num${n === currentPage ? " active" : ""}`}
                          onClick={() => setPage(n)}
                          aria-label={`Page ${n}`}
                          aria-current={n === currentPage ? "page" : undefined}
                        >
                          {n}
                        </button>
                      </span>
                    );
                  })}
                  <button
                    type="button"
                    className="landing-page-btn"
                    disabled={currentPage >= totalPages}
                    onClick={() => setPage(Math.min(totalPages, currentPage + 1))}
                    aria-label="Next page"
                  >
                    <ChevronRight size={14} />
                  </button>
                </div>
              </div>
            ) : null}
          </section>
        </>
      )}
    </div>
  );
}
