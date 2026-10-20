import { useState } from "react";

/** Run times are shown in India time (Last Run At), so the filter reads them that way too. */
const RUN_AFTER_OFFSET = "+05:30";

/** "YYYY-MM-DD" and "HH:MM" of an ISO instant in India time; no instant is today, 12:00 AM. */
export function indiaParts(iso: string | null): { date: string; time: string } {
  const d = iso ? new Date(iso) : new Date();
  if (Number.isNaN(d.getTime())) return { date: "", time: "00:00" };
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Kolkata",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(d)
      .map((part) => [part.type, part.value]),
  );
  return {
    date: `${parts.year}-${parts.month}-${parts.day}`,
    time: iso ? `${parts.hour}:${parts.minute}` : "00:00",
  };
}

/** The date `days` before today, India time, "YYYY-MM-DD". */
function indiaDaysAgo(days: number): string {
  return indiaParts(new Date(Date.now() - days * 86_400_000).toISOString()).date;
}

/** "17 Oct 2026, 12:00 am", India time, for text that names the filter. */
export function formatRunAfter(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
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

/** True when the ISO run time is at or after ``runAfter`` (no filter: always). */
export function ranAfter(lastRun: string | null | undefined, runAfter: string | null): boolean {
  if (!runAfter) return true;
  const ran = lastRun ? new Date(lastRun).getTime() : NaN;
  return !Number.isNaN(ran) && ran >= new Date(runAfter).getTime();
}

const QUICK: [string, number][] = [
  ["Today", 0],
  ["Yesterday", 1],
  ["Last 7 days", 7],
];

/** "HH:MM" (24 h) <-> the hour (1-12), minute and AM / PM the picker shows. */
function toParts(time: string): { hour: number; minute: number; pm: boolean } {
  const [h, m] = (time || "00:00").split(":").map((n) => Number(n) || 0);
  return { hour: h % 12 === 0 ? 12 : h % 12, minute: m, pm: h >= 12 };
}

function fromParts(hour: number, minute: number, pm: boolean): string {
  const h = (hour % 12) + (pm ? 12 : 0);
  return `${String(h).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

const HOURS = Array.from({ length: 12 }, (_, i) => i + 1);
const MINUTES = Array.from({ length: 12 }, (_, i) => i * 5);

/** Hour, minute and AM / PM, in the app's own style rather than the browser's time box. */
function TimePicker({ value, onChange }: { value: string; onChange: (next: string) => void }) {
  const { hour, minute, pm } = toParts(value);
  // A saved time off the 5-minute steps (10:07) still shows as itself.
  const minutes = MINUTES.includes(minute) ? MINUTES : [...MINUTES, minute].sort((a, b) => a - b);
  return (
    <div className="run-after-time" role="group" aria-label="Time">
      <select
        className="run-after-select"
        value={hour}
        aria-label="Hour"
        onChange={(e) => onChange(fromParts(Number(e.target.value), minute, pm))}
      >
        {HOURS.map((h) => (
          <option key={h} value={h}>
            {h}
          </option>
        ))}
      </select>
      <span className="run-after-colon" aria-hidden="true">:</span>
      <select
        className="run-after-select"
        value={minute}
        aria-label="Minute"
        onChange={(e) => onChange(fromParts(hour, Number(e.target.value), pm))}
      >
        {minutes.map((m) => (
          <option key={m} value={m}>
            {String(m).padStart(2, "0")}
          </option>
        ))}
      </select>
      <div className="run-after-ampm">
        {(["AM", "PM"] as const).map((label) => (
          <button
            key={label}
            type="button"
            className={(label === "PM") === pm ? "is-on" : undefined}
            aria-pressed={(label === "PM") === pm}
            onClick={() => onChange(fromParts(hour, minute, label === "PM"))}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}

/** The Run after picker: Today / Yesterday / Last 7 days, a date and a time (12:00 AM unless
 *  changed), Clear and Apply. Mount it when the popover opens; it starts from ``value``. */
export function RunAfterForm({
  value,
  onApply,
}: {
  value: string | null;
  /** The ISO date-time (India offset) to keep runs at or after, or null to clear. */
  onApply: (next: string | null) => void;
}) {
  const [date, setDate] = useState(() => indiaParts(value).date);
  const [time, setTime] = useState(() => indiaParts(value).time);

  function apply() {
    if (date) onApply(`${date}T${time || "00:00"}:00${RUN_AFTER_OFFSET}`);
  }

  return (
    <div className="run-after-form">
      <div className="run-after-quick">
        {QUICK.map(([label, days]) => {
          const day = indiaDaysAgo(days);
          return (
            <button
              key={label}
              type="button"
              className={`run-after-chip${date === day && time === "00:00" ? " is-on" : ""}`}
              onClick={() => {
                setDate(day);
                setTime("00:00");
              }}
            >
              {label}
            </button>
          );
        })}
      </div>
      <div className="run-after-fields">
        <label>
          <span>Date</span>
          <input
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && apply()}
          />
        </label>
        <div className="run-after-field">
          <span>Time</span>
          <TimePicker value={time} onChange={setTime} />
        </div>
      </div>
      <p className="run-after-hint">India time · charts last run at or after this</p>
      <div className="run-after-actions">
        <button type="button" className="run-after-clear" onClick={() => onApply(null)} disabled={!value}>
          Clear
        </button>
        <button type="button" className="run-after-apply" onClick={apply} disabled={!date}>
          Apply
        </button>
      </div>
    </div>
  );
}
