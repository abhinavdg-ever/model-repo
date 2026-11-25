import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { formatIstPlain } from "./istTime";
import { createPortal } from "react-dom";
import {
  ArrowLeft,
  CalendarClock,
  ChevronLeft,
  Check,
  ChevronRight,
  Copy,
  Download,
  ExternalLink,
  Filter,
  Info,
  Loader2,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { CALIBRATED_FEATURES, STRICT_FEATURES, bandFor, chartScore, currentCalibration, fitKnots, levelLabel, type Level } from "./confidenceLevel";
import { downloadXlsx, formatBatchLabel, formatRunLabel, getAccuracyReport, saveCalibration } from "./api";
import { isScoringAdmin } from "./clientView";
import { saveAccuracyOrder } from "./chartSequence";
import { useScrollRestore, useViewState } from "./viewState";
import {
  ClearAllFilters,
  ColumnFilterButton,
  BLANK_META,
  CHART_META_FILTERS,
  MetaFilterBar,
  TopCheckFilter,
  metaFilterCount,
  ConfidenceFilterForm,
  NumberRangeForm,
  confidenceFilterOn,
  inNumberRange,
  passesConfidence,
  rangeOn,
  type ConfidenceFilter,
  type NumberRange,
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
  pageAccuracyCsv,
  pageAccuracyTable,
  chartAccuracyTable,
  scoreChart,
  duplicateScores,
  sumExtras,
  sumMemberMatrix,
  sumRates,
  type MemberMatrix,
  sumTallies,
  type ChartAccuracy,
  type ClassRates,
  type ExtraCounts,
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
  return formatIstPlain(iso);
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
/** ``match``: the [row, column] cells that are matches, when they are not the diagonal. */
type InfoTable = { caption: string; head: string[]; rows: (string | number)[][]; match?: [number, number][] };

type InfoSection = {
  title?: string;
  lines: string[];
  /** A raw-count table (the Stats cross matrix): a caption, column heads, rows. */
  tables?: InfoTable[];
};

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

// One width for every info box (2026-11-14, user): the Member stats tables fit without overflow.
const POP_WIDTH = 440;
const POP_MARGIN = 8;

/** ⓘ with a popover kept inside the window: fixed-position from the button, clamped
 * left and right, opening upwards when there is more room above, scrolling when long.
 * Rendered into document.body: .workspace's backdrop-filter would otherwise make
 * "fixed" relative to it, not to the window. */
/** A table as tab-separated text (caption, head, rows): pastes into Excel cells. */
function tableTsv(table: InfoTable): string {
  const cell = (value: string | number) => String(value).replace(/[\t\n\r]+/g, " ");
  const lines = [table.caption];
  if (table.head.length) lines.push(table.head.map(cell).join("\t"));
  for (const row of table.rows) lines.push(row.map(cell).join("\t"));
  return lines.join("\n");
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Older browsers / insecure origins: a hidden textarea and execCommand.
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

/** Copy a table (or all of them) for Excel (2026-11-14, user): just an icon, top right of
 * the table; a tick once copied. */
function CopyButton({ label, text }: { label: string; text: () => string }) {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle");
  return (
    <button
      type="button"
      className="accuracy-info-copy"
      onMouseDown={(event) => event.preventDefault()}
      onClick={async () => {
        setState((await copyText(text())) ? "done" : "failed");
        window.setTimeout(() => setState("idle"), 1500);
      }}
      title={state === "done" ? "Copied" : state === "failed" ? "Copy failed" : `${label}: paste into Excel`}
      aria-label={label}
    >
      {state === "done" ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
    </button>
  );
}

/** A rendered table as tab-separated text for Excel: the heading, then every row's cells. */
function domTableTsv(table: HTMLTableElement | null | undefined, heading: string): string {
  if (!table) return "";
  const cell = (el: Element) => (el as HTMLElement).innerText.replace(/[\t\n\r]+/g, " ").trim();
  const lines = heading ? [heading] : [];
  for (const row of Array.from(table.rows)) lines.push(Array.from(row.cells).map(cell).join("\t"));
  return lines.join("\n");
}

/** 2026-11-25 (user): copy the table of the section it sits in (Confidence Analysis). */
function CopySectionTable({ label = "Copy table" }: { label?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  return (
    <span ref={ref} className="confidence-copy">
      <CopyButton
        label={label}
        text={() => {
          const section = ref.current?.closest("section");
          const heading = (section?.querySelector("h3, h4") as HTMLElement | null)?.innerText.trim() ?? "";
          const group = (section?.closest(".confidence-group")?.querySelector(".confidence-group-title") as HTMLElement | null)
            ?.innerText.trim();
          return domTableTsv(section?.querySelector("table"), group ? `${group} · ${heading}` : heading);
        }}
      />
    </span>
  );
}

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
          {blocks.some((block) => block.tables?.length) ? (
            <div className="accuracy-info-copyall">
              <CopyButton
                label="Copy all tables"
                text={() => blocks.flatMap((block) => block.tables ?? []).map(tableTsv).join("\n\n")}
              />
            </div>
          ) : null}
          {blocks.map((block, index) => (
            <section key={index} className="accuracy-info-section">
              {block.title ? <h4>{block.title}</h4> : null}
              {block.lines.length > 1 ? (
                <ul>
                  {block.lines.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              ) : block.lines.length === 1 ? (
                <p>{block.lines[0]}</p>
              ) : null}
              {(block.tables ?? []).map((table) => (
                <div key={table.caption} className="accuracy-info-table">
                <CopyButton label="Copy table" text={() => tableTsv(table)} />
                <table
                  key={table.caption}
                  className={`accuracy-info-matrix${table.match ? " has-match" : table.head[0].startsWith("GT") ? " is-cross" : ""}`}
                >
                  <caption>{table.caption}</caption>
                  <thead>
                    <tr>
                      {table.head.map((cell, i) => (
                        <th key={i} scope="col">{cell}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {table.rows.map((row, r) => (
                      <tr key={r}>
                        {row.map((cell, c) =>
                          c === 0 ? (
                            <th key={c} scope="row">{cell}</th>
                          ) : (
                            <td key={c} className={table.match?.some(([mr, mc]) => mr === r && mc === c) ? "is-match" : undefined}>
                              {cell}
                            </td>
                          ),
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
                </div>
              ))}
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

const SCORE_INFO = "Accuracy = (Match + 0.5 × Partial) / scored pages. Only Page Type has a Partial.";
const OVERALL_SCORE_INFO = "Accuracy = pages with all 7 matching / scored pages.";
const RATES_INFO =
  "On the Yes pages only. Precision = of the pipeline's Yes, the share right. " +
  "Recall = of the real Yes pages, the share found. F1 = their balance.";

function Donut({
  label,
  detail,
  tally,
  rates,
  ratesLabel,
  binary = false,
  info,
}: {
  label: string;
  detail: string;
  /** Replaces the default info sections (Member, 2026-11-14). */
  info?: InfoSection[];
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
        text={info ?? [
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

const EXTRA_POP_WIDTH = 460;


/** Precision · Recall · F1 · Accuracy, then the 2 x 2 counts. */
/** Invoice / Duplicate (2026-11-14, user): only the pages either side calls Yes, so only
 * Precision and Recall (no Accuracy: the pages both call No are left out). */
function PrecisionRecallTable({ tp, fp, fn }: { tp: number; fp: number; fn: number }) {
  const ratio = (num: number, den: number) => (den > 0 ? `${Math.round((100 * num) / den)}%` : "—");
  const rows: [string, string | number][] = [
    ["Pages (Yes on either side)", tp + fp + fn],
    ["Both Yes", tp],
    ["Ground truth only", fn],
    ["Pipeline only", fp],
    [`Precision = Both / pipeline Yes (${tp} / ${tp + fp})`, ratio(tp, tp + fp)],
    [`Recall = Both / ground truth Yes (${tp} / ${tp + fn})`, ratio(tp, tp + fn)],
  ];
  return (
    <table className="accuracy-extra-matrix accuracy-extra-pr">
      <tbody>
        {rows.map(([label, value]) => (
          <tr key={label}>
            <th scope="row">{label}</th>
            <td>{typeof value === "number" ? value.toLocaleString() : value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** "Additional Metrics": a link at the top right of By metric (above Orientation) opening
 * Visibility Match % and Invoice Accuracy for the charts shown. Not part of the seven streams. */
function AdditionalMetrics({ extra }: { extra: ExtraCounts }) {
  const [place, setPlace] = useState<{ top: number; right: number } | null>(null);
  const linkRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!place) return;
    const close = (event: Event) => {
      const target = event.target;
      if (target instanceof Node && (popRef.current?.contains(target) || linkRef.current?.contains(target))) return;
      setPlace(null);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPlace(null);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
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
    const rect = linkRef.current?.getBoundingClientRect();
    if (!rect) return;
    setPlace({ top: rect.bottom + 6, right: Math.max(POP_MARGIN, window.innerWidth - rect.right) });
  }

  const { invoice } = extra;
  const dup = duplicateScores(extra.duplicate);
  return (
    <>
      <button
        ref={linkRef}
        type="button"
        className="accuracy-extra-link"
        aria-expanded={place !== null}
        onClick={toggle}
      >
        Additional Metrics
      </button>
      {place
        ? createPortal(
            <div
              ref={popRef}
              className="accuracy-info-pop accuracy-extra-pop"
              role="dialog"
              aria-label="Additional Metrics"
              style={{ top: place.top, right: place.right, width: Math.min(EXTRA_POP_WIDTH, window.innerWidth - 2 * POP_MARGIN) }}
            >
              <section className="accuracy-info-section">
                <h4>Invoice</h4>
                <PrecisionRecallTable tp={invoice.both} fn={invoice.gtOnly} fp={invoice.algoOnly} />
                <p className="accuracy-extra-rule">
                  Pages where the ground truth or the pipeline says Invoice. Ground truth: Is Invoice = Yes or 1, or
                  Invoice as its Page Type (an invoice is a Main page, not Junk). Pipeline: its Final Page Type is Invoice (Invoice Page).
                </p>
              </section>
              <section className="accuracy-info-section">
                <h4>Duplicate</h4>
                <PrecisionRecallTable tp={dup.tp} fn={dup.fn} fp={dup.fp} />
                <p className="accuracy-extra-rule">
                  Pages where the ground truth or the pipeline says Duplicate = Yes, on pages with a
                  ground-truth Is Duplicate. Partial counts as No on both sides.
                </p>
              </section>
            </div>,
            document.body,
          )
        : null}
    </>
  );
}

const OVERALL_RULE =
  "Per page: how many of the 7 streams match (Member, DOS, Page Type, Signature, Codability, Blank / Junk, Orientation). " +
  "Accuracy = pages with 7 of 7. Good = 5 to 7, Fair = 3 to 4, Bad = 0 to 2.";

const MEMBER_SIDES = [["yes", "Yes"], ["no", "No"], ["not_found", "Not Found"]] as const;

function share(num: number, den: number): string {
  return den > 0 ? `${num} / ${den} = ${Math.round((100 * num) / den)}%` : `${num} / 0 = —`;
}

/** Member's info (2026-11-14, user): Match rules, Page matches, Accuracy & precision
 * calculation with the real numbers, and Stats as ground truth x pipeline counts. */
/** The Yes universe (2026-11-14, user): the cases either side calls Yes, as a 2 x 2 cross tab
 * without the No / No cell; its accuracy, precision and recall on that denominator. */
function yesSection(classLabel: string, rates: ClassRates): InfoSection {
  const { tp, fp, fn } = rates;
  const universe = tp + fp + fn;
  return {
    title: `Yes cases only (${classLabel}; denominator = Yes on either side, ${universe})`,
    lines: [
      `Yes accuracy = Both Yes / Yes on either side = ${share(tp, universe)}`,
      `Yes precision = Both Yes / pipeline Yes = ${share(tp, tp + fp)}`,
      `Yes recall = Both Yes / ground truth Yes = ${share(tp, tp + fn)}`,
    ],
    tables: [{
      caption: "GT x Pipeline, Yes on either side (No / No left out)",
      head: ["GT \\ Pipeline", "Yes", "No", "Total"],
      rows: [
        ["Yes", tp, fn, tp + fn],
        ["No", fp, "—", fp],
        ["Total", tp + fp, fn, universe],
      ],
      match: [[0, 1]],
    }],
  };
}

/** A 2 x 2 cross tab from Class 1 counts: ground truth Yes / No down, pipeline Yes / No across. */
function classTable(caption: string, rates: ClassRates) {
  const { tp, fp, fn, tn } = rates;
  return {
    caption,
    head: ["GT \\ Pipeline", "Yes", "No", "Total"],
    rows: withTotals([
      ["Yes", tp, fn],
      ["No", fp, tn],
    ]),
  };
}

/** Each row's total on the right, and a Total row under it: the pipeline's column totals
 * (2026-11-14, user). */
function withTotals(rows: (string | number)[][]): (string | number)[][] {
  const sum = (cells: (string | number)[]) => cells.reduce<number>((a, b) => a + (typeof b === "number" ? b : 0), 0);
  const body = rows.map((row) => [...row, sum(row.slice(1))]);
  const width = body[0]?.length ?? 0;
  const totals: (string | number)[] = ["Total"];
  for (let c = 1; c < width; c += 1) totals.push(body.reduce((a, row) => a + Number(row[c] ?? 0), 0));
  return [...body, totals];
}

function pageMatchLines(tally: Tally, binary: boolean): string[] {
  const parts = [`Match ${tally.yes}`, ...(binary ? [] : [`Partial ${tally.maybe}`]), `No Match ${tally.no}`, `Scored ${tally.scored}`];
  return [parts.join(" · "), ...(tally.na ? [`Not available (no label): ${tally.na}`] : [])];
}

function rateLines(rates: ClassRates, classLabel: string): string[] {
  const { tp, fp, fn, tn } = rates;
  const precision = tp + fp ? tp / (tp + fp) : null;
  const recall = tp + fn ? tp / (tp + fn) : null;
  const f1 = precision != null && recall != null && precision + recall > 0
    ? `${Math.round((200 * precision * recall) / (precision + recall))}%` : "—";
  const lines = [
    `Yes accuracy = TP / (TP + FP + FN), only the cases either side calls Yes = ${share(tp, tp + fp + fn)}`,
    `Precision = TP / (TP + FP) = ${share(tp, tp + fp)}`,
    `Recall = TP / (TP + FN) = ${share(tp, tp + fn)}`,
    `F1 = ${f1} (${classLabel}: TP ${tp} · FP ${fp} · FN ${fn} · TN ${tn})`,
  ];
  if (rates.credCases != null) {
    lines.push(`Cred. M = matching credentials / pages with credentials = ${share(rates.credMatch ?? 0, rates.credCases)}`);
  }
  return lines;
}

/** Every other metric's info (2026-11-14, user): the same four sections as Member. */
function metricInfo(rule: string, tally: Tally, binary: boolean, rates?: ClassRates, classLabel = "",
                    extraTables: InfoTable[] = []): InfoSection[] {
  const accuracy = binary
    ? `Accuracy = Match / Scored = ${share(tally.yes, tally.scored)}`
    : `Accuracy = (Match + 0.5 × Partial) / Scored = (${tally.yes} + 0.5 × ${tally.maybe}) / ${tally.scored}` +
      (tally.scored ? ` = ${Math.round((100 * (tally.yes + 0.5 * tally.maybe)) / tally.scored)}%` : "");
  return [
    { title: "Match rules", lines: sentences(rule) },
    { title: "Page matches", lines: pageMatchLines(tally, binary) },
    { title: "Accuracy & precision calculation", lines: [accuracy, ...(rates ? rateLines(rates, classLabel) : [])] },
    ...(rates || extraTables.length
      ? [{
          title: "Stats (raw counts; the diagonal matches)",
          lines: [],
          tables: [...extraTables, ...(rates ? [classTable(classLabel, rates)] : [])],
        }]
      : []),
    ...(rates ? [yesSection(classLabel, rates)] : []),
  ];
}

/** Provider Signature (2026-11-14, user): Credentials match on the pages whose ground truth has
 * credentials (any one shared is a match), the last section, one row. */
function credentialSection(rates: ClassRates): InfoSection {
  const cases = rates.credCases ?? 0;
  const match = rates.credMatch ?? 0;
  return {
    title: "Credentials match (pages where the ground truth has credentials)",
    lines: [],
    tables: [{
      caption: "Any one credential shared is a Match",
      head: ["", "Match", "No Match", "Total"],
      rows: [["Pages", match, cases - match, cases]],
      match: [[0, 1]],
    }],
  };
}

/** Codability Stats (2026-11-14, user): ground truth Codable / Non-Codable x pipeline. */
function codableTables(matrix: MemberMatrix) {
  const at = (gt: string, pipe: string) => matrix.cells[`cod|${gt}|${pipe}`] ?? 0;
  const sides = [["codable", "Codable"], ["non_codable", "Non-Codable"], ["discharge", "Discharge"]] as const;
  return [{
    caption: "Codability (Blank / Junk = Non-Codable; Discharge shown, not scored)",
    head: ["GT \\ Pipeline", "Codable", "Non-Codable", "Discharge", "Total"],
    rows: withTotals(sides.map(([gt, label]) => [
      label, at(gt, "codable"), at(gt, "non_codable"), at(gt, "discharge"),
    ])),
  }];
}

/** DOS Yes (2026-11-14, user): Yes = a date on at least one side (both Not Found left out),
 * counted once per page. */
function dosYesSection(matrix: MemberMatrix): InfoSection {
  const n = (k: string) => matrix.cells[`page|${k}`] ?? 0;
  const match = n("match"), wrong = n("wrong"), gtOnly = n("gt_only"), pipeOnly = n("pipe_only");
  const universe = match + wrong + gtOnly + pipeOnly;
  return {
    title: `Yes cases only (a date on either side; denominator ${universe} pages)`,
    lines: [
      `Yes accuracy = right date / dated on either side = ${share(match, universe)}`,
      `Yes precision = right date / pipeline dated = ${share(match, match + wrong + pipeOnly)}`,
      `Yes recall = right date / ground truth dated = ${share(match, match + wrong + gtOnly)}`,
    ],
    tables: [{
      caption: "GT x Pipeline, a date on either side (both Not Found left out)",
      head: ["GT \\ Pipeline", "Same date", "Other date", "Not Found", "Total"],
      rows: [
        ["Date", match, wrong, gtOnly, match + wrong + gtOnly],
        ["Not Found", "—", pipeOnly, "—", pipeOnly],
        ["Total", match, wrong + pipeOnly, gtOnly, universe],
      ],
      match: [[0, 1]],
    }],
  };
}

/** DOS Stats (2026-11-14, user): From / To as Match, No Match, Not Found; then Both. */
function dosTables(matrix: MemberMatrix): InfoTable[] {
  const at = (key: string) => matrix.cells[key] ?? 0;
  const both = at("both|2");
  const notBoth = at("both|1") + at("both|0");
  // 2026-11-14 (user): From Match, To Match and Both Match in one table; Not Found = Not Found
  // counts as a Match.
  return [{
    caption: "DOS (Not Found = Not Found is a Match)",
    head: ["", "Match", "No Match", "Total"],
    rows: [
      ["From", at("from|match"), at("from|no_match"), at("from|match") + at("from|no_match")],
      ["To", at("to|match"), at("to|no_match"), at("to|match") + at("to|no_match")],
      ["Both (page)", both, notBoth, both + notBoth],
    ],
    match: [[0, 1], [1, 1], [2, 1]],
  }];
}

/** Both (2026-11-14, user): one row -- pages where both fields match, one does, neither. */
function bothTable(matrix: MemberMatrix, caption = "Both (Name & DOB, pages with both labelled)") {
  const n = (k: number) => matrix.cells[`both|${k}`] ?? 0;
  return {
    caption,
    head: ["", "Both Match", "One Match", "No Match", "Total"],
    rows: [["Pages", n(2), n(1), n(0), n(2) + n(1) + n(0)]] as (string | number)[][],
  };
}

function memberInfo(rule: string, tally: Tally, rates: ClassRates, matrix: MemberMatrix): InfoSection[] {
  const { tp, fp, fn, tn } = rates;
  const precision = tp + fp ? tp / (tp + fp) : null;
  const recall = tp + fn ? tp / (tp + fn) : null;
  const f1 = precision != null && recall != null && precision + recall > 0
    ? `${Math.round((200 * precision * recall) / (precision + recall))}%` : "—";
  const table = (field: "name" | "dob", caption: string) => {
    const at = (gt: string, pipe: string) => matrix.cells[`${field}|${gt}|${pipe}`] ?? 0;
    return {
      caption,
      head: ["GT \\ Pipeline", ...MEMBER_SIDES.map(([, l]) => l), "Total"],
      rows: withTotals(MEMBER_SIDES.map(([gt, l]) => [l, ...MEMBER_SIDES.map(([pipe]) => at(gt, pipe))])),
    };
  };
  return [
    { title: "Match rules", lines: sentences(rule) },
    {
      title: "Page matches",
      lines: [
        `Match ${tally.yes} · No Match ${tally.no} · Scored ${tally.scored}`,
        `Not scored: ${matrix.excluded} page${matrix.excluded === 1 ? "" : "s"} both call Blank / Junk, or a duplicate`,
      ],
    },
    {
      title: "Accuracy & precision calculation",
      lines: [
        `Accuracy = Match / Scored = ${share(tally.yes, tally.scored)}`,
        `Yes accuracy = TP / (TP + FP + FN), only the cases either side calls Yes = ${share(tp, tp + fp + fn)}`,
        `Precision = TP / (TP + FP) = ${share(tp, tp + fp)}`,
        `Recall = TP / (TP + FN) = ${share(tp, tp + fn)}`,
        `F1 = ${f1} (Yes = both Name and DOB found; TP ${tp} · FP ${fp} · FN ${fn} · TN ${tn})`,
      ],
    },
    {
      title: "Stats (raw counts; the diagonal matches)",
      lines: [],
      tables: [
        table("name", "Member Name"),
        table("dob", "Member DOB"),
        bothTable(matrix),
      ],
    },
    yesSection("Name & DOB = Yes", rates),
  ];
}

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

const CONF_LEVELS: Level[] = ["high", "medium", "low"];
/** Each accuracy metric's confidence feature (confidenceLevel.ts CONFIDENCE_FIELDS). */
const METRIC_FEATURE: Record<MetricId, string> = {
  member: "member", dos: "dos", blankJunk: "blank_junk", pageType: "page_type",
  codeable: "codability", signature: "provider", orientation: "orientation",
};

type LevelScore = { pages: number; yes: number; maybe: number; no: number };
const emptyScore = (): LevelScore => ({ pages: 0, yes: 0, maybe: 0, no: 0 });
function addVerdict(score: LevelScore, verdict: string | null | undefined): void {
  if (verdict !== "yes" && verdict !== "maybe" && verdict !== "no") return;
  score.pages += 1;
  score[verdict] += 1;
}
function scorePct(score: LevelScore): string {
  return score.pages ? `${Math.round((100 * (score.yes + 0.5 * score.maybe)) / score.pages)}%` : "—";
}

/** Confidence (2026-11-19, user), beside Additional Metrics: over the filtered charts' scored
 * pages, how confidence is spread (overall and per metric), the cross tab of page confidence
 * against the page's overall match, and each metric's accuracy at each of its confidence levels
 * -- High should be the most accurate. */
/** The Codability tab's i (2026-11-25, user; was the Agreement tab): Page Type and Codability
 * accuracy per model / keyword agreement tier. */
function agreementInfo(agreement: Map<string, { pageType: LevelScore; codable: LevelScore; conf: number; confN: number }>): InfoSection[] {
  return [{
    title: "Accuracy by model and keyword agreement",
    lines: [
      "A: the model's and the keywords' first choice are both the page type.",
      "B: model first, keywords 2nd-3rd. C: model first only.",
      "D: model 2nd-3rd, or same family. E: they disagree on the family.",
      "Accuracy should fall from A to E.",
    ],
    tables: [{
      caption: "Agreement",
      head: ["Tier", "Pages", "Page Type Accuracy", "Codability Accuracy", "Avg Page Type Confidence"],
      rows: AGREEMENT_TIERS.map((t) => {
        const e = agreement.get(t)!;
        return [t, e.pageType.pages.toLocaleString(), scorePct(e.pageType), scorePct(e.codable),
                e.confN ? `${Math.round((100 * e.conf) / e.confN)}%` : "—"];
      }),
    }],
  }];
}

/** The page classifier's model / keyword agreement tiers, best first (2026-11-24). */
const AGREEMENT_TIERS = ["A", "B", "C", "D", "E"] as const;

/** Confidence bands, top first: [label, lowest value] (2026-11-22, user): steps of 5 above 70%,
 * of 10 from 30% to 70%, one band under 30%. */
const BANDS: [string, number][] = [
  ...[95, 90, 85, 80, 75, 70].map((lo) => [`${lo}-${lo + 5}%`, lo / 100] as [string, number]),
  ...[60, 50, 40, 30].map((lo) => [`${lo}-${lo + 10}%`, lo / 100] as [string, number]),
  ["<30%", 0],
];

/** 2026-11-23 (user): Orientation -- 1-point bands from 95. */
const STRICT_BANDS: [string, number][] = [
  ...[99, 98, 97, 96, 95].map((lo) => [`${lo}-${lo + 1}%`, lo / 100] as [string, number]),
  ...BANDS.slice(1),
];

/** 2026-11-24 (user): Blank / Junk and Provider Signature -- 99-100% (High), 95-99% as one band. */
const TOP_BANDS: [string, number][] = [["99-100%", 0.99], ["95-99%", 0.95], ...BANDS.slice(1)];
const TOP_FEATURES = new Set(["blank_junk", "provider"]);

/** The band a confidence falls in (index into ``bands``, BANDS by default). */
function bandIndex(value: number, bands: [string, number][] = BANDS): number {
  const i = bands.findIndex(([, lo]) => value >= lo);
  return i < 0 ? bands.length - 1 : i;
}

/** A metric's bands and the level of a band's lower edge (its row colour). */
function metricBands(id: MetricId): [string, number][] {
  const feature = METRIC_FEATURE[id];
  return TOP_FEATURES.has(feature) ? TOP_BANDS : STRICT_FEATURES.has(feature) ? STRICT_BANDS : BANDS;
}

/** 2026-11-24 (user): fit Page Type / Codability's confidence curves on these pages' raw
 * confidences and verdicts (yes 1, partial 0.5, no 0). feature -> {knots, pages}; a feature with
 * under 30 scored pages is left out. */
function fitCalibration(rows: ChartAccuracy[]): Record<string, { knots: number[][]; pages: number }> {
  const out: Record<string, { knots: number[][]; pages: number }> = {};
  for (const feature of CALIBRATED_FEATURES) {
    const metric = (Object.keys(METRIC_FEATURE) as MetricId[]).find((id) => METRIC_FEATURE[id] === feature);
    if (!metric) continue;
    const points: [number, number][] = [];
    for (const row of rows) {
      for (const page of row.pages) {
        const verdict = page.scores[metric];
        const raw = page.rawFeatures?.[feature];
        if (raw == null || (verdict !== "yes" && verdict !== "maybe" && verdict !== "no")) continue;
        points.push([raw, verdict === "yes" ? 1 : verdict === "maybe" ? 0.5 : 0]);
      }
    }
    const knots = fitKnots(points);
    if (knots) out[feature] = { knots, pages: points.length };
  }
  return out;
}

/** The pages the Accuracy view lists now (every filter and the search), for the profile menu's
 * Calibrate (2026-11-25, user: moved off the Confidence Analysis into the menu). */
let calibrationRows: ChartAccuracy[] = [];

export function calibrationStatus(): string {
  const on = CALIBRATED_FEATURES.filter((f) => currentCalibration()[f]);
  return on.length ? on.map((f) => (f === "page_type" ? "Page Type" : "Codability")).join(", ") : "off (raw confidence)";
}

export function isCalibrated(): boolean {
  return CALIBRATED_FEATURES.some((f) => currentCalibration()[f]);
}

/** Fit (or, ``clear``, remove) the Page Type / Codability curves on the listed pages and save
 * them; the page reloads on success. Resolves to an error message, or null. */
export async function runCalibration(clear: boolean): Promise<string | null> {
  if (!isScoringAdmin()) return "Admins only.";
  const features = clear
    ? Object.fromEntries(CALIBRATED_FEATURES.map((f) => [f, { knots: [], pages: 0 }]))
    : fitCalibration(calibrationRows);
  if (!Object.keys(features).length) return "Not enough scored pages (30+ per metric) to calibrate.";
  try {
    await saveCalibration(features);
    window.location.reload();
    return null;
  } catch (err: unknown) {
    return `Could not save: ${err instanceof Error ? err.message : String(err)}`;
  }
}

function ConfidenceMetrics({ rows }: { rows: ChartAccuracy[] }) {
  const [open, setOpen] = useState(false);
  // 2026-11-19 (user): Overview, then one tab per metric with its accuracy by confidence decile.
  const [tab, setTab] = useState<"overview" | MetricId>("overview");
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const data = useMemo(() => {
    if (!open) return null;
    const pageLevels: Record<Level, number> = { high: 0, medium: 0, low: 0 };
    const scored: { level: Level; critical: boolean }[] = [];
    // 2026-11-23 (user): pages by their own confidence band, against Match = every metric matching.
    const pageBands: LevelScore[] = Array.from({ length: BANDS.length }, emptyScore);
    // 2026-11-23 (user): per metric, its accuracy on the pages of each PAGE confidence band.
    const pageMetricBands = new Map<MetricId, LevelScore[]>();
    for (const metric of LIVE_METRICS) pageMetricBands.set(metric.id, Array.from({ length: BANDS.length }, emptyScore));
    // 2026-11-25 (user): the same two tables by Page Score (the average of the page's metrics).
    const scoreBands: LevelScore[] = Array.from({ length: BANDS.length }, emptyScore);
    // 2026-11-25 (user): per band, pages by how many streams match (0/7 .. 7/7).
    const streamRow = () => new Array<number>(OVERALL_STREAMS + 1).fill(0);
    const pageStreams = Array.from({ length: BANDS.length }, streamRow);
    const scoreStreams = Array.from({ length: BANDS.length }, streamRow);
    const scoreMetricBands = new Map<MetricId, LevelScore[]>();
    for (const metric of LIVE_METRICS) scoreMetricBands.set(metric.id, Array.from({ length: BANDS.length }, emptyScore));
    const perFeature = new Map<string, Record<Level, number>>();
    const cross: Record<Level, LevelScore> = { high: emptyScore(), medium: emptyScore(), low: emptyScore() };
    const byMetric = new Map<MetricId, Record<Level, LevelScore>>();
    for (const metric of LIVE_METRICS) byMetric.set(metric.id, { high: emptyScore(), medium: emptyScore(), low: emptyScore() });
    // Per metric, its pages by their own confidence band (BANDS, top first).
    const deciles = new Map<MetricId, LevelScore[]>();
    for (const metric of LIVE_METRICS) deciles.set(metric.id, Array.from({ length: metricBands(metric.id).length }, emptyScore));
    // 2026-11-23 (user): charts by their own confidence band; Match = a page with every metric matching.
    const chartBands = Array.from({ length: BANDS.length }, () => ({ charts: 0, pages: 0, yes: 0, streams: new Array<number>(OVERALL_STREAMS + 1).fill(0) }));
    // 2026-11-25 (user): per metric, its accuracy on the pages of each CHART confidence band.
    const chartMetricBands = new Map<MetricId, LevelScore[]>();
    for (const metric of LIVE_METRICS) chartMetricBands.set(metric.id, Array.from({ length: BANDS.length }, emptyScore));
    const chartValues: number[] = [];
    for (const row of rows) {
      const cv = row.confidence?.value;
      if (cv != null) {
        chartValues.push(cv);
        const cb = chartBands[bandIndex(cv)];
        cb.charts += 1;
        for (const page of row.pages) {
          for (const metric of LIVE_METRICS) {
            const verdict = page.scores[metric.id];
            if (verdict != null) addVerdict(chartMetricBands.get(metric.id)![bandIndex(cv)], verdict);
          }
          if (!page.overall) continue;
          cb.pages += 1;
          if (page.overall === "yes") cb.yes += 1;
          if (page.matches != null) cb.streams[page.matches] += 1;
        }
      }
      for (const page of row.pages) {
        const pc = page.confidence;
        if (pc?.level && pc.value != null) {
          pageLevels[pc.level] += 1;
          scored.push({ level: pc.level, critical: Boolean(page.critical) });
          if (page.overall) addVerdict(pageBands[bandIndex(pc.value)], page.overall);
          if (page.overall && page.matches != null) pageStreams[bandIndex(pc.value)][page.matches] += 1;
          for (const metric of LIVE_METRICS) {
            const verdict = page.scores[metric.id];
            if (verdict != null) addVerdict(pageMetricBands.get(metric.id)![bandIndex(pc.value)], verdict);
          }
          addVerdict(cross[pc.level], page.overall);
        }
        if (pc?.score != null) {
          const i = bandIndex(pc.score);
          if (page.overall) addVerdict(scoreBands[i], page.overall);
          if (page.overall && page.matches != null) scoreStreams[i][page.matches] += 1;
          for (const metric of LIVE_METRICS) {
            const verdict = page.scores[metric.id];
            if (verdict != null) addVerdict(scoreMetricBands.get(metric.id)![i], verdict);
          }
        }
        // Each metric on every page the accuracy scores it (2026-11-19, user): Blank / Junk
        // pages included, with that metric's own confidence.
        for (const metric of LIVE_METRICS) {
          const verdict = page.scores[metric.id];
          const key = METRIC_FEATURE[metric.id];
          const value = page.features?.[key];
          if (verdict == null || value == null) continue;
          const level = bandFor(key, value);
          const counts = perFeature.get(key) ?? { high: 0, medium: 0, low: 0 };
          counts[level] += 1;
          perFeature.set(key, counts);
          addVerdict(byMetric.get(metric.id)![level], verdict);
          addVerdict(deciles.get(metric.id)![bandIndex(value, metricBands(metric.id))], verdict);
        }
      }
    }
    const total = pageLevels.high + pageLevels.medium + pageLevels.low;
    // 2026-11-23 (user): weighted page levels (High 1, Medium 0.6, Low 0; critical pages x2), banded.
    const score = chartScore(scored);
    // Every metric pooled, per level (each metric on each page it scores counts once).
    const pooled: Record<Level, LevelScore> = { high: emptyScore(), medium: emptyScore(), low: emptyScore() };
    for (const by of byMetric.values()) {
      for (const l of CONF_LEVELS) {
        pooled[l].pages += by[l].pages; pooled[l].yes += by[l].yes; pooled[l].maybe += by[l].maybe; pooled[l].no += by[l].no;
      }
    }
    // 2026-11-24 (user): Page Type and Codability accuracy per model / keyword agreement tier.
    const agreement = new Map<string, { pageType: LevelScore; codable: LevelScore; conf: number; confN: number }>();
    for (const t of AGREEMENT_TIERS) agreement.set(t, { pageType: emptyScore(), codable: emptyScore(), conf: 0, confN: 0 });
    for (const row of rows) {
      for (const page of row.pages) {
        const entry = page.tier ? agreement.get(page.tier) : undefined;
        if (!entry) continue;
        addVerdict(entry.pageType, page.scores.pageType);
        addVerdict(entry.codable, page.scores.codeable);
        const value = page.features?.page_type;
        if (value != null && page.scores.pageType != null) {
          entry.conf += value;
          entry.confN += 1;
        }
      }
    }
    const chartMean = chartValues.length ? Math.round((100 * chartValues.reduce((s, x) => s + x, 0)) / chartValues.length) : null;
    return { pageLevels, total, level: score?.level ?? null, reason: score?.reason ?? null, pageBands, pageMetricBands, scoreBands, scoreMetricBands, pageStreams, scoreStreams, chartBands, chartMetricBands, chartMean,
             mean: score ? Math.round(100 * score.value) : null, perFeature, cross, byMetric, pooled, deciles, agreement };
  }, [open, rows]);

  return (
    <>
      <button type="button" className="accuracy-extra-link" aria-expanded={open} onClick={() => setOpen(true)}>
        Confidence Analysis
      </button>
      {open && data
        ? createPortal(
            <div className="modal-backdrop" role="presentation" onClick={() => setOpen(false)}>
              <div className="modal-card chart-details-card confidence-metrics-card" role="dialog" aria-modal="true"
                   aria-labelledby="confidence-metrics-title" onClick={(event) => event.stopPropagation()}>
                <div className="modal-card-header">
                  <h2 id="confidence-metrics-title">
                    Confidence Analysis · {data.chartMean == null ? "—" : `${data.chartMean}%`}
                  </h2>
                  <button type="button" className="modal-close" onClick={() => setOpen(false)} aria-label="Close">
                    <X size={16} />
                  </button>
                </div>
                <p className="chart-details-note">
                  Average chart confidence · {rows.length.toLocaleString()} charts · {data.total.toLocaleString()} pages
                </p>
                <div className="output-tabs confidence-tabs" role="tablist" aria-label="Confidence Analysis views">
                  {(["overview", ...LIVE_METRICS.map((m) => m.id)] as ("overview" | MetricId)[]).map((id) => (
                    <button key={id} type="button" role="tab" aria-selected={tab === id}
                            className={tab === id ? "active" : undefined} onClick={() => setTab(id)}>
                      {id === "overview" ? "Overview" : (LIVE_METRICS.find((m) => m.id === id)?.tableLabel
                        ?? LIVE_METRICS.find((m) => m.id === id)?.label)}
                    </button>
                  ))}
                </div>
                {tab !== "overview" ? (() => {
                  const metric = LIVE_METRICS.find((m) => m.id === tab)!;
                  const buckets = data.deciles.get(metric.id)!;
                  const partial = buckets.some((b) => b.maybe > 0);
                  const all = buckets.reduce((t, b) => ({ pages: t.pages + b.pages, yes: t.yes + b.yes, maybe: t.maybe + b.maybe, no: t.no + b.no }), emptyScore());
                  return (
                    <section>
                      <h3>
                        {metric.label}: accuracy by confidence band
                        {/* 2026-11-25 (user): the model / keyword agreement as an i on Codability. */}
                        {metric.id === "codeable" ? <InfoTip text={agreementInfo(data.agreement)} /> : null}
                        <CopySectionTable />
                      </h3>
                      <p className="chart-details-note">
                        Pages with ground truth, grouped by this metric's own confidence. Accuracy should rise with
                        confidence. No confidence is below 20%.
                      </p>
                      <table className="chart-details-table">
                        <thead>
                          <tr>
                            <th>Confidence</th><th className="num">Pages</th><th className="num">Match</th>
                            {partial ? <th className="num">Partial</th> : null}
                            <th className="num">No Match</th><th className="num">Accuracy</th>
                          </tr>
                        </thead>
                        <tbody>
                          {metricBands(metric.id).map(([label, lo], i) => {
                            const b = buckets[i];
                            const level = bandFor(METRIC_FEATURE[metric.id], lo);
                            return (
                              <tr key={label}>
                                <td className={`level-text-${level}`}>{label}</td>
                                <td className="num">{b.pages.toLocaleString()}</td>
                                <td className="num">{b.yes.toLocaleString()}</td>
                                {partial ? <td className="num">{b.maybe.toLocaleString()}</td> : null}
                                <td className="num">{b.no.toLocaleString()}</td>
                                <td className="num">{scorePct(b)}</td>
                              </tr>
                            );
                          })}
                          <tr className="chart-details-total">
                            <td>Total</td><td className="num">{all.pages.toLocaleString()}</td>
                            <td className="num">{all.yes.toLocaleString()}</td>
                            {partial ? <td className="num">{all.maybe.toLocaleString()}</td> : null}
                            <td className="num">{all.no.toLocaleString()}</td><td className="num">{scorePct(all)}</td>
                          </tr>
                        </tbody>
                      </table>
                    </section>
                  );
                })() : (
                <>
                <BandTables title="Chart Confidence"
                  note="The chart's confidence (its pages' levels, weighted). Pages counted under their chart's band."
                  unit="Chart"
                  bands={data.chartBands.map((b) => ({ ...emptyScore(), pages: b.pages, yes: b.yes, no: b.pages - b.yes }))}
                  charts={data.chartBands.map((b) => b.charts)}
                  metricBands={data.chartMetricBands} streams={data.chartBands.map((b) => b.streams)} />
                <BandTables title="Page Confidence (basis level)" unit="Page"
                  note="The page's level from how many of its metrics are High / Medium / Low. Colours everywhere follow this."
                  bands={data.pageBands} metricBands={data.pageMetricBands} streams={data.pageStreams} />
                <BandTables title="Page Score (basis average)" unit="Page"
                  note="The plain average of the page's metric confidences. A number only: no colour or level comes from it."
                  bands={data.scoreBands} metricBands={data.scoreMetricBands} streams={data.scoreStreams} />
                </>
                )}
              </div>
            </div>,
            document.body,
          )
        : null}
    </>
  );
}

/** 0/7 .. 7/7: pages by how many of the seven streams match (2026-11-25, user). */
const STREAM_HEADS = Array.from({ length: OVERALL_STREAMS + 1 }, (_, k) => `${k}/${OVERALL_STREAMS}`);

/** One Overview group (2026-11-25, user: Chart Confidence, Page Confidence, Page Score, each
 * clearly set apart with two tables): pages by the measure's band (Match = every metric
 * matching), then each metric's accuracy by the same band. ``charts``: the chart group's
 * chart counts per band. */
function BandTables({ title, note, unit, bands, charts, metricBands, streams }: {
  title: string; note: string; unit: "Chart" | "Page"; bands: LevelScore[]; charts?: number[];
  metricBands: Map<MetricId, LevelScore[]>; streams: number[][];
}) {
  const streamTotal = streams.reduce((acc, row) => acc.map((n, k) => n + row[k]), new Array<number>(OVERALL_STREAMS + 1).fill(0));
  const share = (n: number, total: number) => (total ? `${Math.round((100 * n) / total)}%` : "—");
  const levelOf = (lo: number) => (lo >= 0.8 ? "high" : lo >= 0.5 ? "medium" : "low");
  // Accuracy (6/7), 2026-11-25 (user): pages with at least 6 of the 7 streams matching, of the
  // pages counted by streams.
  const nearShare = (row: number[]) => share(row[OVERALL_STREAMS - 1] + row[OVERALL_STREAMS], row.reduce((sum, n) => sum + n, 0));
  const t = bands.reduce((acc, b) => ({ pages: acc.pages + b.pages, yes: acc.yes + b.yes }), { pages: 0, yes: 0 });
  const chartTotal = charts?.reduce((sum, n) => sum + n, 0);
  return (
    <div className="confidence-group">
      <h3 className="confidence-group-title">{title}</h3>
      <p className="chart-details-note">{note} Match = every metric on the page matches.</p>
      <section>
        <h4>{unit} level: accuracy by band <CopySectionTable /></h4>
        <table className="chart-details-table">
          <thead>
            <tr><th>Band</th>{charts ? <th className="num">Charts</th> : null}<th className="num">Pages</th>
              {STREAM_HEADS.map((h) => <th key={h} className="num">{h}</th>)}
              <th className="num">Match</th><th className="num">No Match</th><th className="num">Accuracy</th>
              <th className="num" title={`Pages with at least ${OVERALL_STREAMS - 1} of the ${OVERALL_STREAMS} metrics matching`}>
                Accuracy ({OVERALL_STREAMS - 1}/{OVERALL_STREAMS})
              </th></tr>
          </thead>
          <tbody>
            {BANDS.map(([label, lo], i) => {
              const b = bands[i];
              return (
                <tr key={label}>
                  <td className={`level-text-${levelOf(lo)}`}>{label}</td>
                  {charts ? <td className="num">{charts[i].toLocaleString()}</td> : null}
                  <td className="num">{b.pages.toLocaleString()}</td>
                  {streams[i].map((n, k) => <td key={k} className="num">{n.toLocaleString()}</td>)}
                  <td className="num">{b.yes.toLocaleString()}</td>
                  <td className="num">{(b.no + b.maybe).toLocaleString()}</td>
                  <td className="num">{b.pages ? share(b.yes, b.pages) : "—"}</td>
                  <td className="num">{nearShare(streams[i])}</td>
                </tr>
              );
            })}
            <tr className="chart-details-total">
              <td>Total</td>
              {chartTotal != null ? <td className="num">{chartTotal.toLocaleString()}</td> : null}
              <td className="num">{t.pages.toLocaleString()}</td>
              {streamTotal.map((n, k) => <td key={k} className="num">{n.toLocaleString()}</td>)}
              <td className="num">{t.yes.toLocaleString()}</td>
              <td className="num">{(t.pages - t.yes).toLocaleString()}</td><td className="num">{share(t.yes, t.pages)}</td>
              <td className="num">{nearShare(streamTotal)}</td>
            </tr>
          </tbody>
        </table>
      </section>
      <section>
        <h4>Metric level: each metric's accuracy by band <CopySectionTable /></h4>
        <table className="chart-details-table">
          <thead>
            <tr><th>Band</th>
              {LIVE_METRICS.map((m) => <th key={m.id} className="num">{m.tableLabel ?? m.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {BANDS.map(([label, lo], i) => (
              <tr key={label}>
                <td className={`level-text-${levelOf(lo)}`}>{label}</td>
                {LIVE_METRICS.map((m) => {
                  const b = metricBands.get(m.id)![i];
                  return <td key={m.id} className="num">{b.pages ? scorePct(b) : "—"}</td>;
                })}
              </tr>
            ))}
            <tr className="chart-details-total">
              <td>Total</td>
              {LIVE_METRICS.map((m) => {
                const all = metricBands.get(m.id)!.reduce((acc, b) => ({ pages: acc.pages + b.pages, yes: acc.yes + b.yes, maybe: acc.maybe + b.maybe, no: acc.no + b.no }), emptyScore());
                return <td key={m.id} className="num">{scorePct(all)}</td>;
              })}
            </tr>
          </tbody>
        </table>
      </section>
    </div>
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
type Measure = "accuracy" | "precision" | "recall" | "f1" | "credMatch" | "invoiceMatch" | "duplicateMatch";
type ColumnRanges = Partial<Record<Measure, Range>>;
const RATE_MEASURES = [
  { key: "precision", label: "Precision", percent: precisionPercent },
  { key: "recall", label: "Recall", percent: recallPercent },
  { key: "f1", label: "F1", percent: f1Percent },
  { key: "credMatch", label: "Cred. M", percent: credMatchPercent },
] as const;

/** Match % on the pages either side calls Yes: both Yes over Yes on either side (No / No
 * left out, as the Additional Metrics tables). Null when neither side calls any page Yes. */
function yesMatchPercent(rates: ClassRates | undefined): number | null {
  if (!rates) return null;
  const total = rates.tp + rates.fp + rates.fn;
  return total === 0 ? null : Math.round((100 * rates.tp) / total);
}

/** Beside B/J (2026-11-14, user): sliders for Invoice and Duplicate Match, e.g. to list the
 * files under 100%. Duplicate = Yes only (Partial counts as No). */
const BJ_EXTRA_MEASURES = [
  { key: "invoiceMatch", label: "Invoice Match", percent: (row: ChartAccuracy) => yesMatchPercent(row.invoiceRates) },
  { key: "duplicateMatch", label: "Duplicate Match", percent: (row: ChartAccuracy) => yesMatchPercent(row.duplicateRates) },
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
    for (const measure of BJ_EXTRA_MEASURES) {
      if (f[measure.key] && !inRange(measure.percent(row), f[measure.key])) return false;
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
          {column === "blankJunk"
            ? BJ_EXTRA_MEASURES.map((measure) => (
                <RangeSliders
                  key={measure.key}
                  label={`${measure.label} (Yes on either side)`}
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

/** A chart's values for one metadata filter: one for Scope / Source / Measure / Retrieval,
 * any number for Tag (2026-11-07). None reads "(Blank)". */
function metaValues(row: ChartAccuracy, key: MetaFilterKey): string[] {
  if (key === "tag") return row.tags ?? [];
  if (key === "cluster") return row.chartCluster ? [row.chartCluster] : [];
  const value =
    key === "scope" ? row.projectScope
    : key === "source" ? row.sourceName
    : key === "measure" ? row.measure
    : row.rrMethod;
  return value ? [value] : [];
}

function passesMeta(row: ChartAccuracy, filters: MetaFilters): boolean {
  return CHART_META_FILTERS.every(({ key }) => {
    const wanted = filters[key];
    if (!wanted?.length) return true;
    const values = metaValues(row, key);
    return (values.length ? values : [BLANK_META]).some((value) => wanted.includes(value));
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
  // 2026-11-19 (user): the Confidence column (was Last Verified At): High > Medium > Low, then the %.
  if (key === "verified") {
    const cc = row.confidence;
    if (!cc?.level) return null;
    return ({ high: 3, medium: 2, low: 1 } as const)[cc.level] * 1000 + Math.round((cc.value ?? 0) * 100);
  }
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

/** Download Page Level Accuracy (2026-11-16, user): verdicts only (the default), or with the
 * values each metric compared, as GT_ / Algo_ columns after the verdicts. Always a CSV. */
function PageAccuracyDownloadDialog({
  charts,
  pages,
  onCancel,
  onDownload,
}: {
  charts: number;
  pages: number;
  onCancel: () => void;
  onDownload: (withValues: boolean) => void;
}) {
  const [withValues, setWithValues] = useState(false);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);
  const choices: [boolean, string, string][] = [
    [false, "Match only", "Match / No Match per metric."],
    [true, "Along with values", "Adds GT_ and Algo_ value columns."],
  ];
  return createPortal(
    <div className="modal-backdrop" role="presentation" onClick={onCancel}>
      <div
        className="modal-card accuracy-download-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="page-accuracy-download-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-card-header">
          <div className="blob-auth-title-row">
            <Download size={18} aria-hidden="true" />
            <h2 id="page-accuracy-download-title">Download Accuracy</h2>
          </div>
          <button type="button" className="modal-close" onClick={onCancel} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <p className="blob-auth-copy">
          {pages.toLocaleString()} scored page{pages === 1 ? "" : "s"} from {charts.toLocaleString()} chart
          {charts === 1 ? "" : "s"} (filters applied), as an Excel file with two tabs: Pages (page level,
          with each page's confidence) and Charts (chart level accuracy and confidence).
        </p>
        <div className="download-results-choice" role="radiogroup" aria-label="What to include">
          {choices.map(([value, title, detail]) => (
            <label key={title} className={withValues === value ? "active" : undefined}>
              <input
                type="radio"
                name="page-accuracy-download"
                checked={withValues === value}
                onChange={() => setWithValues(value)}
              />
              <span>
                <strong>{title}</strong>
                <span>{detail}</span>
              </span>
            </label>
          ))}
        </div>
        <div className="download-results-actions">
          <button type="button" className="landing-file-viewer-btn" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="landing-file-viewer-btn landing-file-viewer-btn-primary"
            onClick={() => onDownload(withValues)}
            autoFocus
          >
            <Download size={14} aria-hidden="true" />
            Download CSV
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export default function AccuracyView({ onBack, onOpenChart, chartHref }: Props) {
  const cached = lastRows?.source === scoringKey() ? lastRows.rows : null;
  const [allRows, setRows] = useState<ChartAccuracy[]>(cached ?? []);
  // Nothing to show yet: the full Calculating panel. Cached rows on screen: a small note.
  const [loading, setLoading] = useState(cached === null);
  const [refreshing, setRefreshing] = useState(false);
  const [reloads, setReloads] = useState(0);
  const [downloadOpen, setDownloadOpen] = useState(false);
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
    /** Pages (scored pages) and Confidence column filters (2026-11-25, user). */
    pagesRange?: NumberRange | null;
    confidenceFilter?: ConfidenceFilter | null;
  }>("accuracy", {
    query: "", page: 1, sortKey: "run", sortDir: "desc", filters: {}, runAfter: null, meta: {}, runs: [], batches: [],
  });
  const { query, page, sortKey, sortDir, filters } = view;
  const runAfter = view.runAfter ?? null;
  const meta = useMemo(() => view.meta ?? {}, [view.meta]);
  const runs = useMemo(() => view.runs ?? [], [view.runs]);
  const batches = useMemo(() => view.batches ?? [], [view.batches]);
  const pagesRange = view.pagesRange ?? null;
  const confidenceFilter = view.confidenceFilter ?? null;
  // The chart-level filters (Run after, Run, Batch, Source, Measure, Retrieval).
  const rows = useMemo(
    () =>
      allRows.filter(
        (row) =>
          ranAfter(row.lastUpdatedAt, runAfter) &&
          passesMeta(row, meta) &&
          (!runs.length || (row.runId != null && runs.includes(row.runId))) &&
          (!batches.length || (row.batchId != null && batches.includes(row.batchId))) &&
          inNumberRange(row.overall.scored, pagesRange) &&
          passesConfidence(row.confidence, confidenceFilter),
      ),
    [allRows, runAfter, meta, runs, batches, pagesRange, confidenceFilter],
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
    for (const { key } of CHART_META_FILTERS) {
      const values = new Set<string>();
      let blank = false;
      for (const row of allRows) {
        const found = metaValues(row, key);
        for (const value of found) values.add(value);
        if (!found.length) blank = true;
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
    extra,
    extraActive = false,
  }: {
    sort: SortKey;
    label: string;
    filter?: ScoreColumn;
    runFilter?: boolean;
    /** Another filter button after the title (Pages, Confidence). */
    extra?: ReactNode;
    extraActive?: boolean;
  }) {
    const rates = filter && filter !== "overall" ? ratesFor(filter) : undefined;
    const filtered =
      (filter ? isActive(filters[filter]) : false) || (runFilter && Boolean(runAfter)) || extraActive;
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
          {extra}
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
          row.tags = chart.tags ?? [];
          row.chartCluster = chart.chartCluster ?? null;
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
    filterCount + (runAfter ? 1 : 0) + metaFilterCount(meta) + (runs.length ? 1 : 0) + (batches.length ? 1 : 0) +
    (rangeOn(pagesRange) ? 1 : 0) + (confidenceFilterOn(confidenceFilter) ? 1 : 0);
  const clearAll = () =>
    setView({
      filters: {}, runAfter: null, meta: {}, runs: [], batches: [], pagesRange: null, confidenceFilter: null, page: 1,
    });
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matched = rows.filter(
      (row) => (!needle || row.chartName.toLowerCase().includes(needle)) && passesFilters(row, filters),
    );
    return matched.sort((a, b) => compareRows(a, b, sortKey, sortDir));
  }, [rows, query, filters, sortKey, sortDir]);
  calibrationRows = filtered;
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
  // Back from a chart: the scroll position too, once the rows are back (2026-11-16, user).
  const scrollRef = useScrollRestore("accuracy", !loading && filtered.length > 0);
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
  const memberMatrix = useMemo(() => sumMemberMatrix(filtered.map((row) => row.memberMatrix)), [filtered]);
  const dosMatrix = useMemo(() => sumMemberMatrix(filtered.map((row) => row.dosMatrix)), [filtered]);
  const codableMatrix = useMemo(() => sumMemberMatrix(filtered.map((row) => row.codableMatrix)), [filtered]);
  const signatureRates = useMemo(
    () => sumRates(filtered.map((row) => row.signatureRates)),
    [filtered],
  );
  const dosRates = useMemo(() => sumRates(filtered.map((row) => row.dosRates)), [filtered]);
  const orientationRates = useMemo(
    () => sumRates(filtered.map((row) => row.orientationRates)),
    [filtered],
  );
  const extra = useMemo(() => sumExtras(filtered.map((row) => row.extra)), [filtered]);
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

  // 2026-11-19 (user): one .xlsx, two tabs: Pages (page level) and Charts (chart level). If
  // the server cannot build it, the page-level CSV as before.
  async function downloadPageAccuracy(withValues: boolean) {
    const day = new Date().toISOString().slice(0, 10);
    const save = (blob: Blob, name: string) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    try {
      const pages = pageAccuracyTable(filtered, withValues);
      const charts = chartAccuracyTable(filtered);
      const blob = await downloadXlsx([
        { name: "Pages", header: pages.header, rows: pages.rows },
        { name: "Charts", header: charts.header, rows: charts.rows },
      ]);
      save(blob, `accuracy${withValues ? "_with_values" : ""}_${day}.xlsx`);
    } catch {
      save(new Blob(["\ufeff" + pageAccuracyCsv(filtered, withValues)], { type: "text/csv;charset=utf-8" }),
        `page_level_accuracy${withValues ? "_with_values" : ""}_${day}.csv`);
    }
  }

  return (
    <div className="workspace accuracy-view" ref={scrollRef}>
      {downloadOpen ? (
        <PageAccuracyDownloadDialog
          charts={filtered.length}
          pages={filtered.reduce((sum, chart) => sum + chart.pages.length, 0)}
          onCancel={() => setDownloadOpen(false)}
          onDownload={(withValues) => {
            downloadPageAccuracy(withValues);
            setDownloadOpen(false);
          }}
        />
      ) : null}
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
              disabled={refreshing || filtered.length === 0}
              title="Every scored page of the charts shown (filters applied): Match / No Match per metric, as a CSV"
              onClick={() => setDownloadOpen(true)}
            >
              <Download size={14} aria-hidden="true" />
              Download Page Level Accuracy
            </button>
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
            <div className="accuracy-metric-head">
              <h2>By metric</h2>
              <span className="accuracy-extra-links">
                <ConfidenceMetrics rows={filtered} />
                <AdditionalMetrics extra={extra} />
              </span>
            </div>
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
                  info={metric.id === "member"
                    ? memberInfo(metric.rule, overallByMetric.member, memberRates, memberMatrix)
                    : metric.id === "dos"
                      ? [...metricInfo(metric.rule, overallByMetric.dos, true, undefined, "", dosTables(dosMatrix)), dosYesSection(dosMatrix)]
                      : metricInfo(
                        metric.rule,
                        overallByMetric[metric.id],
                        Boolean(metric.binary),
                        // DOS has its own Yes view (page counts), above.
                        ["blankJunk", "orientation", "signature"].includes(metric.id) ? ratesFor(metric.id) : undefined,
                        ratesLabelFor(metric.id),
                        metric.id === "codeable" ? codableTables(codableMatrix) : [],
                      ).concat(metric.id === "signature" ? [credentialSection(signatureRates)] : [])}
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
                <MetaFilterBar only={CHART_META_FILTERS} options={metaOptions} filters={meta} onChange={(next) => setView({ meta: next, page: 1 })} />
                <ClearAllFilters count={allFilterCount} onClear={clearAll} />
              </div>
            </div>
            <div className="accuracy-table-scroll">
            <table className="landing-history-table accuracy-table">
              <thead>
                <tr>
                  {SortHeader({ sort: "name", label: "Chart" })}
                  {SortHeader({ sort: "run", label: "Last Run At", runFilter: true })}
                  {SortHeader({
                    sort: "verified",
                    label: "Confidence",
                    extraActive: confidenceFilterOn(confidenceFilter),
                    extra: (
                      <ColumnFilterButton
                        label="Confidence"
                        active={confidenceFilterOn(confidenceFilter)}
                        icon={<Filter size={11} aria-hidden="true" />}
                      >
                        {() => (
                          <ConfidenceFilterForm
                            value={confidenceFilter}
                            onChange={(next) => setView({ confidenceFilter: next, page: 1 })}
                          />
                        )}
                      </ColumnFilterButton>
                    ),
                  })}
                  {SortHeader({
                    sort: "pages",
                    label: "Pages",
                    extraActive: rangeOn(pagesRange),
                    extra: (
                      <ColumnFilterButton
                        label="Pages"
                        active={rangeOn(pagesRange)}
                        icon={<Filter size={11} aria-hidden="true" />}
                      >
                        {(close) => (
                          <NumberRangeForm
                            value={pagesRange}
                            onApply={(next) => {
                              setView({ pagesRange: next, page: 1 });
                              close();
                            }}
                          />
                        )}
                      </ColumnFilterButton>
                    ),
                  })}
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
                  <tr
                    key={row.chartId}
                    title={row.confidence?.level
                      ? `Confidence ${row.confidence.level}: pages High ${row.confidence.counts.high} · Medium ${row.confidence.counts.medium} · Low ${row.confidence.counts.low}`
                      : undefined}
                  >
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
                    <td className="accuracy-col-confidence"
                        title="Chart confidence: High = 80%+ of pages High; Low = 20%+ of pages Low; else Medium">
                      {row.confidence?.level ? (
                        <div className="accuracy-cell">
                          <strong>
                            {Math.round((row.confidence.value ?? 0) * 100)}%{" "}
                            <span className={`chart-glance-pill level-${row.confidence.level}`}>
                              {levelLabel(row.confidence.level)}
                            </span>
                          </strong>
                          <dl className="accuracy-class-grid">
                            {(["high", "medium", "low"] as const).map((lv) => (
                              <div key={lv}>
                                <dt>{levelLabel(lv)}</dt>
                                <dd>{row.confidence?.counts[lv] ?? 0}</dd>
                              </div>
                            ))}
                          </dl>
                        </div>
                      ) : "—"}
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
