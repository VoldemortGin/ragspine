/** Execution state: trace normalization, run lifecycle via the store,
 * run history persistence (push / cap / size degradation), per-workflow load. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The store module reads localStorage at import time (via model/library, which
// tolerates a missing storage) — stub an in-memory one before it is imported
// so persistence behaves identically on every node version.
vi.hoisted(() => {
  const data = new Map<string, string>();
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => data.get(key) ?? null,
      setItem: (key: string, value: string) => void data.set(key, value),
      removeItem: (key: string) => void data.delete(key),
      clear: () => data.clear(),
      key: (index: number) => [...data.keys()][index] ?? null,
      get length() {
        return data.size;
      },
    },
  });
});

import { ApiError } from '../../src/api/client';
import type { NodeTrace } from '../../src/api/types';
import {
  appendRunHistory,
  loadRunHistory,
  nodeTracesFromError,
  tracesToMap,
} from '../../src/pages/workflows/model/execution';
import type { RunHistoryEntry } from '../../src/pages/workflows/model/execution';
import { useEditorStore } from '../../src/pages/workflows/store';

const initialState = useEditorStore.getState();

const state = () => useEditorStore.getState();

function trace(overrides: Partial<NodeTrace> = {}): NodeTrace {
  return {
    index: 0,
    node_id: 'start_1',
    title: 'Start',
    node_type: 'start',
    status: 'succeeded',
    elapsed_ms: 12,
    inputs: null,
    outputs: null,
    error: null,
    ...overrides,
  };
}

function runEntry(overrides: Partial<RunHistoryEntry> = {}): RunHistoryEntry {
  return {
    at: '2026-07-10T00:00:00.000Z',
    status: 'succeeded',
    inputs: {},
    result: 'ok',
    warnings: [],
    node_traces: null,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  localStorage.clear();
  useEditorStore.setState(initialState, true);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('tracesToMap', () => {
  it('normalizes a trace array into a per-node-id map', () => {
    const t1 = trace({ node_id: 'a', index: 0 });
    const t2 = trace({ node_id: 'b', index: 1 });
    expect(tracesToMap([t1, t2])).toEqual({ a: t1, b: t2 });
  });

  it('lets later duplicates win and drops empty node ids', () => {
    const first = trace({ node_id: 'a', elapsed_ms: 1 });
    const second = trace({ node_id: 'a', elapsed_ms: 2 });
    const map = tracesToMap([first, trace({ node_id: '' }), second]);
    expect(Object.keys(map)).toEqual(['a']);
    expect(map['a']!.elapsed_ms).toBe(2);
  });

  it('returns an empty map for null/undefined', () => {
    expect(tracesToMap(null)).toEqual({});
    expect(tracesToMap(undefined)).toEqual({});
  });
});

describe('nodeTracesFromError', () => {
  it('extracts node_traces from either level of the 400 error body', () => {
    const traces = [trace()];
    const inError = new ApiError('boom', 400, 't', undefined, {
      error: { type: 't', message: 'boom', node_traces: traces },
    });
    const topLevel = new ApiError('boom', 400, 't', undefined, { node_traces: traces });
    expect(nodeTracesFromError(inError)).toEqual(traces);
    expect(nodeTracesFromError(topLevel)).toEqual(traces);
  });

  it('returns null for non-ApiError or bodies without traces', () => {
    expect(nodeTracesFromError(new Error('x'))).toBeNull();
    expect(nodeTracesFromError(new ApiError('x', 400, 't'))).toBeNull();
  });
});

describe('run history persistence', () => {
  it('pushes newest first and caps at 10 entries', () => {
    const id = state().activeId;
    for (let i = 0; i < 12; i += 1) {
      appendRunHistory(id, runEntry({ at: `2026-07-10T00:00:${String(i).padStart(2, '0')}.000Z` }));
    }
    const history = loadRunHistory(id);
    expect(history).toHaveLength(10);
    expect(history[0]!.at).toBe('2026-07-10T00:00:11.000Z');
    expect(history[9]!.at).toBe('2026-07-10T00:00:02.000Z');
  });

  it('summarizes node IO when an entry exceeds the size budget', () => {
    const id = state().activeId;
    const entry = runEntry({
      node_traces: [
        trace({ node_id: 'llm_1', inputs: { q: 'hi' }, outputs: { text: 'x'.repeat(250_000) } }),
      ],
    });
    appendRunHistory(id, entry);
    const stored = loadRunHistory(id)[0]!;
    expect(stored.tracesSummarized).toBe(true);
    expect(stored.node_traces).toHaveLength(1);
    expect(stored.node_traces![0]!.inputs).toBeNull();
    expect(stored.node_traces![0]!.outputs).toBeNull();
    expect(stored.node_traces![0]!.status).toBe('succeeded');
    // The original entry passed in is not mutated.
    expect(entry.node_traces![0]!.inputs).toEqual({ q: 'hi' });
  });

  it('tolerates unserializable payloads instead of throwing', () => {
    const id = state().activeId;
    const circular: Record<string, unknown> = {};
    circular['self'] = circular;
    expect(() => appendRunHistory(id, runEntry({ result: circular }))).not.toThrow();
    const stored = loadRunHistory(id)[0]!;
    expect(stored.status).toBe('succeeded');
    expect(stored.result).toBeNull();
  });
});

describe('startRun (sync, stubbed fetch)', () => {
  it('normalizes traces, records history, and stays out of the undo stacks', async () => {
    const traces = [
      trace({ node_id: 'start_1', index: 0 }),
      trace({ node_id: 'llm_1', index: 1, title: 'LLM', node_type: 'llm', elapsed_ms: 1234 }),
    ];
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({ request_id: 'r1', result: 'hi', warnings: ['w'], node_traces: traces }),
      ),
    );
    const pastBefore = state().past.length;
    state().startRun('sync', { q: 'x' });
    expect(state().execution.status).toBe('running');
    expect(state().run.name).toBe('running');

    await vi.waitFor(() => expect(state().execution.status).toBe('succeeded'));
    expect(state().execution.traces['llm_1']!.elapsed_ms).toBe(1234);
    expect(state().run).toMatchObject({ name: 'result', result: 'hi', requestId: 'r1' });
    // Execution state never enters the undo history.
    expect(state().past).toHaveLength(pastBefore);
    expect(state().future).toHaveLength(0);

    const history = loadRunHistory(state().activeId);
    expect(history).toHaveLength(1);
    expect(history[0]).toMatchObject({ status: 'succeeded', inputs: { q: 'x' }, result: 'hi' });
    expect(history[0]!.node_traces).toHaveLength(2);
  });

  it('surfaces node_traces from the HTTP 400 error body on failure', async () => {
    const failedTrace = trace({ node_id: 'llm_1', status: 'failed', error: 'exploded' });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          {
            error: {
              type: 'dify.run_failed',
              message: 'boom',
              request_id: 'r2',
              node_traces: [trace({ node_id: 'start_1' }), failedTrace],
            },
          },
          400,
        ),
      ),
    );
    state().startRun('sync', {});
    await vi.waitFor(() => expect(state().execution.status).toBe('failed'));
    expect(state().execution.traces['llm_1']!.status).toBe('failed');
    expect(state().execution.error).toBe('boom');
    expect(state().run.name).toBe('error');
    const history = loadRunHistory(state().activeId);
    expect(history[0]).toMatchObject({ status: 'failed', result: null, error: 'boom' });
    expect(history[0]!.node_traces).toHaveLength(2);
  });

  it('undo/redo of canvas edits never touches execution state', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({ request_id: 'r3', result: 'ok', warnings: [], node_traces: [trace()] }),
      ),
    );
    state().addNodeAtPosition('llm', { x: 100, y: 100 });
    state().startRun('sync', {});
    await vi.waitFor(() => expect(state().execution.status).toBe('succeeded'));
    const execution = state().execution;

    state().undo();
    expect(state().execution).toBe(execution);
    state().redo();
    expect(state().execution).toBe(execution);
  });
});

describe('per-workflow execution state', () => {
  it('loads the most recent stored run when switching workflows', () => {
    const firstId = state().activeId;
    state().createWorkflow();
    const secondId = state().activeId;
    expect(secondId).not.toBe(firstId);

    appendRunHistory(
      firstId,
      runEntry({
        status: 'failed',
        error: 'boom',
        node_traces: [trace({ node_id: 'llm_9', status: 'failed' })],
      }),
    );
    appendRunHistory(firstId, runEntry({ node_traces: [trace({ node_id: 'llm_9' })] }));

    state().switchWorkflow(firstId);
    // The newest entry wins (succeeded, not the older failed one).
    expect(state().execution.status).toBe('succeeded');
    expect(state().execution.traces['llm_9']!.status).toBe('succeeded');
    expect(state().run.name).toBe('idle');

    state().switchWorkflow(secondId);
    expect(state().execution.status).toBe('idle');
    expect(state().execution.traces).toEqual({});
  });

  it('deleteWorkflow drops the stored run history', () => {
    const firstId = state().activeId;
    state().createWorkflow();
    appendRunHistory(firstId, runEntry());
    expect(loadRunHistory(firstId)).toHaveLength(1);
    state().deleteWorkflow(firstId);
    expect(loadRunHistory(firstId)).toHaveLength(0);
  });
});
