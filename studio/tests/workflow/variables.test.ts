/** Upstream-variable model: upstream traversal, per-type outputs, ref validation. */

import { describe, expect, it } from 'vitest';

import {
  availableVariables,
  nodeOutputVariables,
  upstreamNodes,
  validateVariableRefs,
} from '../../src/pages/workflows/model/variables';
import type { VariableEdge, VariableNode } from '../../src/pages/workflows/model/variables';
import { parseWorkflowYaml } from '../../src/workflow/convert';
import { loadFixtureText } from './helpers';

function node(
  id: string,
  type: string,
  extra: Record<string, unknown> = {},
  parentId?: string,
): VariableNode {
  return { id, ...(parentId !== undefined ? { parentId } : {}), data: { type, ...extra } };
}

function edge(source: string, target: string): VariableEdge {
  return { source, target };
}

function ids(nodes: VariableNode[]): string[] {
  return nodes.map((n) => n.id);
}

describe('upstreamNodes', () => {
  const linearNodes = [node('start_1', 'start'), node('llm_1', 'llm'), node('answer_1', 'answer')];
  const linearEdges = [edge('start_1', 'llm_1'), edge('llm_1', 'answer_1')];

  it('walks a linear chain backwards', () => {
    expect(ids(upstreamNodes(linearNodes, linearEdges, 'answer_1'))).toEqual(['start_1', 'llm_1']);
    expect(ids(upstreamNodes(linearNodes, linearEdges, 'llm_1'))).toEqual(['start_1']);
    expect(ids(upstreamNodes(linearNodes, linearEdges, 'start_1'))).toEqual([]);
  });

  it('only sees the own branch, and both branches after a join', () => {
    const nodes = [
      node('start_1', 'start'),
      node('if_1', 'if-else'),
      node('llm_a', 'llm'),
      node('llm_b', 'llm'),
      node('tpl_1', 'template-transform'),
    ];
    const edges = [
      edge('start_1', 'if_1'),
      edge('if_1', 'llm_a'),
      edge('if_1', 'llm_b'),
      edge('llm_a', 'tpl_1'),
      edge('llm_b', 'tpl_1'),
    ];
    expect(ids(upstreamNodes(nodes, edges, 'llm_a'))).toEqual(['start_1', 'if_1']);
    expect(ids(upstreamNodes(nodes, edges, 'tpl_1'))).toEqual([
      'start_1',
      'if_1',
      'llm_a',
      'llm_b',
    ]);
  });

  it('terminates on cycles and never includes the node itself', () => {
    const nodes = [node('a', 'llm'), node('b', 'llm')];
    const edges = [edge('a', 'b'), edge('b', 'a')];
    expect(ids(upstreamNodes(nodes, edges, 'a'))).toEqual(['b']);
  });

  it('gives iteration children the container and its upstream, not vice versa', () => {
    const wf = parseWorkflowYaml(loadFixtureText('iteration'));
    // Child sees the container itself plus everything upstream of it.
    expect(ids(upstreamNodes(wf.nodes, wf.edges, 'iter_llm'))).toEqual(['start_1', 'iter_1']);
    // Downstream of the container sees the container but not its children.
    expect(ids(upstreamNodes(wf.nodes, wf.edges, 'end_1'))).toEqual(['start_1', 'iter_1']);
    expect(ids(upstreamNodes(wf.nodes, wf.edges, 'iter_1'))).toEqual(['start_1']);
  });
});

describe('nodeOutputVariables', () => {
  it('start: declared variables plus sys.query/sys.files, defensively', () => {
    expect(
      nodeOutputVariables(node('s', 'start', { variables: [{ variable: 'q' }, { variable: 'f' }] })),
    ).toEqual(['q', 'f', 'sys.query', 'sys.files']);
    expect(nodeOutputVariables(node('s', 'start'))).toEqual(['sys.query', 'sys.files']);
    expect(
      nodeOutputVariables(node('s', 'start', { variables: [{ label: 'no name' }, 'junk', null] })),
    ).toEqual(['sys.query', 'sys.files']);
  });

  it('llm exposes text', () => {
    expect(nodeOutputVariables(node('l', 'llm'))).toEqual(['text']);
  });

  it('code exposes its outputs keys (empty/missing outputs tolerated)', () => {
    expect(nodeOutputVariables(node('c', 'code', { outputs: { result: {}, extra: {} } }))).toEqual([
      'result',
      'extra',
    ]);
    expect(nodeOutputVariables(node('c', 'code'))).toEqual([]);
    expect(nodeOutputVariables(node('c', 'code', { outputs: 'nope' }))).toEqual([]);
  });

  it('parameter-extractor exposes parameter names plus __is_success/__reason', () => {
    expect(
      nodeOutputVariables(node('p', 'parameter-extractor', { parameters: [{ name: 'city' }] })),
    ).toEqual(['city', '__is_success', '__reason']);
    expect(nodeOutputVariables(node('p', 'parameter-extractor'))).toEqual([
      '__is_success',
      '__reason',
    ]);
  });

  it('fixed single-output types', () => {
    expect(nodeOutputVariables(node('q', 'question-classifier'))).toEqual(['class_name']);
    expect(nodeOutputVariables(node('k', 'knowledge-retrieval'))).toEqual(['result']);
    expect(nodeOutputVariables(node('t', 'template-transform'))).toEqual(['output']);
    expect(nodeOutputVariables(node('i', 'iteration'))).toEqual(['output']);
  });

  it('open-output types return no variables', () => {
    expect(nodeOutputVariables(node('t', 'tool'))).toEqual([]);
    expect(nodeOutputVariables(node('e', 'end'))).toEqual([]);
    expect(nodeOutputVariables(node('a', 'answer'))).toEqual([]);
    expect(nodeOutputVariables(node('x', 'http-request'))).toEqual([]);
    expect(nodeOutputVariables(node('u', 'totally-unknown'))).toEqual([]);
  });
});

describe('availableVariables', () => {
  it('lists upstream variables with titles, falling back to the node id', () => {
    const wf = parseWorkflowYaml(loadFixtureText('qa_fold'));
    const forLlm = availableVariables(wf.nodes, wf.edges, 'llm_1');
    expect(forLlm).toContainEqual({
      nodeId: 'start_1',
      nodeTitle: '开始',
      nodeType: 'start',
      variable: 'question',
    });
    expect(forLlm).toContainEqual({
      nodeId: 'kr_1',
      nodeTitle: '知识检索',
      nodeType: 'knowledge-retrieval',
      variable: 'result',
    });
    // llm_1 itself and its downstream answer_1 are not referenceable.
    expect(forLlm.some((v) => v.nodeId === 'llm_1' || v.nodeId === 'answer_1')).toBe(false);

    const untitled = [node('n1', 'llm'), node('n2', 'answer')];
    const vars = availableVariables(untitled, [edge('n1', 'n2')], 'n2');
    expect(vars).toEqual([{ nodeId: 'n1', nodeTitle: 'n1', nodeType: 'llm', variable: 'text' }]);
  });

  it('exposes item/index for ancestor containers and output outside them', () => {
    const wf = parseWorkflowYaml(loadFixtureText('iteration'));
    const inside = availableVariables(wf.nodes, wf.edges, 'iter_llm');
    const iterVars = inside.filter((v) => v.nodeId === 'iter_1').map((v) => v.variable);
    expect(iterVars).toEqual(['item', 'index']);
    expect(inside.filter((v) => v.nodeId === 'start_1').map((v) => v.variable)).toEqual([
      'items',
      'sys.query',
      'sys.files',
    ]);

    const outside = availableVariables(wf.nodes, wf.edges, 'end_1');
    expect(outside.filter((v) => v.nodeId === 'iter_1').map((v) => v.variable)).toEqual(['output']);
  });

  it('marks open-output upstream nodes with an empty variable entry', () => {
    const wf = parseWorkflowYaml(loadFixtureText('agent_tool'));
    const forEnd = availableVariables(wf.nodes, wf.edges, 'end_1');
    expect(forEnd).toContainEqual({
      nodeId: 'tool_1',
      nodeTitle: '天气工具',
      nodeType: 'tool',
      variable: '',
    });
  });
});

describe('validateVariableRefs', () => {
  const wf = parseWorkflowYaml(loadFixtureText('qa_fold'));
  const forLlm = availableVariables(wf.nodes, wf.edges, 'llm_1');
  const forAnswer = availableVariables(wf.nodes, wf.edges, 'answer_1');

  it('accepts valid refs (fixture prompts validate cleanly)', () => {
    expect(validateVariableRefs('根据资料回答：{{#start_1.question#}}', forLlm)).toEqual([]);
    expect(validateVariableRefs('{{#kr_1.result#}} and {{# start_1.question #}}', forLlm)).toEqual(
      [],
    );
    expect(validateVariableRefs('{{#llm_1.text#}}', forAnswer)).toEqual([]);
  });

  it('flags refs to nodes that are not upstream', () => {
    expect(validateVariableRefs('{{#ghost.text#}}', forLlm)).toEqual([
      { ref: '{{#ghost.text#}}', nodeId: 'ghost', variable: 'text', reason: 'unknown-node' },
    ]);
    // llm_1 referencing its own downstream answer node.
    expect(validateVariableRefs('{{#answer_1.answer#}}', forLlm)).toEqual([
      {
        ref: '{{#answer_1.answer#}}',
        nodeId: 'answer_1',
        variable: 'answer',
        reason: 'unknown-node',
      },
    ]);
  });

  it('flags unknown variables on nodes with a known output set', () => {
    expect(validateVariableRefs('{{#kr_1.text#}}', forLlm)).toEqual([
      { ref: '{{#kr_1.text#}}', nodeId: 'kr_1', variable: 'text', reason: 'unknown-variable' },
    ]);
  });

  it('is lenient for open-output nodes, sys/env namespaces and bare node refs', () => {
    const toolWf = parseWorkflowYaml(loadFixtureText('agent_tool'));
    const forEnd = availableVariables(toolWf.nodes, toolWf.edges, 'end_1');
    expect(validateVariableRefs('{{#tool_1.anything.goes#}}', forEnd)).toEqual([]);
    expect(validateVariableRefs('{{#sys.query#}} {{#env.API_KEY#}}', forLlm)).toEqual([]);
    expect(validateVariableRefs('{{#kr_1#}}', forLlm)).toEqual([]); // bare ref, node exists
    expect(validateVariableRefs('{{#ghost#}}', forLlm)).toHaveLength(1);
  });

  it('validates iteration item refs inside the container only', () => {
    const iterWf = parseWorkflowYaml(loadFixtureText('iteration'));
    const inside = availableVariables(iterWf.nodes, iterWf.edges, 'iter_llm');
    const outside = availableVariables(iterWf.nodes, iterWf.edges, 'end_1');
    expect(validateVariableRefs('翻译这一项：{{#iter_1.item#}}', inside)).toEqual([]);
    expect(validateVariableRefs('{{#iter_1.item#}}', outside)).toEqual([
      { ref: '{{#iter_1.item#}}', nodeId: 'iter_1', variable: 'item', reason: 'unknown-variable' },
    ]);
    expect(validateVariableRefs('{{#iter_1.output#}}', outside)).toEqual([]);
  });

  it('reports duplicate failing refs once and ignores non-ref braces', () => {
    expect(validateVariableRefs('{{#ghost.x#}} {{#ghost.x#}}', forLlm)).toHaveLength(1);
    expect(validateVariableRefs('Jinja {{ variable }} and {} braces', forLlm)).toEqual([]);
    expect(validateVariableRefs('', forLlm)).toEqual([]);
  });
});
