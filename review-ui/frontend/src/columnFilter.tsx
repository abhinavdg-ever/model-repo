import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown, Filter, Search, X } from "lucide-react";

/** A table column's filter: an icon button at the right of the header title and a popover
 * under it. The popover is fixed-position so the table's scroll box does not clip it.
 * The History table and Accuracy View share it, so their filters look and behave alike. */
export function ColumnFilterButton({
  label,
  active,
  icon,
  width = 220,
  align = "end",
  popoverClassName,
  text,
  children,
}: {
  label: string;
  active: boolean;
  icon: ReactNode;
  /** Set for a toolbar filter: a labelled button ("Source: All") instead of a bare icon. */
  text?: ReactNode;
  /** Popover width in px, used to keep it on screen. */
  width?: number;
  /** "end": the popover's right edge under the button; "start": its left edge. */
  align?: "start" | "end";
  popoverClassName?: string;
  /** The popover's content; ``close`` shuts it (after Apply, say). */
  children: (close: () => void) => ReactNode;
}) {
  const [anchor, setAnchor] = useState<{ top: number; left: number; maxHeight: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!anchor) return;
    const close = (event: Event) => {
      const target = event.target as Node;
      if (popoverRef.current?.contains(target) || buttonRef.current?.contains(target)) return;
      setAnchor(null);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setAnchor(null);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
    // Scrolling the table or page would leave the popover behind its column.
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [anchor]);

  function toggle() {
    if (anchor) {
      setAnchor(null);
      return;
    }
    const rect = buttonRef.current?.getBoundingClientRect();
    if (!rect) return;
    const wanted = align === "end" ? rect.right - width : rect.left;
    const top = rect.bottom + 6;
    // Never past the window: the popover scrolls inside itself instead.
    setAnchor({
      top,
      left: Math.max(8, Math.min(wanted, window.innerWidth - width - 8)),
      maxHeight: Math.max(160, window.innerHeight - top - 8),
    });
  }

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className={`${text ? "top-filter-btn" : "column-filter-btn"}${active ? " active" : ""}`}
        title={active ? `Filter ${label} (on)` : `Filter ${label}`}
        aria-label={`Filter ${label}`}
        aria-pressed={active}
        aria-expanded={anchor !== null}
        onClick={toggle}
      >
        {icon}
        {text}
      </button>
      {anchor ? (
        <div
          ref={popoverRef}
          className={`column-filter-popover${popoverClassName ? ` ${popoverClassName}` : ""}`}
          style={{ top: anchor.top, left: anchor.left, width, maxHeight: anchor.maxHeight }}
          role="dialog"
          aria-label={`Filter ${label}`}
        >
          {children(() => setAnchor(null))}
        </div>
      ) : null}
    </>
  );
}

/** Edits to turn ``a`` into ``b``, stopping once it passes ``limit``. */
function editDistance(a: string, b: string, limit: number): number {
  if (Math.abs(a.length - b.length) > limit) return limit + 1;
  let previous = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const current = [i];
    let best = i;
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      current[j] = Math.min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost);
      best = Math.min(best, current[j]);
    }
    if (best > limit) return limit + 1;
    previous = current;
  }
  return previous[b.length];
}

/** How well ``text`` answers a typed search, 0 = not at all. Starts of the text or of a
 * word rank first, then anywhere in it, then every typed word somewhere in it, then the
 * letters in order ("athint" finds "Athena Integration"), then a word one or two typos off
 * ("athina"). */
export function fuzzyScore(query: string, text: string): number {
  const q = query.trim().toLowerCase();
  if (!q) return 1;
  const t = text.toLowerCase();
  if (t.startsWith(q)) return 6;
  const words = t.split(/[^a-z0-9]+/).filter(Boolean);
  if (words.some((w) => w.startsWith(q))) return 5;
  if (t.includes(q)) return 4;
  const parts = q.split(/\s+/).filter(Boolean);
  if (parts.length > 1 && parts.every((part) => t.includes(part))) return 3;
  let at = 0;
  for (const ch of q.replace(/\s+/g, "")) {
    at = t.indexOf(ch, at);
    if (at < 0) break;
    at += 1;
  }
  if (at > 0 && q.replace(/\s+/g, "").length >= 2) return 2;
  const typos = (part: string) => (part.length >= 7 ? 2 : part.length >= 4 ? 1 : 0);
  if (
    parts.every((part) => {
      const limit = typos(part);
      return limit > 0 && words.some((w) => editDistance(part, w.slice(0, part.length + limit), limit) <= limit);
    })
  ) {
    return 1;
  }
  return 0;
}

/** A filter popover's tick list: a search box (fuzzy, like Excel's), "Select all" (no
 * filter) or, while searching, "Select all shown", and one box per value. */
export function CheckListFilter({
  options,
  selected,
  onChange,
  formatOption,
  lockedHint,
}: {
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  formatOption: (value: string) => string;
  /** Set when the list cannot be used yet ("Select a run first"). */
  lockedHint?: string;
}) {
  const [query, setQuery] = useState("");
  if (lockedHint) return <p className="column-filter-hint">{lockedHint}</p>;
  const byNumber = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true });
  const toggle = (value: string) =>
    onChange(selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value].sort(byNumber));
  const searching = query.trim() !== "";
  const shown = searching
    ? options
        .map((value, index) => ({ value, index, score: fuzzyScore(query, formatOption(value)) }))
        .filter((row) => row.score > 0)
        .sort((a, b) => b.score - a.score || a.index - b.index)
        .map((row) => row.value)
    : options;
  const allShownPicked = shown.length > 0 && shown.every((value) => selected.includes(value));
  function pickShown() {
    if (allShownPicked) onChange(selected.filter((value) => !shown.includes(value)));
    else onChange([...new Set([...selected, ...shown])].sort(byNumber));
  }
  return (
    <>
      {options.length > 0 ? (
        <label className="column-filter-search">
          <Search size={12} aria-hidden="true" />
          <input
            type="search"
            value={query}
            placeholder="Search…"
            aria-label="Search the values"
            autoFocus
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              // Enter ticks every value the search shows, as Excel's does.
              if (e.key === "Enter" && searching && shown.length && !allShownPicked) pickShown();
            }}
          />
        </label>
      ) : null}
      <div className="column-filter-list" role="listbox" aria-multiselectable="true">
        {searching ? (
          <label className="landing-multi-option landing-multi-select-all">
            <input type="checkbox" checked={allShownPicked} disabled={!shown.length} onChange={pickShown} />
            <span>Select all shown ({shown.length})</span>
          </label>
        ) : (
          <label className="landing-multi-option landing-multi-select-all">
            <input
              type="checkbox"
              checked={selected.length === 0}
              onChange={() => {
                if (selected.length > 0) onChange([]);
              }}
            />
            <span>Select all</span>
          </label>
        )}
        {options.length === 0 ? (
          <div className="landing-multi-empty">No values yet</div>
        ) : shown.length === 0 ? (
          <div className="landing-multi-empty">No value matches “{query.trim()}”</div>
        ) : (
          shown.map((value) => (
            <label key={value} className="landing-multi-option" title={formatOption(value)}>
              <input type="checkbox" checked={selected.includes(value)} onChange={() => toggle(value)} />
              <span>{formatOption(value)}</span>
            </label>
          ))
        )}
      </div>
      <button
        type="button"
        className="column-filter-clear"
        disabled={selected.length === 0}
        onClick={() => onChange([])}
      >
        Clear{selected.length ? ` (${selected.length})` : ""}
      </button>
    </>
  );
}

/** Shown once any column filter is on: clears every one of them (not the search box). */
export function ClearAllFilters({ count, onClear }: { count: number; onClear: () => void }) {
  if (count === 0) return null;
  return (
    <button type="button" className="clear-all-filters" onClick={onClear} title="Clear every column filter">
      <X size={13} aria-hidden="true" />
      Clear all filters ({count})
    </button>
  );
}

/** The chart-metadata filters both tables offer (manifest / metadata CSV). */
export type MetaFilterKey = "scope" | "source" | "measure" | "retrieval";
export type MetaFilters = Partial<Record<MetaFilterKey, string[]>>;
export const META_FILTERS: { key: MetaFilterKey; label: string; title: string }[] = [
  { key: "scope", label: "Scope", title: "Scope (Project Scope)" },
  { key: "measure", label: "Measure", title: "Measure" },
  { key: "source", label: "Source Name", title: "Source Name" },
  { key: "retrieval", label: "Retrieval Method", title: "Retrieval Method (RR Method)" },
];

/** A metadata filter's option for a chart with no value (same as the API's). */
export const BLANK_META = "(Blank)";

/** How many of the metadata filters are on. */
export function metaFilterCount(filters: MetaFilters | undefined): number {
  return META_FILTERS.filter(({ key }) => (filters?.[key]?.length ?? 0) > 0).length;
}

/** A labelled tick-list filter in a table's toolbar ("Run: All ▾"). */
export function TopCheckFilter({
  label,
  title,
  options,
  selected,
  onChange,
  formatOption = (value) => value,
}: {
  label: string;
  title?: string;
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  formatOption?: (value: string) => string;
}) {
  // A count badge, not the value: a long source name made the button wrap.
  const summary =
    selected.length === 0 ? (
      "All"
    ) : (
      <span className="top-filter-count" aria-label={`${selected.length} selected`}>
        {selected.length}
      </span>
    );
  return (
    <ColumnFilterButton
      label={title ?? label}
      active={selected.length > 0}
      icon={<Filter size={12} aria-hidden="true" />}
      align="start"
      width={240}
      text={
        <span
          className="top-filter-text"
          title={selected.length ? `${label}: ${selected.map(formatOption).join(", ")}` : undefined}
        >
          <span className="top-filter-label">{label}:</span>
          <span className="top-filter-value">{summary}</span>
          <ChevronDown size={12} aria-hidden="true" />
        </span>
      }
    >
      {() => (
        <CheckListFilter options={options} selected={selected} onChange={onChange} formatOption={formatOption} />
      )}
    </ColumnFilterButton>
  );
}

/** Source / Measure / Retrieval as labelled filters in a table's toolbar. */
export function MetaFilterBar({
  options,
  filters,
  onChange,
}: {
  options: Partial<Record<MetaFilterKey, string[]>>;
  filters: MetaFilters;
  onChange: (next: MetaFilters) => void;
}) {
  return (
    <>
      {META_FILTERS.map((filter) => (
        <TopCheckFilter
          key={filter.key}
          label={filter.label}
          title={filter.title}
          options={options[filter.key] ?? []}
          selected={filters[filter.key] ?? []}
          onChange={(next) => onChange({ ...filters, [filter.key]: next })}
        />
      ))}
    </>
  );
}
