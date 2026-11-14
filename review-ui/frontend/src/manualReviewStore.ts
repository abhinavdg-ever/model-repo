/** Manual Review inputs, kept per browser so leaving the page or reloading
 * does not lose them. Files go to IndexedDB (a File survives a structured
 * clone); the source choices to localStorage. Storage can be missing or
 * blocked (private window): every call then quietly does nothing. */

/** none: the source is left out of the comparison. */
export type Source = "upload" | "db" | "none";
/** qa = QA Ground Truth, ground = Client Ground Truth, system = Pipeline Output. */
export type Side = "qa" | "ground" | "system";
type SourceSide = Exclude<Side, "qa">;

const DB_NAME = "advantmed_manual_review";
const STORE = "files";
// v2 (2026-11-14): Client Ground Truth defaults to the database, as the Pipeline does.
const SOURCE_KEY = "advantmed_manual_review_sources_v2";
const DEFAULT_SOURCES: Record<SourceSide, Source> = { ground: "db", system: "db" };

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function run<T>(mode: IDBTransactionMode, work: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await open();
  try {
    return await new Promise<T>((resolve, reject) => {
      const request = work(db.transaction(STORE, mode).objectStore(STORE));
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  } finally {
    db.close();
  }
}

export async function readFile(side: Side): Promise<File | null> {
  try {
    const value = await run("readonly", (store) => store.get(side));
    return value instanceof File ? value : null;
  } catch {
    return null;
  }
}

export async function writeFile(side: Side, file: File | null): Promise<void> {
  try {
    if (file) await run("readwrite", (store) => store.put(file, side));
    else await run("readwrite", (store) => store.delete(side));
  } catch {
    // Not kept: the file lasts until the page is left.
  }
}

export function readSources(): Record<SourceSide, Source> {
  try {
    const saved = JSON.parse(localStorage.getItem(SOURCE_KEY) ?? "{}") as Partial<Record<SourceSide, unknown>>;
    const pick = (side: SourceSide): Source => {
      const value = saved[side];
      return value === "upload" || value === "db" || value === "none" ? value : DEFAULT_SOURCES[side];
    };
    return { ground: pick("ground"), system: pick("system") };
  } catch {
    return { ...DEFAULT_SOURCES };
  }
}

export function writeSources(sources: Record<SourceSide, Source>): void {
  try {
    localStorage.setItem(SOURCE_KEY, JSON.stringify(sources));
  } catch {
    // Not kept: the choice lasts until the page is left.
  }
}
