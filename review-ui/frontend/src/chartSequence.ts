import { listFolders, type FolderListParams } from "./api";

/** The chart after this one in the list the chart was opened from: the Accuracy
 * View's filtered and sorted rows, or the History list with its saved search,
 * filters and sort (also the fallback for a chart opened by URL). */

export type ChartRef = { id: string; name: string };

const ACCURACY_ORDER_KEY = "advantmed_accuracy_chart_order";

/** Called when the Accuracy View opens a chart. sessionStorage, so a tab opened
 * from it (window.open) starts with a copy. */
export function saveAccuracyOrder(charts: ChartRef[]): void {
  try {
    sessionStorage.setItem(ACCURACY_ORDER_KEY, JSON.stringify(charts));
  } catch {
    // Blocked storage: Next falls back to the History order.
  }
}

function readAccuracyOrder(): ChartRef[] | null {
  try {
    const raw = sessionStorage.getItem(ACCURACY_ORDER_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    return Array.isArray(parsed) ? (parsed as ChartRef[]) : null;
  } catch {
    return null;
  }
}

function after(charts: ChartRef[], current: string): ChartRef | null {
  const index = charts.findIndex((chart) => chart.id === current);
  return index >= 0 && index + 1 < charts.length ? charts[index + 1] : null;
}

export async function nextChart(
  current: string,
  from: "accuracy" | undefined,
  historyParams: FolderListParams,
): Promise<ChartRef | null> {
  if (from === "accuracy") {
    const order = readAccuracyOrder();
    if (order?.some((chart) => chart.id === current)) return after(order, current);
  }
  const list = await listFolders(historyParams);
  return after(
    list.items.map((folder) => ({ id: folder.id, name: folder.name })),
    current,
  );
}
