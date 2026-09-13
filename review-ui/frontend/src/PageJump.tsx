import { useEffect, useState } from "react";

type Props = {
  pageIndex: number;
  pageCount: number;
  onJump: (pageIndex: number) => void;
  className?: string;
  /** Extra text after the total, e.g. fullscreen filename */
  suffix?: string;
};

/** Editable "N / total" control — type a page number and Enter/blur to jump. */
export default function PageJump({
  pageIndex,
  pageCount,
  onJump,
  className = "pager-label",
  suffix,
}: Props) {
  const [draft, setDraft] = useState(String(pageIndex + 1));

  useEffect(() => {
    setDraft(String(pageIndex + 1));
  }, [pageIndex]);

  function commit() {
    if (pageCount <= 0) {
      setDraft("—");
      return;
    }
    const n = Number.parseInt(draft, 10);
    if (!Number.isFinite(n)) {
      setDraft(String(pageIndex + 1));
      return;
    }
    const clamped = Math.min(pageCount, Math.max(1, n));
    setDraft(String(clamped));
    if (clamped - 1 !== pageIndex) {
      onJump(clamped - 1);
    }
  }

  if (pageCount <= 0) {
    return <span className={className}>—</span>;
  }

  return (
    <span className={`${className} pager-jump`}>
      <input
        type="text"
        inputMode="numeric"
        pattern="[0-9]*"
        className="pager-jump-input"
        value={draft}
        size={Math.max(2, String(pageCount).length)}
        onChange={(e) => setDraft(e.target.value.replace(/\D/g, ""))}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            (e.target as HTMLInputElement).blur();
          } else if (e.key === "Escape") {
            setDraft(String(pageIndex + 1));
            (e.target as HTMLInputElement).blur();
          }
        }}
        aria-label="Go to page"
        title="Type a page number and press Enter"
      />
      <span className="pager-jump-total"> / {pageCount}</span>
      {suffix ? <span className="pager-jump-suffix">{suffix}</span> : null}
    </span>
  );
}
