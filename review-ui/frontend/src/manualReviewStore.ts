/** Manual Review inputs, kept per browser so leaving the page or reloading
 * does not lose them. Files go to IndexedDB (a File survives a structured
 * clone); the source choices to localStorage. Storage can be missing or
 * blocked (private window): every call then quietly does nothing. */

export type Source = "upload" | "db";
export type Side = "ground" | "system";

const DB_NAME = "advantmed_manual_review";
const STORE = "files";
const SOURCE_KEY = "advantmed_manual_review_sources";
const DEFAULT_SOURCES: Record<Side, Source> = { ground: "upload", system: "db" };

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

export function readSources(): Record<Side, Source> {
  try {
    const saved = JSON.parse(localStorage.getItem(SOURCE_KEY) ?? "{}") as Partial<Record<Side, unknown>>;
    const pick = (side: Side): Source =>
      saved[side] === "upload" || saved[side] === "db" ? saved[side] : DEFAULT_SOURCES[side];
    return { ground: pick("ground"), system: pick("system") };
  } catch {
    return { ...DEFAULT_SOURCES };
  }
}

export function writeSources(sources: Record<Side, Source>): void {
  try {
    localStorage.setItem(SOURCE_KEY, JSON.stringify(sources));
  } catch {
    // Not kept: the choice lasts until the page is left.
  }
}
