/** Undo/redo history: snapshot timing, coalescing, stack limits, per-workflow reset. */

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

import { useEditorStore } from '../../src/pages/workflows/store';

const initialState = useEditorStore.getState();

const state = () => useEditorStore.getState();

function addNode(): void {
  state().addNodeAtPosition('llm', { x: 200, y: 200 });
}

beforeEach(() => {
  useEditorStore.setState(initialState, true);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('undo/redo basics', () => {
  it('undoes and redoes addNodeAtPosition', () => {
    const before = state();
    addNode();
    const after = state();
    expect(after.nodes).toHaveLength(before.nodes.length + 1);
    expect(after.past).toHaveLength(1);
    expect(after.future).toHaveLength(0);

    state().undo();
    expect(state().nodes).toBe(before.nodes);
    expect(state().edges).toBe(before.edges);
    expect(state().base).toBe(before.base);
    expect(state().past).toHaveLength(0);
    expect(state().future).toHaveLength(1);
    // The selection pointed at the (now undone) new node.
    expect(state().selection).toBeNull();
    expect(state().saveState).toBe('dirty');

    state().redo();
    expect(state().nodes).toBe(after.nodes);
    expect(state().past).toHaveLength(1);
    expect(state().future).toHaveLength(0);
  });

  it('is a no-op on empty stacks', () => {
    const before = state();
    state().undo();
    expect(state().nodes).toBe(before.nodes);
    state().redo();
    expect(state().nodes).toBe(before.nodes);
  });

  it('clears the redo stack on a new edit', () => {
    addNode();
    state().undo();
    expect(state().future).toHaveLength(1);
    addNode();
    expect(state().future).toHaveLength(0);
    expect(state().past).toHaveLength(1);
  });

  it('caps the undo stack at 100 snapshots, dropping the oldest', () => {
    const baseCount = state().nodes.length;
    for (let i = 0; i < 105; i += 1) addNode();
    expect(state().past).toHaveLength(100);
    // Adds 1..5 were dropped; the oldest kept snapshot precedes add #6.
    expect(state().past[0]!.nodes).toHaveLength(baseCount + 5);
  });
});

describe('updateNodeData coalescing', () => {
  it('coalesces rapid edits to the same field into one snapshot', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    addNode(); // structural snapshot: resets the coalescing window
    const id = state().selection!.id;
    const dify = () => state().nodes.find((n) => n.id === id)!.data.dify;
    const title0 = dify().title;

    state().updateNodeData(id, { ...dify(), title: 'a' });
    expect(state().past).toHaveLength(2);
    state().updateNodeData(id, { ...dify(), title: 'ab' });
    vi.advanceTimersByTime(300);
    state().updateNodeData(id, { ...dify(), title: 'abc' });
    expect(state().past).toHaveLength(2);

    // Undo restores the pre-edit value, not an intermediate keystroke.
    state().undo();
    expect(dify().title).toBe(title0);
  });

  it('starts a new snapshot when a different field changes', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    addNode();
    const id = state().selection!.id;
    const dify = () => state().nodes.find((n) => n.id === id)!.data.dify;

    state().updateNodeData(id, { ...dify(), title: 'a' });
    state().updateNodeData(id, { ...dify(), desc: 'note' });
    state().updateNodeData(id, { ...dify(), desc: 'notes' });
    expect(state().past).toHaveLength(3);
  });

  it('starts a new snapshot after the 800ms window expires', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    addNode();
    const id = state().selection!.id;
    const dify = () => state().nodes.find((n) => n.id === id)!.data.dify;

    state().updateNodeData(id, { ...dify(), title: 'a' });
    vi.advanceTimersByTime(900);
    state().updateNodeData(id, { ...dify(), title: 'ab' });
    expect(state().past).toHaveLength(3);
  });
});

describe('React Flow delete gesture (Canvas.onBeforeDelete contract)', () => {
  it('records one snapshot so keyboard remove changes are undoable', () => {
    addNode();
    const id = state().selection!.id;
    state().connect({ source: 'llm_1', target: id, sourceHandle: null, targetHandle: null });
    const before = state();
    const edgeId = before.edges.find((e) => e.target === id)!.id;

    // Canvas.onBeforeDelete calls recordSnapshot() once per gesture; React
    // Flow then applies the deletion as remove changes on both callbacks,
    // which bypass store.deleteNodes and record no history themselves.
    state().recordSnapshot();
    state().onNodesChange([{ type: 'remove', id }]);
    state().onEdgesChange([{ type: 'remove', id: edgeId }]);

    expect(state().nodes.some((n) => n.id === id)).toBe(false);
    expect(state().edges.some((e) => e.id === edgeId)).toBe(false);
    expect(state().past).toHaveLength(before.past.length + 1);

    state().undo();
    expect(state().nodes).toBe(before.nodes);
    expect(state().edges).toBe(before.edges);
  });
});

describe('per-workflow history', () => {
  it('clears history on createWorkflow and switchWorkflow', () => {
    const firstId = state().activeId;
    addNode();
    expect(state().past).toHaveLength(1);

    state().createWorkflow();
    expect(state().activeId).not.toBe(firstId);
    expect(state().past).toHaveLength(0);
    expect(state().future).toHaveLength(0);

    addNode();
    state().undo();
    expect(state().future).toHaveLength(1);

    state().switchWorkflow(firstId);
    expect(state().past).toHaveLength(0);
    expect(state().future).toHaveLength(0);
  });

  it('importYaml is undoable (history survives the document load)', () => {
    const yaml = state().getYaml();
    addNode();
    const withExtra = state().nodes;
    state().importYaml(yaml);
    expect(state().nodes).toHaveLength(withExtra.length - 1);
    expect(state().past).toHaveLength(2);

    state().undo();
    expect(state().nodes).toBe(withExtra);
  });
});
