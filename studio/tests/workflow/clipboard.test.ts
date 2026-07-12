/** Clipboard copy/cut/paste + multi-selection: id remapping, containers,
 * variable-ref rewriting, paste offsets, cross-workflow paste, history. */

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

import { clearClipboard } from '../../src/pages/workflows/model/clipboard';
import { useEditorStore } from '../../src/pages/workflows/store';

const initialState = useEditorStore.getState();

const state = () => useEditorStore.getState();

function addNode(type = 'llm', pos = { x: 200, y: 200 }): string {
  state().addNodeAtPosition(type, pos);
  return state().selection!.id;
}

/** Mark exactly `ids` as the React Flow selection. */
function selectNodes(ids: readonly string[]): void {
  useEditorStore.setState((s) => ({
    nodes: s.nodes.map((n) => ({ ...n, selected: ids.includes(n.id) })),
    multiSelection: [...ids],
  }));
}

const nodeById = (id: string) => state().nodes.find((n) => n.id === id)!;

beforeEach(() => {
  useEditorStore.setState(initialState, true);
  clearClipboard();
});

describe('copy/paste', () => {
  it('pastes remapped clones, keeps originals untouched and internal edges', () => {
    const a = addNode(); // llm_2 at (80, 164)
    const b = addNode('code', { x: 400, y: 200 }); // code_1
    state().connect({ source: a, target: b, sourceHandle: null, targetHandle: null });
    state().connect({ source: 'llm_1', target: a, sourceHandle: null, targetHandle: null });
    selectNodes([a, b]);
    const beforeNodes = state().nodes;
    const beforeEdges = state().edges;
    const beforePast = state().past.length;

    state().copySelection();
    expect(state().past).toHaveLength(beforePast); // copy records no history
    state().pasteClipboard();

    const pasted = state().multiSelection;
    expect(pasted).toEqual(['llm_3', 'code_2']);
    expect(state().nodes).toHaveLength(beforeNodes.length + 2);
    expect(state().selection).toEqual({ kind: 'node', id: 'code_2' });
    for (const id of pasted) expect(nodeById(id).selected).toBe(true);

    // Originals untouched (position + data), only deselected.
    const origA = beforeNodes.find((n) => n.id === a)!;
    expect(nodeById(a).position).toEqual(origA.position);
    expect(nodeById(a).data.dify).toEqual(origA.data.dify);
    expect(nodeById(a).selected).toBe(false);

    // Pasted clones sit at +40/+40.
    expect(nodeById('llm_3').position).toEqual({ x: 120, y: 204 });

    // The edge inside the selection is remapped; the boundary edge
    // (llm_1 -> a) is not copied.
    expect(state().edges).toHaveLength(beforeEdges.length + 1);
    expect(state().edges.some((e) => e.source === 'llm_3' && e.target === 'code_2')).toBe(true);
    expect(state().edges.filter((e) => e.source === 'llm_1')).toHaveLength(2); // template + manual
  });

  it('copies an iteration container together with its children', () => {
    addNode('iteration', { x: 400, y: 400 }); // iteration_1 at (280, 364)
    state().addNodeAtPosition('llm', { x: 500, y: 480 }); // child, relative (100, 80)
    const child = state().selection!.id;
    expect(nodeById(child).parentId).toBe('iteration_1');

    selectNodes(['iteration_1']); // container only — children come along
    state().copySelection();
    state().pasteClipboard();

    expect(state().multiSelection).toEqual(['iteration_2', 'llm_3']);
    const newContainer = nodeById('iteration_2');
    const newChild = nodeById('llm_3');
    expect(newContainer.position).toEqual({ x: 320, y: 404 }); // +40/+40
    expect(newChild.parentId).toBe('iteration_2');
    expect(newChild.extent).toBe('parent');
    expect(newChild.position).toEqual({ x: 100, y: 80 }); // container-relative, unshifted
    expect(newChild.data.dify['iteration_id']).toBe('iteration_2');
  });

  it('keeps a lone copied child inside its still-existing container', () => {
    addNode('iteration', { x: 400, y: 400 });
    state().addNodeAtPosition('llm', { x: 500, y: 480 });
    const child = state().selection!.id; // llm_2, relative (100, 80)

    selectNodes([child]);
    state().copySelection();
    state().pasteClipboard();

    const clone = nodeById(state().multiSelection[0]!);
    expect(clone.parentId).toBe('iteration_1');
    expect(clone.position).toEqual({ x: 140, y: 120 }); // relative +40/+40
    expect(clone.data.dify['iteration_id']).toBe('iteration_1');
  });

  it('rewrites {{#id.var#}} references that point into the copied set', () => {
    const a = addNode(); // llm_2
    const b = addNode(); // llm_3
    state().updateNodeData(b, {
      ...nodeById(b).data.dify,
      prompt_template: [{ role: 'user', text: `{{#${a}.text#}} and {{#llm_1.text#}}` }],
    });

    selectNodes([a, b]);
    state().copySelection();
    state().pasteClipboard();

    const [newA, newB] = state().multiSelection;
    expect(newA).toBe('llm_4');
    const prompt = nodeById(newB!).data.dify['prompt_template'] as { text: string }[];
    // Reference to the co-copied node follows the new id; the reference to
    // the uncopied llm_1 is left alone.
    expect(prompt[0]!.text).toBe(`{{#${newA}.text#}} and {{#llm_1.text#}}`);
  });

  it('offsets repeated pastes of the same content; re-copy resets the offset', () => {
    const a = addNode(); // (80, 164)
    selectNodes([a]);
    state().copySelection();

    state().pasteClipboard();
    const first = state().multiSelection[0]!;
    state().pasteClipboard();
    const second = state().multiSelection[0]!;
    expect(nodeById(first).position).toEqual({ x: 120, y: 204 }); // +40
    expect(nodeById(second).position).toEqual({ x: 160, y: 244 }); // +80

    selectNodes([a]);
    state().copySelection();
    state().pasteClipboard();
    expect(nodeById(state().multiSelection[0]!).position).toEqual({ x: 120, y: 204 });
  });

  it('pastes across workflows, detaching children of uncopied containers', () => {
    addNode('iteration', { x: 400, y: 400 }); // container at (280, 364)
    state().addNodeAtPosition('llm', { x: 500, y: 480 }); // child, relative (100, 80)
    const child = state().selection!.id;
    selectNodes([child]);
    state().copySelection();

    const firstId = state().activeId;
    state().createWorkflow();
    expect(state().activeId).not.toBe(firstId);
    expect(state().multiSelection).toEqual([]);

    state().pasteClipboard();
    const pasted = state().multiSelection;
    expect(pasted).toEqual(['llm_2']); // remapped against the new document
    const clone = nodeById('llm_2');
    expect(clone.parentId).toBeUndefined();
    expect(clone.data.dify['iteration_id']).toBeUndefined();
    expect(clone.position).toEqual({ x: 420, y: 484 }); // absolute (380, 444) +40
  });

  it('paste is undoable', () => {
    const a = addNode();
    selectNodes([a]);
    state().copySelection();
    const before = state();

    state().pasteClipboard();
    expect(state().past).toHaveLength(before.past.length + 1);

    state().undo();
    expect(state().nodes).toBe(before.nodes);
    expect(state().edges).toBe(before.edges);
    expect(state().multiSelection).toEqual([]); // pasted ids pruned
  });

  it('cut removes the selection, is undoable, and pastes back', () => {
    const a = addNode();
    const total = state().nodes.length;
    selectNodes([a]);

    state().cutSelection();
    expect(state().nodes.some((n) => n.id === a)).toBe(false);
    expect(state().nodes).toHaveLength(total - 1);
    expect(state().multiSelection).toEqual([]);

    state().undo();
    expect(state().nodes.some((n) => n.id === a)).toBe(true);

    state().pasteClipboard(); // the cut content is on the clipboard
    expect(state().nodes).toHaveLength(total + 1);
    expect(state().multiSelection).toEqual(['llm_3']);
  });
});

describe('multi-selection state', () => {
  it('setMultiSelection stores ids and deleteNodes prunes them', () => {
    const a = addNode();
    const b = addNode();
    state().setMultiSelection([a, b]);
    expect(state().multiSelection).toEqual([a, b]);
    state().deleteNodes([a]);
    expect(state().multiSelection).toEqual([b]);
  });

  it('selectAll marks every node selected', () => {
    addNode();
    state().selectAll();
    const all = state().nodes;
    expect(all.every((n) => n.selected === true)).toBe(true);
    expect(state().multiSelection).toEqual(all.map((n) => n.id));
    expect(state().selection).toEqual({ kind: 'node', id: all[all.length - 1]!.id });
  });

  it('duplicateNodes clones the given nodes in place, undoably', () => {
    const a = addNode();
    const b = addNode('code', { x: 400, y: 200 });
    state().connect({ source: a, target: b, sourceHandle: null, targetHandle: null });
    const nodeCount = state().nodes.length;
    const edgeCount = state().edges.length;

    state().duplicateNodes([a, b]);
    expect(state().nodes).toHaveLength(nodeCount + 2);
    expect(state().edges).toHaveLength(edgeCount + 1);
    const [newA, newB] = state().multiSelection;
    expect(state().edges.some((e) => e.source === newA && e.target === newB)).toBe(true);
    expect(nodeById(newA!).position).toEqual({ x: 120, y: 204 }); // +40/+40

    state().undo();
    expect(state().nodes).toHaveLength(nodeCount);
    expect(state().edges).toHaveLength(edgeCount);
  });
});
