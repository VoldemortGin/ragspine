/** Pure editor helpers: id generation and the new-workflow template. */

import { describe, expect, it } from 'vitest';

import { nodeIdPrefix, uniqueEdgeId, uniqueNodeId } from '../../src/pages/workflows/model/ids';
import type { LibraryEntry } from '../../src/pages/workflows/model/library';
import {
  copyName,
  createTemplateWorkflow,
  untitledName,
} from '../../src/pages/workflows/model/template';
import { parseWorkflowYaml, serializeWorkflowYaml } from '../../src/workflow/convert';
import { hasFinitePosition } from '../../src/workflow/layout';
import { asRecord, loadDoc, rawEdges, rawNodes } from './helpers';

function entry(name: string): LibraryEntry {
  return { id: name, name, updatedAt: new Date().toISOString(), yaml: '' };
}

describe('id generation', () => {
  it('sanitizes node type into an id prefix', () => {
    expect(nodeIdPrefix('llm')).toBe('llm');
    expect(nodeIdPrefix('if-else')).toBe('if_else');
    expect(nodeIdPrefix('Custom Type!')).toBe('custom_type');
    expect(nodeIdPrefix('***')).toBe('node');
  });

  it('generates type-prefixed counter ids avoiding collisions', () => {
    expect(uniqueNodeId('llm', [])).toBe('llm_1');
    expect(uniqueNodeId('llm', ['llm_1', 'llm_2', 'start_1'])).toBe('llm_3');
    expect(uniqueNodeId('if-else', ['if_else_1'])).toBe('if_else_2');
  });

  it('generates edge ids in the convert.ts style, collision-safe', () => {
    expect(uniqueEdgeId('a', 'source', 'b', [])).toBe('a__source__b');
    expect(uniqueEdgeId('a', 'source', 'b', ['a__source__b'])).toBe('a__source__b__2');
    expect(uniqueEdgeId('a', 'true', 'b', ['a__source__b'])).toBe('a__true__b');
  });
});

describe('workflow template', () => {
  it('creates start -> llm -> end with finite positions', () => {
    const wf = createTemplateWorkflow('My workflow');
    expect(wf.name).toBe('My workflow');
    expect(wf.mode).toBe('workflow');
    expect(wf.version).toBe('0.6.0');
    expect(wf.nodes.map((n) => n.type)).toEqual(['start', 'llm', 'end']);
    expect(wf.edges).toHaveLength(2);
    for (const node of wf.nodes) {
      expect(hasFinitePosition(node.position)).toBe(true);
    }
  });

  it('serializes to YAML that parses back losslessly', () => {
    const wf = createTemplateWorkflow('Round trip');
    const parsed = parseWorkflowYaml(serializeWorkflowYaml(wf));
    expect(parsed.name).toBe('Round trip');
    expect(parsed.nodes.map((n) => n.id)).toEqual(['start_1', 'llm_1', 'end_1']);
    expect(parsed.edges.map((e) => [e.source, e.target])).toEqual([
      ['start_1', 'llm_1'],
      ['llm_1', 'end_1'],
    ]);
    // The start node declares the "query" text input.
    const start = parsed.nodes[0]!;
    expect(start.data['variables']).toEqual([
      { variable: 'query', label: 'Query', type: 'text-input', required: true },
    ]);
  });

  it('exports a complete, editable Dify 0.6 document shape', () => {
    const doc = loadDoc(serializeWorkflowYaml(createTemplateWorkflow('Dify compatible')));
    expect(doc['version']).toBe('0.6.0');
    expect(doc['kind']).toBe('app');
    expect(doc['dependencies']).toEqual([
      {
        current_identifier: null,
        type: 'marketplace',
        value: {
          marketplace_plugin_unique_identifier:
            'langgenius/openai:0.3.8@592c8252795b5f75807de2d609a03196ed02596b409f7642b4a07548c7ff57ef',
        },
      },
    ]);

    const app = asRecord(doc['app'], 'app');
    expect(app).toMatchObject({
      description: 'Spine-authored editable Dify workflow.',
      icon: '🧩',
      icon_background: '#E4FBCC',
      mode: 'workflow',
      name: 'Dify compatible',
      use_icon_as_answer_icon: false,
    });

    const workflow = asRecord(doc['workflow'], 'workflow');
    expect(workflow['conversation_variables']).toEqual([]);
    expect(workflow['environment_variables']).toEqual([]);
    expect(workflow['features']).toEqual({});
    const graph = asRecord(workflow['graph'], 'workflow.graph');
    expect(graph['viewport']).toEqual({ x: 0, y: 0, zoom: 0.7 });

    for (const rawNode of rawNodes(doc)) {
      expect(rawNode['type']).toBe('custom');
      expect(rawNode['selected']).toBe(false);
      expect(rawNode['sourcePosition']).toBe('right');
      expect(rawNode['targetPosition']).toBe('left');
      expect(rawNode['width']).toBe(244);
      expect(rawNode['height']).toBe(90);
      expect(rawNode['positionAbsolute']).toEqual(rawNode['position']);
    }
    for (const rawEdge of rawEdges(doc)) {
      expect(rawEdge['type']).toBe('custom');
      expect(rawEdge['zIndex']).toBe(0);
      expect(asRecord(rawEdge['data'], 'edge.data')).toMatchObject({
        isInIteration: false,
        isInLoop: false,
      });
    }

    const llm = rawNodes(doc).find((rawNode) => rawNode['id'] === 'llm_1');
    if (llm === undefined) throw new Error('llm_1 missing');
    const llmData = asRecord(llm['data'], 'llm_1.data');
    expect(llmData['model']).toEqual({
      provider: 'langgenius/openai/openai',
      name: 'gpt-4o-mini',
      mode: 'chat',
      completion_params: {},
    });
    expect(llmData['context']).toEqual({
      enabled: false,
      variable_selector: [],
    });
  });
});

describe('library naming', () => {
  it('picks the next free untitled name', () => {
    expect(untitledName([])).toBe('Untitled workflow 1');
    expect(untitledName([entry('Untitled workflow 1')])).toBe('Untitled workflow 2');
    expect(untitledName([entry('Untitled workflow 2')])).toBe('Untitled workflow 1');
  });

  it('picks collision-free copy names', () => {
    expect(copyName('Flow', [])).toBe('Flow copy');
    expect(copyName('Flow', [entry('Flow copy')])).toBe('Flow copy 2');
    expect(copyName('Flow', [entry('Flow copy'), entry('Flow copy 2')])).toBe('Flow copy 3');
  });
});
