import { useState } from "react";

/** A text input whose list of options drops down once ``minChars`` letters are typed, those
 * that start with them first, as Page Details' ground-truth cells do. Any text is kept; one
 * not in the list shows ``warning`` (2026-10-27, user). */
export function SearchInput({
  value,
  options,
  onChange,
  ariaLabel,
  className,
  minChars = 2,
  warning,
}: {
  value: string;
  options: readonly string[];
  onChange: (value: string) => void;
  ariaLabel: string;
  className?: string;
  minChars?: number;
  warning?: string;
}) {
  const [open, setOpen] = useState(false);
  const folded = value.trim().toLowerCase();
  const matches =
    open && folded.length >= minChars
      ? [
          ...options.filter((option) => option.toLowerCase().startsWith(folded)),
          ...options.filter((option) => !option.toLowerCase().startsWith(folded) && option.toLowerCase().includes(folded)),
        ].slice(0, 30)
      : [];
  const known = !folded || options.some((option) => option.toLowerCase() === folded);
  return (
    <div className="imaging-gt-picker search-input">
      <input
        className={className}
        value={value}
        aria-label={ariaLabel}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onChange={(event) => {
          onChange(event.target.value);
          setOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") setOpen(false);
        }}
      />
      {matches.length > 0 && !(matches.length === 1 && matches[0].toLowerCase() === folded) ? (
        <ul className="imaging-gt-menu" role="listbox" aria-label={ariaLabel}>
          {matches.map((option) => (
            <li key={option}>
              <button
                type="button"
                role="option"
                aria-selected={option === value}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  onChange(option);
                  setOpen(false);
                }}
              >
                {option}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {warning && !known && !open ? <span className="imaging-gt-hint is-warning">{warning}</span> : null}
    </div>
  );
}
