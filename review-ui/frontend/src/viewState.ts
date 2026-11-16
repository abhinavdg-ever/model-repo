import { useEffect, useState } from "react";

/** A list page's search, filters, sort and page, kept for this browser tab
 * (sessionStorage) so Back from a chart lands where the reader left off. A tab
 * opened from the page (window.open) starts with a copy. */
export function useViewState<T extends object>(key: string, initial: T): [T, (update: Partial<T>) => void] {
  const storageKey = `advantmed_view_${key}`;
  const [state, setState] = useState<T>(() => {
    try {
      const raw = sessionStorage.getItem(storageKey);
      return raw ? { ...initial, ...(JSON.parse(raw) as Partial<T>) } : initial;
    } catch {
      return initial;
    }
  });

  useEffect(() => {
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(state));
    } catch {
      // Blocked storage: the page starts fresh after Back.
    }
  }, [storageKey, state]);

  return [state, (update) => setState((prev) => ({ ...prev, ...update }))];
}

function scrollParent(node: HTMLElement | null): HTMLElement {
  for (let el = node; el; el = el.parentElement) {
    const style = window.getComputedStyle(el);
    if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight) return el;
  }
  return (document.scrollingElement as HTMLElement) ?? document.documentElement;
}

/** Back from a chart lands where the reader was scrolled, not at the top (2026-11-16, user):
 * the list's scroll position is kept for this tab and put back once ``ready`` (its rows are
 * on screen). Attach the returned ref to the list page's root element. */
export function useScrollRestore(key: string, ready: boolean): (node: HTMLElement | null) => void {
  const storageKey = `advantmed_scroll_${key}`;
  const [node, setNode] = useState<HTMLElement | null>(null);
  const [restored, setRestored] = useState(false);

  useEffect(() => {
    if (!node) return;
    const onScroll = (event: Event) => {
      const target = event.target === document ? document.scrollingElement : (event.target as HTMLElement | null);
      if (!(target instanceof HTMLElement) || !(target.contains(node) || node.contains(target))) return;
      if (target !== scrollParent(node)) return;
      try {
        sessionStorage.setItem(storageKey, String(Math.round(target.scrollTop)));
      } catch {
        // Blocked storage: Back starts at the top.
      }
    };
    document.addEventListener("scroll", onScroll, true);
    return () => document.removeEventListener("scroll", onScroll, true);
  }, [node, storageKey]);

  useEffect(() => {
    if (!node || !ready || restored) return;
    setRestored(true);
    let top = 0;
    try {
      top = Number(sessionStorage.getItem(storageKey) ?? 0) || 0;
    } catch {
      top = 0;
    }
    if (top > 0) window.requestAnimationFrame(() => scrollParent(node).scrollTo({ top }));
  }, [node, ready, restored, storageKey]);

  return setNode;
}
