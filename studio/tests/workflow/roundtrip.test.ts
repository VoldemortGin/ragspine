/**
 * THE round-trip gate: for every backend fixture, import -> export ->
 * re-import must preserve every original field byte-for-byte (semantically),
 * only ever ADDING defaults (positions, edge id/targetHandle).
 */

import { dump } from 'js-yaml';
import { describe, expect, it } from 'vitest';

import { parseWorkflowYaml, serializeWorkflowYaml, WorkflowParseError } from '../../src/workflow/convert';
import type { StudioWorkflow } from '../../src/workflow/types';
import {
  assertDeepSubset,
  FIXTURE_NAMES,
  loadDoc,
  loadFixtureText,
  rawEdges,
  rawNodes,
} from './helpers';

function edgeKey(edge: { source: string; target: string; sourceHandle: string; targetHandle: string }): string {
  return `${edge.source} --${edge.sourceHandle}/${edge.targetHandle}--> ${edge.target}`;
}

function nodeById(wf: StudioWorkflow, id: string) {
  const node = wf.nodes.find((n) => n.id === id);
  if (node === undefined) throw new Error(`node "${id}" not found`);
  return node;
}

for (const name of FIXTURE_NAMES) {
  describe(`round-trip: ${name}`, () => {
    const text = loadFixtureText(name);
    const original = loadDoc(text);
    const wf = parseWorkflowYaml(text);
    const exportedText = serializeWorkflowYaml(wf);
    const exported = loadDoc(exportedText);
    const wf2 = parseWorkflowYaml(exportedText);

    it('preserves the node id set', () => {
      const originalIds = rawNodes(original).map((n) => n.id);
      expect(wf.nodes.map((n) => n.id).sort()).toEqual([...originalIds].sort());
      expect(wf2.nodes.map((n) => n.id).sort()).toEqual([...originalIds].sort());
    });

    it('re-imported node data deep-equals the original raw yaml data', () => {
      for (const rawNode of rawNodes(original)) {
        const id = rawNode.id as string;
        expect(nodeById(wf2, id).data).toEqual(rawNode.data);
      }
    });

    it('preserves parentId across the round trip', () => {
      for (const node of wf.nodes) {
        expect(nodeById(wf2, node.id).parentId).toBe(node.parentId);
      }
    });

    it('preserves the edge set on (source, target, sourceHandle, targetHandle)', () => {
      const originalKeys = rawEdges(original)
        .map((e) =>
          edgeKey({
            source: e.source as string,
            target: e.target as string,
            sourceHandle: typeof e.sourceHandle === 'string' ? e.sourceHandle : 'source',
            targetHandle: typeof e.targetHandle === 'string' ? e.targetHandle : 'target',
          }),
        )
        .sort();
      expect(wf.edges.map(edgeKey).sort()).toEqual(originalKeys);
      expect(wf2.edges.map(edgeKey).sort()).toEqual(originalKeys);
    });

    it('exported doc is a lossless superset of the original doc', () => {
      assertDeepSubset(original, exported);
    });

    it('keeps Chinese text unescaped in the exported yaml', () => {
      expect(exportedText).toContain('开始');
      expect(exportedText).not.toContain('\\u');
    });

    it('assigns unique edge ids and positions to all nodes', () => {
      expect(new Set(wf.edges.map((e) => e.id)).size).toBe(wf.edges.length);
      for (const node of wf.nodes) {
        expect(Number.isFinite(node.position.x)).toBe(true);
        expect(Number.isFinite(node.position.y)).toBe(true);
      }
    });
  });
}

describe('fixture-specific spot checks', () => {
  it('iteration: iter_llm is parented to iter_1 and unknown fields survive', () => {
    const wf = parseWorkflowYaml(loadFixtureText('iteration'));
    expect(nodeById(wf, 'iter_llm').parentId).toBe('iter_1');

    const wf2 = parseWorkflowYaml(serializeWorkflowYaml(wf));
    expect(nodeById(wf2, 'iter_llm').parentId).toBe('iter_1');
    expect(nodeById(wf2, 'iter_llm').data.iteration_id).toBe('iter_1');
    const iterData = nodeById(wf2, 'iter_1').data;
    expect(iterData.output_type).toBe('array[string]');
    expect(iterData.start_node_id).toBe('iter_llm');
    expect(iterData.is_parallel).toBe(true);
    expect(iterData.parallel_nums).toBe(5);
  });

  it('knowledge: multiple_retrieval_config.top_k === 3 survives un-normalized', () => {
    const wf2 = parseWorkflowYaml(serializeWorkflowYaml(parseWorkflowYaml(loadFixtureText('knowledge'))));
    const data = nodeById(wf2, 'kr_1').data;
    expect(data.multiple_retrieval_config).toEqual({ top_k: 3 });
    expect(data.top_k).toBeUndefined();
  });

  it('branch: keeps the true/false branch sourceHandles', () => {
    const wf = parseWorkflowYaml(loadFixtureText('branch'));
    const handles = wf.edges.filter((e) => e.source === 'ifelse_1').map((e) => e.sourceHandle);
    expect(handles.sort()).toEqual(['false', 'true']);
  });

  it('parallel: keeps all 5 edges', () => {
    const wf = parseWorkflowYaml(loadFixtureText('parallel'));
    expect(wf.edges).toHaveLength(5);
    expect(parseWorkflowYaml(serializeWorkflowYaml(wf)).edges).toHaveLength(5);
  });
});

describe('edge id policy', () => {
  const minimalDoc = (edges: unknown[]): string =>
    dump({
      app: { mode: 'workflow', name: 't' },
      kind: 'app',
      version: '0.1.5',
      workflow: {
        graph: {
          nodes: [
            { id: 'a', data: { type: 'start' } },
            { id: 'b', data: { type: 'end' } },
          ],
          edges,
        },
      },
    });

  it('generates deterministic ids with numeric suffixes on collision', () => {
    const wf = parseWorkflowYaml(
      minimalDoc([
        { source: 'a', target: 'b' },
        { source: 'a', target: 'b' },
      ]),
    );
    expect(wf.edges.map((e) => e.id)).toEqual(['a__source__b', 'a__source__b__2']);
  });

  it('never collides with explicit edge ids', () => {
    const wf = parseWorkflowYaml(
      minimalDoc([
        { id: 'a__source__b', source: 'a', target: 'b', sourceHandle: 'source' },
        { source: 'a', target: 'b' },
      ]),
    );
    expect(wf.edges.map((e) => e.id)).toEqual(['a__source__b', 'a__source__b__2']);
  });
});

describe('serialization iteration_id policy', () => {
  it('drops data.iteration_id when the node was detached from its container', () => {
    const wf = parseWorkflowYaml(loadFixtureText('iteration'));
    const detached: StudioWorkflow = {
      ...wf,
      nodes: wf.nodes.map((n) => {
        if (n.id !== 'iter_llm') return n;
        const { parentId: _parentId, ...rest } = n;
        return rest;
      }),
    };
    const doc = loadDoc(serializeWorkflowYaml(detached));
    const llm = rawNodes(doc).find((n) => n.id === 'iter_llm');
    expect(llm).toBeDefined();
    expect((llm?.data as Record<string, unknown>)['iteration_id']).toBeUndefined();
  });

  it('overwrites data.iteration_id when parentId changed', () => {
    const wf = parseWorkflowYaml(loadFixtureText('iteration'));
    const moved: StudioWorkflow = {
      ...wf,
      nodes: wf.nodes.map((n) => (n.id === 'iter_llm' ? { ...n, parentId: 'iter_other' } : n)),
    };
    const doc = loadDoc(serializeWorkflowYaml(moved));
    const llm = rawNodes(doc).find((n) => n.id === 'iter_llm');
    expect((llm?.data as Record<string, unknown>)['iteration_id']).toBe('iter_other');
  });
});

describe('parse errors', () => {
  it('rejects unparseable yaml', () => {
    expect(() => parseWorkflowYaml('a: [1, 2')).toThrow(WorkflowParseError);
  });

  it('rejects a non-mapping document root', () => {
    expect(() => parseWorkflowYaml('42')).toThrow(WorkflowParseError);
  });

  it('rejects a missing app section', () => {
    expect(() => parseWorkflowYaml(dump({ kind: 'app', workflow: { graph: {} } }))).toThrow(/app/);
  });

  it('rejects an unsupported app.mode', () => {
    expect(() =>
      parseWorkflowYaml(dump({ app: { mode: 'chat', name: 'x' }, workflow: { graph: {} } })),
    ).toThrow(/mode/);
  });

  it('rejects a missing workflow.graph', () => {
    expect(() =>
      parseWorkflowYaml(dump({ app: { mode: 'workflow', name: 'x' }, workflow: {} })),
    ).toThrow(/graph/);
  });

  it('rejects a node without an id', () => {
    const doc = dump({
      app: { mode: 'workflow', name: 'x' },
      workflow: { graph: { nodes: [{ data: { type: 'start' } }], edges: [] } },
    });
    expect(() => parseWorkflowYaml(doc)).toThrow(/id/);
  });

  it('rejects a node without data.type', () => {
    const doc = dump({
      app: { mode: 'workflow', name: 'x' },
      workflow: { graph: { nodes: [{ id: 'a', data: { title: 't' } }], edges: [] } },
    });
    expect(() => parseWorkflowYaml(doc)).toThrow(/data\.type/);
  });

  it('rejects an edge without a source', () => {
    const doc = dump({
      app: { mode: 'workflow', name: 'x' },
      workflow: {
        graph: { nodes: [{ id: 'a', data: { type: 'start' } }], edges: [{ target: 'a' }] },
      },
    });
    expect(() => parseWorkflowYaml(doc)).toThrow(/source/);
  });
});
