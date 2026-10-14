import { listFolders, type FolderListParams } from "./api";

/** The chart after this one in the list the chart was opened from: the Accuracy
 * View's or Manual Review's filtered and sorted rows, or the History list with
 * its saved search, filters and sort (also the fallback for a chart opened by URL). */

export type ChartRef = { id: string; name: string };
export type ChartSource = "accuracy" | "manual-review";

const orderKey = (source: ChartSource) => `advantmed_${source}_chart_order`;

/** Called when a list opens a chart. sessionStorage, so a tab opened from it
 * (window.open) starts with a copy. */
export function saveChartOrder(source: ChartSource, charts: ChartRef[]): void {
  try {
    sessionStorage.setItem(orderKey(source), JSON.stringify(charts));
  } catch {
    // Blocked storage: Next falls back to the History order.
  }
}

export function saveAccuracyOrder(charts: ChartRef[]): void {
  saveChartOrder("accuracy", charts);
}

function readChartOrder(source: ChartSource): ChartRef[] | null {
  try {
    const raw = sessionStorage.getItem(orderKey(source));
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
  from: ChartSource | undefined,
  historyParams: FolderListParams,
): Promise<ChartRef | null> {
  if (from) {
    const order = readChartOrder(from);
    if (order?.some((chart) => chart.id === current)) return after(order, current);
  }
  const list = await listFolders(historyParams);
  return after(
    list.items.map((folder) => ({ id: folder.id, name: folder.name })),
    current,
  );
}
