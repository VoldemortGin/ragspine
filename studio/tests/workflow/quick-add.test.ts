/** addNodeWithConnection: quick-add atomicity, one-step undo, drag direction. */

import { beforeEach, describe, expect, it, vi } from 'vitest';

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

import { useEditorStore } from '../../src/pages/workflows/store';

const initialState = useEditorStore.getState();

const state = () => useEditorStore.getState();

/** Id of the node created by the last add action (it becomes the selection). */
const addedId = () => {
  const sel = state().selection;
  if (sel === null || sel.kind !== 'node') throw new Error('expected a node selection');
  return sel.id;
};

beforeEach(() => {
  useEditorStore.setState(initialState, true);
});

// The template document is start_1 -> llm_1 -> end_1 (3 nodes, 2 edges).

describe('addNodeWithConnection', () => {
  it('adds the node and the edge atomically with a single snapshot', () => {
    const before = state();
    state().addNodeWithConnection(
      'code',
      { x: 600, y: 300 },
      { nodeId: 'llm_1', handleType: 'source', handleId: null },
    );

    const id = addedId();
    expect(state().nodes).toHaveLength(before.nodes.length + 1);
    expect(state().edges).toHaveLength(before.edges.length + 1);
    const edge = state().edges.find((e) => e.target === id);
    expect(edge).toMatchObject({
      source: 'llm_1',
      sourceHandle: 'source',
      target: id,
      targetHandle: 'target',
    });
    expect(state().past).toHaveLength(before.past.length + 1);
    expect(state().saveState).toBe('dirty');
  });

  it('one undo rolls back both the node and the edge', () => {
    const before = state();
    state().addNodeWithConnection(
      'code',
      { x: 600, y: 300 },
      { nodeId: 'llm_1', handleType: 'source', handleId: null },
    );

    state().undo();
    expect(state().nodes).toBe(before.nodes);
    expect(state().edges).toBe(before.edges);
    expect(state().past).toHaveLength(before.past.length);

    state().redo();
    expect(state().nodes).toHaveLength(before.nodes.length + 1);
    expect(state().edges).toHaveLength(before.edges.length + 1);
  });

  it('makes the new node the edge SOURCE when dragged out of a target handle', () => {
    state().addNodeWithConnection(
      'code',
      { x: 100, y: 300 },
      { nodeId: 'llm_1', handleType: 'target', handleId: 'target' },
    );

    const id = addedId();
    const edge = state().edges.find((e) => e.source === id);
    expect(edge).toMatchObject({
      source: id,
      sourceHandle: 'source',
      target: 'llm_1',
      targetHandle: 'target',
    });
  });

  it('keeps the dragged branch handle as the edge sourceHandle', () => {
    state().addNodeAtPosition('if-else', { x: 900, y: 300 });
    const branchId = addedId();
    state().addNodeWithConnection(
      'code',
      { x: 1200, y: 300 },
      { nodeId: branchId, handleType: 'source', handleId: 'false' },
    );

    const edge = state().edges.find((e) => e.source === branchId);
    expect(edge).toMatchObject({ sourceHandle: 'false', target: addedId() });
  });

  it('wires a reverse-dragged branching node from its first source handle', () => {
    state().addNodeWithConnection(
      'if-else',
      { x: 100, y: 300 },
      { nodeId: 'llm_1', handleType: 'target', handleId: 'target' },
    );

    const edge = state().edges.find((e) => e.source === addedId());
    // Default if-else data has one case with case_id 'true'.
    expect(edge).toMatchObject({ sourceHandle: 'true', target: 'llm_1' });
  });

  it('adds the node without an edge when the type cannot attach', () => {
    const before = state();
    // 'start' has no target handle, so a source-drag cannot point at it.
    state().addNodeWithConnection(
      'start',
      { x: 600, y: 500 },
      { nodeId: 'llm_1', handleType: 'source', handleId: null },
    );
    expect(state().nodes).toHaveLength(before.nodes.length + 1);
    expect(state().edges).toHaveLength(before.edges.length);
    expect(state().past).toHaveLength(before.past.length + 1);
  });

  it('drops the new node into an iteration container under the point', () => {
    state().addNodeAtPosition('iteration', { x: 1000, y: 1000 });
    const containerId = addedId();
    // addNodeAtPosition centers the container on the point (480x240 default).
    state().addNodeWithConnection(
      'code',
      { x: 1000, y: 1020 },
      { nodeId: 'llm_1', handleType: 'source', handleId: null },
    );

    const node = state().nodes.find((n) => n.id === addedId());
    expect(node?.parentId).toBe(containerId);
    expect(node?.extent).toBe('parent');
    const edge = state().edges.find((e) => e.target === addedId());
    expect(edge?.source).toBe('llm_1');
  });
});
