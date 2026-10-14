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
