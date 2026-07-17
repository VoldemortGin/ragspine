/**
 * Workflow execution state: node traces from /v1/dify/run keyed by canvas
 * node id, plus a per-workflow run history in localStorage (last 10 runs).
 *
 * Execution state is transient UI state — it never enters the undo/redo
 * history and is never written into the persisted workflow YAML.
 */

import { ApiError } from '../../../api/client';
import type { NodeTrace, NodeTraceStatus } from '../../../api/types';
import { readJson, writeJson } from './library';

export type ExecutionStatus = 'idle' | 'running' | 'succeeded' | 'failed';

export interface ExecutionState {
  status: ExecutionStatus;
  /** Latest trace per canvas node id (nodes without a trace stay unstyled). */
  traces: Record<string, NodeTrace>;
  startedAt?: string;
  finishedAt?: string;
  /** Run-level error message (failed runs only). */
  error?: string;
}

export const IDLE_EXECUTION: ExecutionState = { status: 'idle', traces: {} };

export type RunFailure = { kind: 'api'; error: unknown } | { kind: 'job'; message: string };

/** Modal-facing lifecycle of the in-flight or most recent run attempt. */
export type RunSlice =
  | { name: 'idle' }
  | { name: 'running' }
  | { name: 'job'; jobId: string; jobState: 'queued' | 'started' }
  | {
      name: 'result';
      result: unknown;
      warnings: string[];
      requestId: string | null;
      traces: NodeTrace[] | null;
    }
  | { name: 'error'; failure: RunFailure; traces: NodeTrace[] | null };

/* --------------------------- trace normalization ------------------------ */

const TRACE_STATUSES: readonly string[] = ['succeeded', 'failed', 'skipped'];

/** Strict runtime guard for one already-normalized trace object. */
export function isNodeTrace(value: unknown): value is NodeTrace {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v['index'] === 'number' &&
    Number.isInteger(v['index']) &&
    v['index'] >= 0 &&
    typeof v['node_id'] === 'string' &&
    typeof v['title'] === 'string' &&
    typeof v['node_type'] === 'string' &&
    typeof v['status'] === 'string' &&
    TRACE_STATUSES.includes(v['status']) &&
    typeof v['elapsed_ms'] === 'number' &&
    Number.isFinite(v['elapsed_ms']) &&
    v['elapsed_ms'] >= 0 &&
    (v['inputs'] === null ||
      (typeof v['inputs'] === 'object' && v['inputs'] !== null && !Array.isArray(v['inputs']))) &&
    (v['outputs'] === null ||
      (typeof v['outputs'] === 'object' &&
        v['outputs'] !== null &&
        !Array.isArray(v['outputs']))) &&
    (v['error'] === null || typeof v['error'] === 'string')
  );
}

function normalizeNodeTrace(value: unknown): NodeTrace | null {
  if (typeof value !== 'object' || value === null) return null;
  const v = value as Record<string, unknown>;
  if (
    typeof v['index'] !== 'number' ||
    !Number.isInteger(v['index']) ||
    v['index'] < 0 ||
    typeof v['node_id'] !== 'string' ||
    typeof v['title'] !== 'string' ||
    typeof v['node_type'] !== 'string' ||
    typeof v['status'] !== 'string' ||
    !TRACE_STATUSES.includes(v['status']) ||
    typeof v['elapsed_ms'] !== 'number' ||
    !Number.isFinite(v['elapsed_ms']) ||
    v['elapsed_ms'] < 0
  ) {
    return null;
  }
  const mappingOrNull = (payload: unknown): Record<string, unknown> | null =>
    typeof payload === 'object' && payload !== null && !Array.isArray(payload)
      ? (payload as Record<string, unknown>)
      : null;
  return {
    index: v['index'],
    node_id: v['node_id'],
    title: v['title'],
    node_type: v['node_type'],
    status: v['status'] as NodeTraceStatus,
    elapsed_ms: v['elapsed_ms'],
    inputs: mappingOrNull(v['inputs']),
    outputs: mappingOrNull(v['outputs']),
    error: typeof v['error'] === 'string' ? v['error'] : null,
  };
}

/** Normalize an unknown payload field to safe trace records (null when absent). */
export function toNodeTraces(value: unknown): NodeTrace[] | null {
  if (!Array.isArray(value)) return null;
  return value
    .map(normalizeNodeTrace)
    .filter((trace): trace is NodeTrace => trace !== null);
}

/** Normalize a trace array into a per-node-id map (later entries win). */
export function tracesToMap(traces: readonly NodeTrace[] | null | undefined): Record<string, NodeTrace> {
  const map: Record<string, NodeTrace> = {};
  if (traces == null) return map;
  for (const trace of traces) {
    if (trace.node_id !== '') map[trace.node_id] = trace;
  }
  return map;
}

/** Stable execution order used by the timeline and completed-run replay. */
export function orderedNodeTraces(
  traces: readonly NodeTrace[] | null | undefined,
): NodeTrace[] {
  if (traces == null) return [];
  return traces
    .map((trace, position) => ({ trace, position }))
    .sort((a, b) => a.trace.index - b.trace.index || a.position - b.position)
    .map(({ trace }) => trace);
}

/**
 * Visible trace map for one completed-run replay frame. `throughStep` is a
 * zero-based position in execution order; null shows the completed run.
 */
export function traceMapThroughStep(
  traces: readonly NodeTrace[] | null | undefined,
  throughStep: number | null,
): Record<string, NodeTrace> {
  const ordered = orderedNodeTraces(traces);
  if (throughStep === null) return tracesToMap(ordered);
  if (!Number.isFinite(throughStep) || throughStep < 0) return {};
  return tracesToMap(ordered.slice(0, Math.floor(throughStep) + 1));
}

export interface TraceSummary {
  total: number;
  succeeded: number;
  failed: number;
  skipped: number;
  elapsedMs: number;
}

/** Run-level counts for the execution inspector. */
export function summarizeNodeTraces(
  traces: readonly NodeTrace[] | null | undefined,
): TraceSummary {
  const summary: TraceSummary = {
    total: 0,
    succeeded: 0,
    failed: 0,
    skipped: 0,
    elapsedMs: 0,
  };
  for (const trace of traces ?? []) {
    summary.total += 1;
    summary[trace.status] += 1;
    if (Number.isFinite(trace.elapsed_ms) && trace.elapsed_ms > 0) {
      summary.elapsedMs += trace.elapsed_ms;
    }
  }
  return summary;
}

/** node_traces from a failed run's HTTP 400 error body (top level or in error). */
export function nodeTracesFromError(error: unknown): NodeTrace[] | null {
  if (!(error instanceof ApiError)) return null;
  if (typeof error.body !== 'object' || error.body === null) return null;
  const body = error.body as Record<string, unknown>;
  const direct = toNodeTraces(body['node_traces']);
  if (direct !== null) return direct;
  const inner = body['error'];
  if (typeof inner === 'object' && inner !== null) {
    return toNodeTraces((inner as Record<string, unknown>)['node_traces']);
  }
  return null;
}

/** Parse an async dify-run job result ({ result, warnings, node_traces }). */
export function parseJobRunResult(value: unknown): {
  result: unknown;
  warnings: string[];
  node_traces: NodeTrace[] | null;
} {
  if (typeof value === 'object' && value !== null && 'result' in value) {
    const v = value as Record<string, unknown>;
    return {
      result: v['result'],
      warnings: Array.isArray(v['warnings'])
        ? v['warnings'].filter((w): w is string => typeof w === 'string')
        : [],
      node_traces: toNodeTraces(v['node_traces']),
    };
  }
  return { result: value, warnings: [], node_traces: null };
}

/** "85ms" under a second, "1.2s" from there up. */
export function formatElapsedMs(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '';
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** Badge variant per trace status (shared by panel + modal). */
export const TRACE_BADGE_VARIANT: Record<NodeTraceStatus, 'success' | 'danger' | 'neutral'> = {
  succeeded: 'success',
  failed: 'danger',
  skipped: 'neutral',
};

/* ------------------------------ run history ----------------------------- */

const RUN_HISTORY_KEY = 'ragspine-studio.workflow-run-history';
const RUN_HISTORY_LIMIT = 10;
/** Entries above this serialized size keep trace summaries only (no node IO). */
const RUN_HISTORY_MAX_CHARS = 200_000;

export interface RunHistoryEntry {
  /** ISO timestamp of when the run started. */
  at: string;
  status: 'succeeded' | 'failed';
  inputs: Record<string, unknown>;
  result: unknown;
  warnings: string[];
  node_traces: NodeTrace[] | null;
  /** Run-level error message (failed runs). */
  error?: string;
  /** True when node IO was dropped to fit the storage budget. */
  tracesSummarized?: boolean;
  /** Fingerprint of the serialized workflow that produced this run. */
  workflowFingerprint?: string;
}

function historyKey(workflowId: string): string {
  return `${RUN_HISTORY_KEY}:${workflowId}`;
}

function normalizeRunHistoryEntry(value: unknown): RunHistoryEntry | null {
  if (typeof value !== 'object' || value === null) return null;
  const v = value as Record<string, unknown>;
  if (
    typeof v['at'] !== 'string' ||
    (v['status'] !== 'succeeded' && v['status'] !== 'failed')
  ) {
    return null;
  }
  const inputs =
    typeof v['inputs'] === 'object' && v['inputs'] !== null && !Array.isArray(v['inputs'])
      ? (v['inputs'] as Record<string, unknown>)
      : {};
  const error = typeof v['error'] === 'string' ? v['error'] : undefined;
  const workflowFingerprint =
    typeof v['workflowFingerprint'] === 'string' ? v['workflowFingerprint'] : undefined;
  return {
    at: v['at'],
    status: v['status'],
    inputs,
    result: v['result'] ?? null,
    warnings: Array.isArray(v['warnings'])
      ? v['warnings'].filter((warning): warning is string => typeof warning === 'string')
      : [],
    node_traces: v['node_traces'] === null ? null : toNodeTraces(v['node_traces']),
    ...(error !== undefined ? { error } : {}),
    ...(v['tracesSummarized'] === true ? { tracesSummarized: true } : {}),
    ...(workflowFingerprint !== undefined ? { workflowFingerprint } : {}),
  };
}

/** Small deterministic identity for compatibility checks, not a security hash. */
export function workflowFingerprint(serializedWorkflow: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < serializedWorkflow.length; index += 1) {
    hash ^= serializedWorkflow.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return `wf-v1:${serializedWorkflow.length}:${(hash >>> 0).toString(16).padStart(8, '0')}`;
}

/** Strip per-node inputs/outputs, keeping the timeline (status + timing). */
function summarizeEntry(entry: RunHistoryEntry): RunHistoryEntry {
  return {
    ...entry,
    node_traces:
      entry.node_traces === null
        ? null
        : entry.node_traces.map((t) => ({ ...t, inputs: null, outputs: null })),
    tracesSummarized: true,
  };
}

/** Fit an entry into the per-entry budget; serialization failures degrade. */
function fitEntry(entry: RunHistoryEntry): RunHistoryEntry {
  try {
    if (JSON.stringify(entry).length <= RUN_HISTORY_MAX_CHARS) return entry;
  } catch {
    /* unserializable payload — fall through to the degraded forms */
  }
  const slim = summarizeEntry(entry);
  try {
    JSON.stringify(slim);
    return slim;
  } catch {
    // Even the summary fails to serialize (exotic result/inputs payload):
    // keep only the run outcome.
    return { ...slim, inputs: {}, result: null, node_traces: null };
  }
}

/** Most recent runs first. */
export function loadRunHistory(workflowId: string): RunHistoryEntry[] {
  const raw = readJson(historyKey(workflowId));
  if (!Array.isArray(raw)) return [];
  return raw
    .map(normalizeRunHistoryEntry)
    .filter((entry): entry is RunHistoryEntry => entry !== null)
    .slice(0, RUN_HISTORY_LIMIT);
}

export function appendRunHistory(workflowId: string, entry: RunHistoryEntry): void {
  const next = [fitEntry(entry), ...loadRunHistory(workflowId)].slice(0, RUN_HISTORY_LIMIT);
  writeJson(historyKey(workflowId), next);
}

export function deleteRunHistory(workflowId: string): void {
  try {
    localStorage.removeItem(historyKey(workflowId));
  } catch {
    /* best-effort */
  }
}

/** Execution slice reconstructed from a workflow's most recent stored run. */
export function executionFromHistory(
  workflowId: string,
  currentWorkflowFingerprint?: string,
): ExecutionState {
  const latest = loadRunHistory(workflowId)[0];
  if (latest === undefined) return IDLE_EXECUTION;
  if (
    currentWorkflowFingerprint !== undefined &&
    latest.workflowFingerprint !== currentWorkflowFingerprint
  ) {
    return IDLE_EXECUTION;
  }
  return {
    status: latest.status,
    traces: tracesToMap(latest.node_traces),
    startedAt: latest.at,
    finishedAt: latest.at,
    ...(latest.error !== undefined ? { error: latest.error } : {}),
  };
}

/** Execution slice for a stored run, optionally stopped at one replay step. */
export function executionFromRunHistory(
  entry: RunHistoryEntry,
  throughStep: number | null = null,
): ExecutionState {
  return {
    status: entry.status,
    traces: traceMapThroughStep(entry.node_traces, throughStep),
    startedAt: entry.at,
    finishedAt: entry.at,
    ...(entry.error !== undefined ? { error: entry.error } : {}),
  };
}
