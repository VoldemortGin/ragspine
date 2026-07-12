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

/** Runtime guard for one trace object from the wire (tolerant on IO/error). */
export function isNodeTrace(value: unknown): value is NodeTrace {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v['index'] === 'number' &&
    typeof v['node_id'] === 'string' &&
    typeof v['title'] === 'string' &&
    typeof v['node_type'] === 'string' &&
    typeof v['status'] === 'string' &&
    TRACE_STATUSES.includes(v['status']) &&
    typeof v['elapsed_ms'] === 'number'
  );
}

/** Narrow an unknown payload field to a trace array (null when absent). */
export function toNodeTraces(value: unknown): NodeTrace[] | null {
  return Array.isArray(value) ? value.filter(isNodeTrace) : null;
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
}

function historyKey(workflowId: string): string {
  return `${RUN_HISTORY_KEY}:${workflowId}`;
}

function isRunHistoryEntry(value: unknown): value is RunHistoryEntry {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v['at'] === 'string' && (v['status'] === 'succeeded' || v['status'] === 'failed')
  );
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
  return Array.isArray(raw) ? raw.filter(isRunHistoryEntry) : [];
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
export function executionFromHistory(workflowId: string): ExecutionState {
  const latest = loadRunHistory(workflowId)[0];
  if (latest === undefined) return IDLE_EXECUTION;
  return {
    status: latest.status,
    traces: tracesToMap(latest.node_traces),
    startedAt: latest.at,
    finishedAt: latest.at,
    ...(latest.error !== undefined ? { error: latest.error } : {}),
  };
}
