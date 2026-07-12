/**
 * Workflow library persistence (localStorage only, no backend).
 * All reads/writes are guarded: a missing or corrupt storage never throws.
 */

const LIBRARY_KEY = 'ragspine-studio.workflow-library';
const ACTIVE_KEY = 'ragspine-studio.workflow-active';
const RUN_INPUTS_KEY = 'ragspine-studio.workflow-run-inputs';
const FOLD_KEY = 'ragspine-studio.workflow-fold';

export interface LibraryEntry {
  id: string;
  name: string;
  /** ISO timestamp of the last save. */
  updatedAt: string;
  /** Serialized workflow YAML (the tested canonical form). */
  yaml: string;
}

export function readJson(key: string): unknown {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? undefined : (JSON.parse(raw) as unknown);
  } catch {
    return undefined;
  }
}

export function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable or full — persistence is best-effort */
  }
}

function isLibraryEntry(value: unknown): value is LibraryEntry {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v['id'] === 'string' &&
    typeof v['name'] === 'string' &&
    typeof v['updatedAt'] === 'string' &&
    typeof v['yaml'] === 'string'
  );
}

export function loadLibrary(): LibraryEntry[] {
  const raw = readJson(LIBRARY_KEY);
  return Array.isArray(raw) ? raw.filter(isLibraryEntry) : [];
}

export function saveLibrary(entries: readonly LibraryEntry[]): void {
  writeJson(LIBRARY_KEY, entries);
}

export function loadActiveId(): string | null {
  try {
    return localStorage.getItem(ACTIVE_KEY);
  } catch {
    return null;
  }
}

export function saveActiveId(id: string): void {
  try {
    localStorage.setItem(ACTIVE_KEY, id);
  } catch {
    /* best-effort */
  }
}

export function newEntryId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `wf-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  }
}

/* ----------------------- last-used run inputs ----------------------- */

function loadAllRunInputs(): Record<string, Record<string, unknown>> {
  const raw = readJson(RUN_INPUTS_KEY);
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return {};
  const out: Record<string, Record<string, unknown>> = {};
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
      out[key] = value as Record<string, unknown>;
    }
  }
  return out;
}

export function loadRunInputs(workflowId: string): Record<string, unknown> {
  return loadAllRunInputs()[workflowId] ?? {};
}

export function saveRunInputs(workflowId: string, inputs: Record<string, unknown>): void {
  const all = loadAllRunInputs();
  all[workflowId] = inputs;
  writeJson(RUN_INPUTS_KEY, all);
}

export function deleteRunInputs(workflowId: string): void {
  const all = loadAllRunInputs();
  if (workflowId in all) {
    delete all[workflowId];
    writeJson(RUN_INPUTS_KEY, all);
  }
}

/* --------------------- fold answer/question toggle ------------------ */

export function loadFold(): boolean {
  const raw = readJson(FOLD_KEY);
  return typeof raw === 'boolean' ? raw : true;
}

export function saveFold(value: boolean): void {
  writeJson(FOLD_KEY, value);
}
