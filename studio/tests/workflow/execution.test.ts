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
  executionFromRunHistory,
  loadRunHistory,
  nodeTracesFromError,
  orderedNodeTraces,
  summarizeNodeTraces,
  traceMapThroughStep,
  toNodeTraces,
  tracesToMap,
  workflowFingerprint,
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

describe('trace wire normalization', () => {
  it('repairs unsafe payload slots and rejects invalid numeric fields', () => {
    expect(
      toNodeTraces([
        { ...trace(), inputs: ['bad'], outputs: 'bad', error: { message: 'bad' } },
        { ...trace({ node_id: 'nan' }), elapsed_ms: Number.NaN },
        { ...trace({ node_id: 'fractional' }), index: 1.5 },
      ]),
    ).toEqual([{ ...trace(), inputs: null, outputs: null, error: null }]);
  });
});

describe('completed-run replay helpers', () => {
  const traces = [
    trace({ node_id: 'end', index: 2, elapsed_ms: 7 }),
    trace({ node_id: 'start', index: 0, elapsed_ms: 3 }),
    trace({ node_id: 'branch', index: 1, status: 'skipped', elapsed_ms: 0 }),
  ];

  it('orders traces stably without mutating the wire response', () => {
    expect(orderedNodeTraces(traces).map((item) => item.node_id)).toEqual([
      'start',
      'branch',
      'end',
    ]);
    expect(traces[0]!.node_id).toBe('end');
  });

  it('builds an honest frame through the selected execution step', () => {
    expect(traceMapThroughStep(traces, -1)).toEqual({});
    expect(Object.keys(traceMapThroughStep(traces, 0))).toEqual(['start']);
    expect(Object.keys(traceMapThroughStep(traces, 1))).toEqual(['start', 'branch']);
    expect(Object.keys(traceMapThroughStep(traces, null))).toEqual(['start', 'branch', 'end']);
  });

  it('summarizes status counts and aggregate node time', () => {
    expect(summarizeNodeTraces(traces)).toEqual({
      total: 3,
      succeeded: 2,
      failed: 0,
      skipped: 1,
      elapsedMs: 10,
    });
  });

  it('reconstructs canvas execution state for a historical replay frame', () => {
    const execution = executionFromRunHistory(
      runEntry({ status: 'failed', error: 'boom', node_traces: traces }),
      0,
    );
    expect(execution.status).toBe('failed');
    expect(execution.error).toBe('boom');
    expect(Object.keys(execution.traces)).toEqual(['start']);
  });

  it('previews a historical frame in the store and restores the latest run', () => {
    const id = state().activeId;
    const fingerprint = workflowFingerprint(state().getYaml());
    const older = runEntry({
      at: '2026-07-09T00:00:00.000Z',
      node_traces: traces,
      workflowFingerprint: fingerprint,
    });
    const latestTrace = trace({ node_id: 'latest', index: 0 });
    appendRunHistory(id, older);
    appendRunHistory(
      id,
      runEntry({
        at: '2026-07-10T00:00:00.000Z',
        node_traces: [latestTrace],
        workflowFingerprint: fingerprint,
      }),
    );

    state().previewRunHistory(older, 0);
    expect(Object.keys(state().execution.traces)).toEqual(['start']);
    state().restoreLatestExecution();
    expect(Object.keys(state().execution.traces)).toEqual(['latest']);
  });

  it('refuses to project a run recorded from another workflow revision', () => {
    const incompatible = runEntry({
      node_traces: traces,
      workflowFingerprint: workflowFingerprint('a different workflow'),
    });
    state().previewRunHistory(incompatible, null);
    expect(state().execution).toEqual({ status: 'idle', traces: {} });
  });

  it('programmatic trace focus collapses an existing canvas multi-selection', () => {
    useEditorStore.setState((current) => ({
      nodes: current.nodes.map((node) =>
        node.id === 'start_1' || node.id === 'llm_1' ? { ...node, selected: true } : node,
      ),
      multiSelection: ['start_1', 'llm_1'],
      selection: { kind: 'node', id: 'llm_1' },
    }));

    state().selectSingleNode('start_1');
    expect(state().multiSelection).toEqual(['start_1']);
    expect(state().selection).toEqual({ kind: 'node', id: 'start_1' });
    expect(state().nodes.filter((node) => node.selected === true).map((node) => node.id)).toEqual([
      'start_1',
    ]);
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
  it('creates a stable workflow identity that changes with the document', () => {
    expect(workflowFingerprint('app: one')).toBe(workflowFingerprint('app: one'));
    expect(workflowFingerprint('app: one')).not.toBe(workflowFingerprint('app: two'));
  });

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

  it('normalizes damaged legacy entries before the inspector reads them', () => {
    const id = state().activeId;
    localStorage.setItem(
      `ragspine-studio.workflow-run-history:${id}`,
      JSON.stringify([
        {
          at: '2026-07-10T00:00:00.000Z',
          status: 'succeeded',
          inputs: ['not', 'a', 'mapping'],
          warnings: [1, 'kept'],
          node_traces: [{ invalid: true }, trace()],
        },
        { status: 'failed' },
      ]),
    );
    expect(loadRunHistory(id)).toEqual([
      {
        at: '2026-07-10T00:00:00.000Z',
        status: 'succeeded',
        inputs: {},
        result: null,
        warnings: ['kept'],
        node_traces: [trace()],
      },
    ]);
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
    expect(history[0]!.workflowFingerprint).toMatch(/^wf-v1:/);
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

  it('clears stale execution overlays when the canvas changes', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({ request_id: 'r3', result: 'ok', warnings: [], node_traces: [trace()] }),
      ),
    );
    state().addNodeAtPosition('llm', { x: 100, y: 100 });
    state().startRun('sync', {});
    await vi.waitFor(() => expect(state().execution.status).toBe('succeeded'));
    state().undo();
    expect(state().execution).toEqual({ status: 'idle', traces: {} });
    state().redo();
    expect(state().execution).toEqual({ status: 'idle', traces: {} });
  });

  it('does not project a completed run when the canvas changed in flight', async () => {
    let resolveResponse: ((response: Response) => void) | undefined;
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveResponse = resolve;
          }),
      ),
    );

    state().startRun('sync', {});
    expect(state().execution.status).toBe('running');
    state().addNodeAtPosition('template-transform', { x: 500, y: 100 });
    expect(state().execution.status).toBe('running');

    resolveResponse?.(
      jsonResponse({ request_id: 'changed', result: 'ok', warnings: [], node_traces: [trace()] }),
    );
    await vi.waitFor(() => expect(state().run.name).toBe('result'));
    expect(state().execution).toEqual({ status: 'idle', traces: {} });
    expect(loadRunHistory(state().activeId)[0]!.node_traces).toHaveLength(1);
  });
});

describe('per-workflow execution state', () => {
  it('loads the most recent stored run when switching workflows', () => {
    const firstId = state().activeId;
    const firstFingerprint = workflowFingerprint(state().getYaml());
    state().createWorkflow();
    const secondId = state().activeId;
    expect(secondId).not.toBe(firstId);

    appendRunHistory(
      firstId,
      runEntry({
        status: 'failed',
        error: 'boom',
        node_traces: [trace({ node_id: 'llm_9', status: 'failed' })],
        workflowFingerprint: firstFingerprint,
      }),
    );
    appendRunHistory(
      firstId,
      runEntry({
        node_traces: [trace({ node_id: 'llm_9' })],
        workflowFingerprint: firstFingerprint,
      }),
    );

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
