import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown, Filter, X } from "lucide-react";

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

/** A filter popover's tick list: "Select all" (no filter) and one box per value. */
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
  if (lockedHint) return <p className="column-filter-hint">{lockedHint}</p>;
  const toggle = (value: string) =>
    onChange(
      selected.includes(value)
        ? selected.filter((v) => v !== value)
        : [...selected, value].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })),
    );
  return (
    <>
      <div className="column-filter-list" role="listbox" aria-multiselectable="true">
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
        {options.length === 0 ? (
          <div className="landing-multi-empty">No values yet</div>
        ) : (
          options.map((value) => (
            <label key={value} className="landing-multi-option">
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
        Clear
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
export type MetaFilterKey = "source" | "measure" | "retrieval";
export type MetaFilters = Partial<Record<MetaFilterKey, string[]>>;
export const META_FILTERS: { key: MetaFilterKey; label: string; title: string }[] = [
  { key: "source", label: "Source", title: "Source name" },
  { key: "measure", label: "Measure", title: "Measure" },
  { key: "retrieval", label: "Retrieval", title: "Retrieval (RR Method)" },
];

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
  const summary =
    selected.length === 0 ? "All" : selected.length === 1 ? formatOption(selected[0]) : `${selected.length} selected`;
  return (
    <ColumnFilterButton
      label={title ?? label}
      active={selected.length > 0}
      icon={<Filter size={12} aria-hidden="true" />}
      align="start"
      width={240}
      text={
        <span className="top-filter-text">
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
