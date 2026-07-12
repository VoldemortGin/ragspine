/**
 * THE round-trip gate: for every backend fixture, import -> export ->
 * re-import must preserve every original field byte-for-byte (semantically),
 * only ever ADDING defaults (positions, edge id/targetHandle).
 */

import { dump } from 'js-yaml';
import { describe, expect, it } from 'vitest';

import { parseWorkflowYaml, serializeWorkflowYaml, WorkflowParseError } from '../../src/workflow/convert';
import { nodeRegistry } from '../../src/workflow/registry';
import type { StudioNodeData, StudioWorkflow } from '../../src/workflow/types';
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

describe('round-trip: five new node types (inline)', () => {
  const originalText = dump({
    app: { mode: 'workflow', name: 'five-new-types' },
    kind: 'app',
    version: '0.1.5',
    workflow: {
      graph: {
        nodes: [
          {
            id: 'start_1',
            position: { x: 0, y: 0 },
            data: {
              type: 'start',
              title: '开始',
              variables: [{ variable: 'query', label: 'Query', type: 'text-input', required: true }],
            },
          },
          {
            id: 'http_1',
            position: { x: 200, y: 0 },
            data: {
              type: 'http-request',
              title: 'HTTP 请求',
              method: 'post',
              url: 'https://api.example.com/search',
              headers: 'Content-Type: application/json',
              params: '',
              authorization: { type: 'api-key', config: { type: 'bearer', api_key: 'test-key' } },
              body: { type: 'json', data: [{ type: 'text', value: '{"q": "{{#start_1.query#}}"}' }] },
              timeout: { max_connect_timeout: 0, max_read_timeout: 0, max_write_timeout: 0 },
              ssl_verify: true,
              variables: [],
              retry_config: { retry_enabled: true, max_retries: 3, retry_interval: 100 },
            },
          },
          {
            id: 'agg_1',
            position: { x: 400, y: 0 },
            data: {
              type: 'variable-aggregator',
              title: '变量聚合',
              output_type: 'string',
              variables: [['http_1', 'body']],
              advanced_settings: {
                group_enabled: true,
                groups: [
                  {
                    group_name: 'g1',
                    groupId: 'group-id-1',
                    output_type: 'string',
                    variables: [['http_1', 'body']],
                  },
                  {
                    group_name: 'g2',
                    groupId: 'group-id-2',
                    output_type: 'string',
                    variables: [['http_1', 'status_code']],
                  },
                ],
              },
            },
          },
          {
            id: 'assigner_1',
            position: { x: 600, y: 0 },
            data: {
              type: 'assigner',
              title: '变量赋值',
              version: '2',
              items: [
                {
                  variable_selector: ['conversation', 'memory'],
                  input_type: 'variable',
                  operation: 'over-write',
                  value: ['http_1', 'body'],
                },
              ],
            },
          },
          {
            id: 'doc_1',
            position: { x: 800, y: 0 },
            data: {
              type: 'document-extractor',
              title: '文档提取',
              variable_selector: ['start_1', 'file'],
              is_array_file: false,
            },
          },
          {
            id: 'loop_1',
            position: { x: 1000, y: 0 },
            width: 720,
            height: 360,
            data: {
              type: 'loop',
              title: '循环',
              loop_count: 5,
              break_conditions: [
                {
                  id: 'bc-1',
                  varType: 'string',
                  variable_selector: ['loop_llm', 'text'],
                  comparison_operator: 'contains',
                  value: 'DONE',
                },
              ],
              logical_operator: 'and',
              loop_variables: [
                { id: 'lv-1', label: 'acc', var_type: 'string', value_type: 'constant', value: '' },
              ],
              start_node_id: 'loop_start_1',
            },
          },
          {
            id: 'loop_llm',
            position: { x: 60, y: 60 },
            data: {
              type: 'llm',
              title: '循环体 LLM',
              loop_id: 'loop_1',
              isInLoop: true,
              model: { provider: 'anthropic', name: 'claude', completion_params: {} },
              prompt_template: [{ role: 'user', text: '{{#loop_1.acc#}}' }],
            },
          },
          {
            id: 'loop_assigner',
            position: { x: 320, y: 60 },
            data: {
              type: 'assigner',
              title: '写回累积值',
              version: '2',
              loop_id: 'loop_1',
              isInLoop: true,
              items: [
                {
                  variable_selector: ['loop_1', 'acc'],
                  input_type: 'variable',
                  operation: 'over-write',
                  value: ['loop_llm', 'text'],
                },
              ],
            },
          },
        ],
        edges: [
          { source: 'start_1', target: 'http_1' },
          { source: 'http_1', target: 'agg_1' },
          { source: 'agg_1', target: 'assigner_1' },
          { source: 'assigner_1', target: 'doc_1' },
          { source: 'doc_1', target: 'loop_1' },
          { source: 'loop_llm', target: 'loop_assigner' },
        ],
      },
    },
  });
  const original = loadDoc(originalText);
  const wf = parseWorkflowYaml(originalText);
  const exportedText = serializeWorkflowYaml(wf);
  const exported = loadDoc(exportedText);
  const wf2 = parseWorkflowYaml(exportedText);

  it('re-imported node data deep-equals the original raw yaml data', () => {
    for (const rawNode of rawNodes(original)) {
      const id = rawNode.id as string;
      expect(nodeById(wf2, id).data).toEqual(rawNode.data);
    }
  });

  it('parents loop children to the container across the round trip', () => {
    for (const id of ['loop_llm', 'loop_assigner']) {
      expect(nodeById(wf, id).parentId).toBe('loop_1');
      expect(nodeById(wf2, id).parentId).toBe('loop_1');
    }
    expect(nodeById(wf2, 'loop_1').parentId).toBeUndefined();
  });

  it('exported doc is a lossless superset of the original doc', () => {
    assertDeepSubset(original, exported);
  });

  it('keeps unmodeled fields alive across the round trip', () => {
    expect(nodeById(wf2, 'loop_llm').data.isInLoop).toBe(true);
    expect(nodeById(wf2, 'loop_assigner').data.isInLoop).toBe(true);
    const advanced = nodeById(wf2, 'agg_1').data['advanced_settings'] as {
      groups: { groupId: string }[];
    };
    expect(advanced.groups.map((g) => g.groupId)).toEqual(['group-id-1', 'group-id-2']);
    expect(nodeById(wf2, 'loop_1').passthrough.width).toBe(720);
    expect(nodeById(wf2, 'loop_1').passthrough.height).toBe(360);
  });
});

describe('registry defaults round-trip for the five new types', () => {
  const NEW_TYPES = [
    'http-request',
    'variable-aggregator',
    'assigner',
    'document-extractor',
    'loop',
  ] as const;

  function singleNodeWorkflow(data: StudioNodeData): StudioWorkflow {
    return {
      name: 't',
      mode: 'workflow',
      version: '0.1.5',
      appPassthrough: {},
      docPassthrough: {},
      workflowPassthrough: {},
      graphPassthrough: {},
      nodes: [{ id: 'n1', type: data.type, position: { x: 0, y: 0 }, data, passthrough: {} }],
      edges: [],
    };
  }

  for (const type of NEW_TYPES) {
    it(`${type}: createDefaultData survives serialize -> parse unchanged`, () => {
      const wf = singleNodeWorkflow(nodeRegistry[type].createDefaultData());
      const wf2 = parseWorkflowYaml(serializeWorkflowYaml(wf));
      expect(nodeById(wf2, 'n1').data).toEqual(nodeRegistry[type].createDefaultData());
    });
  }
});

describe('serialization loop_id policy', () => {
  const loopDoc = (): string =>
    dump({
      app: { mode: 'workflow', name: 't' },
      kind: 'app',
      version: '0.1.5',
      workflow: {
        graph: {
          nodes: [
            {
              id: 'loop_a',
              data: {
                type: 'loop',
                loop_count: 2,
                break_conditions: [],
                logical_operator: 'and',
                loop_variables: [],
              },
            },
            {
              id: 'loop_b',
              data: {
                type: 'loop',
                loop_count: 3,
                break_conditions: [],
                logical_operator: 'and',
                loop_variables: [],
              },
            },
            { id: 'iter_a', data: { type: 'iteration', iterator_selector: [], output_selector: [] } },
            { id: 'child_loop', data: { type: 'llm', loop_id: 'loop_a' } },
            { id: 'child_iter', data: { type: 'llm', iteration_id: 'iter_a' } },
          ],
          edges: [],
        },
      },
    });

  const rawDataOf = (wf: StudioWorkflow, id: string): Record<string, unknown> => {
    const raw = rawNodes(loadDoc(serializeWorkflowYaml(wf))).find((n) => n.id === id);
    if (raw === undefined) throw new Error(`node "${id}" missing in exported doc`);
    return raw.data as Record<string, unknown>;
  };

  it('drops data.loop_id when the node was detached from its loop container', () => {
    const wf = parseWorkflowYaml(loopDoc());
    expect(nodeById(wf, 'child_loop').parentId).toBe('loop_a');
    const detached: StudioWorkflow = {
      ...wf,
      nodes: wf.nodes.map((n) => {
        if (n.id !== 'child_loop') return n;
        const { parentId: _parentId, ...rest } = n;
        return rest;
      }),
    };
    expect(rawDataOf(detached, 'child_loop')['loop_id']).toBeUndefined();
  });

  it('overwrites data.loop_id when parentId moved to another loop container', () => {
    const wf = parseWorkflowYaml(loopDoc());
    const moved: StudioWorkflow = {
      ...wf,
      nodes: wf.nodes.map((n) => (n.id === 'child_loop' ? { ...n, parentId: 'loop_b' } : n)),
    };
    expect(rawDataOf(moved, 'child_loop')['loop_id']).toBe('loop_b');
  });

  it('replaces iteration_id with loop_id when the node moved into a loop container', () => {
    const wf = parseWorkflowYaml(loopDoc());
    expect(nodeById(wf, 'child_iter').parentId).toBe('iter_a');
    const moved: StudioWorkflow = {
      ...wf,
      nodes: wf.nodes.map((n) => (n.id === 'child_iter' ? { ...n, parentId: 'loop_a' } : n)),
    };
    const data = rawDataOf(moved, 'child_iter');
    expect(data['loop_id']).toBe('loop_a');
    expect(data['iteration_id']).toBeUndefined();
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
