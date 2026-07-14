/** Node type registry: handle derivation, default data, fallback definition. */

import { describe, expect, it } from 'vitest';

import { getNodeDefinition, nodeRegistry } from '../../src/workflow/registry';
import { NODE_TYPES } from '../../src/workflow/types';
import type { IfElseNodeData, QuestionClassifierNodeData } from '../../src/workflow/types';

describe('source handle derivation', () => {
  it('if-else with a single "true" case yields [true, false]', () => {
    const data: IfElseNodeData = {
      type: 'if-else',
      cases: [{ case_id: 'true', logical_operator: 'and', conditions: [] }],
    };
    const handles = nodeRegistry['if-else'].getSourceHandles(data);
    expect(handles.map((h) => h.id)).toEqual(['true', 'false']);
    expect(handles[0]?.label).toBe('IF');
    expect(handles[1]?.label).toBe('ELSE');
  });

  it('if-else labels later cases as ELIF and keeps an explicit false case', () => {
    const data: IfElseNodeData = {
      type: 'if-else',
      cases: [
        { case_id: 'c1', logical_operator: 'and', conditions: [] },
        { case_id: 'c2', logical_operator: 'or', conditions: [] },
        { case_id: 'false', logical_operator: 'and', conditions: [] },
      ],
    };
    const handles = nodeRegistry['if-else'].getSourceHandles(data);
    expect(handles.map((h) => h.id)).toEqual(['c1', 'c2', 'false']);
    expect(handles.map((h) => h.label)).toEqual(['IF', 'ELIF 1', 'ELIF 2']);
  });

  it('question-classifier yields one handle per class labeled by name', () => {
    const data: QuestionClassifierNodeData = {
      type: 'question-classifier',
      classes: [
        { id: '1', name: 'Billing' },
        { id: '2', name: 'Support' },
      ],
    };
    const handles = nodeRegistry['question-classifier'].getSourceHandles(data);
    expect(handles).toEqual([
      { id: '1', label: 'Billing' },
      { id: '2', label: 'Support' },
    ]);
  });

  it('end has no source handles', () => {
    expect(nodeRegistry.end.getSourceHandles({ type: 'end' })).toEqual([]);
  });

  it('plain nodes expose a single source handle', () => {
    for (const type of ['llm', 'code', 'answer', 'iteration', 'tool'] as const) {
      const handles = nodeRegistry[type].getSourceHandles({ type });
      expect(handles.map((h) => h.id)).toEqual(['source']);
    }
  });
});

describe('default data', () => {
  it('every registry type creates default data with the correct type', () => {
    for (const type of NODE_TYPES) {
      const def = nodeRegistry[type];
      expect(def.type).toBe(type);
      const data = def.createDefaultData();
      expect(data.type).toBe(type);
      // Handles and summary must work on fresh default data.
      expect(Array.isArray(def.getSourceHandles(data))).toBe(true);
      expect(typeof def.summarize(data)).toBe('string');
    }
  });

  it('start defaults to one required query variable and no target handle', () => {
    const def = nodeRegistry.start;
    expect(def.hasTargetHandle).toBe(false);
    expect(def.createDefaultData().variables).toEqual([
      { variable: 'query', label: 'Query', type: 'text-input', required: true },
    ]);
  });

  it('llm defaults satisfy the Dify 0.6 graphon model contract', () => {
    const data = nodeRegistry.llm.createDefaultData();
    expect(data.model).toEqual({
      provider: 'langgenius/openai/openai',
      name: 'gpt-4o-mini',
      mode: 'chat',
      completion_params: {},
    });
    expect(data.context).toEqual({ enabled: false, variable_selector: [] });
    expect(data.prompt_template).toEqual([{ role: 'user', text: '' }]);
  });

  it('knowledge retrieval defaults to a complete multiple-retrieval config', () => {
    const data = nodeRegistry['knowledge-retrieval'].createDefaultData();
    expect(data.retrieval_mode).toBe('multiple');
    expect(data.multiple_retrieval_config).toEqual({
      top_k: 4,
      score_threshold: null,
      reranking_mode: 'reranking_model',
      reranking_enable: false,
    });
  });

  it('parameter extractor defaults satisfy the Dify 0.6 graphon model contract', () => {
    const data = nodeRegistry['parameter-extractor'].createDefaultData();
    expect(data.model).toEqual({
      provider: 'langgenius/openai/openai',
      name: 'gpt-4o-mini',
      mode: 'chat',
      completion_params: {},
    });
    expect(data.reasoning_mode).toBe('function_call');
  });

  it('if-else defaults to a single true case', () => {
    expect(nodeRegistry['if-else'].createDefaultData().cases).toEqual([
      { case_id: 'true', logical_operator: 'and', conditions: [] },
    ]);
  });

  it('question-classifier defaults to two classes', () => {
    expect(nodeRegistry['question-classifier'].createDefaultData().classes).toEqual([
      { id: '1', name: 'Class 1' },
      { id: '2', name: 'Class 2' },
    ]);
  });

  it('code defaults to a python3 main stub', () => {
    const data = nodeRegistry.code.createDefaultData();
    expect(data.code_language).toBe('python3');
    expect(data.code).toContain('def main(');
  });

  it('only iteration and loop are containers', () => {
    for (const type of NODE_TYPES) {
      expect(nodeRegistry[type].isContainer).toBe(type === 'iteration' || type === 'loop');
    }
  });
});

describe('newly added dify types', () => {
  const NEW_TYPES = [
    'http-request',
    'variable-aggregator',
    'assigner',
    'document-extractor',
    'loop',
  ] as const;

  it('registers real definitions, not the unknown-type fallback', () => {
    expect(getNodeDefinition('http-request').label).toBe('HTTP Request');
    expect(getNodeDefinition('variable-aggregator').label).toBe('Variable Aggregator');
    expect(getNodeDefinition('assigner').label).toBe('Variable Assigner');
    expect(getNodeDefinition('document-extractor').label).toBe('Document Extractor');
    expect(getNodeDefinition('loop').label).toBe('Loop');
    for (const type of NEW_TYPES) {
      expect(getNodeDefinition(type)).toBe(nodeRegistry[type]);
    }
  });

  it('categorizes http-request under tools, loop under flow, the rest under transform', () => {
    expect(nodeRegistry['http-request'].category).toBe('tools');
    expect(nodeRegistry['variable-aggregator'].category).toBe('transform');
    expect(nodeRegistry.assigner.category).toBe('transform');
    expect(nodeRegistry['document-extractor'].category).toBe('transform');
    expect(nodeRegistry.loop.category).toBe('flow');
  });

  it('only loop is a container; all five accept incoming edges', () => {
    for (const type of NEW_TYPES) {
      expect(nodeRegistry[type].isContainer).toBe(type === 'loop');
      expect(nodeRegistry[type].hasTargetHandle).toBe(true);
    }
  });

  it('default data yields a single source handle', () => {
    for (const type of NEW_TYPES) {
      const def = nodeRegistry[type];
      expect(def.getSourceHandles(def.createDefaultData()).map((h) => h.id)).toEqual(['source']);
    }
  });
});

describe('summaries', () => {
  it('llm summarizes to the model name', () => {
    expect(
      nodeRegistry.llm.summarize({
        type: 'llm',
        model: { name: 'claude-opus-4-8' },
      }),
    ).toBe('claude-opus-4-8');
  });

  it('if-else summarizes to the case count', () => {
    expect(
      nodeRegistry['if-else'].summarize({
        type: 'if-else',
        cases: [{ case_id: 'true', logical_operator: 'and', conditions: [] }],
      }),
    ).toBe('1 case');
  });

  it('knowledge-retrieval reads top_k from either location', () => {
    const def = nodeRegistry['knowledge-retrieval'];
    expect(
      def.summarize({
        type: 'knowledge-retrieval',
        top_k: 7,
        dataset_ids: ['a'],
      }),
    ).toBe('k=7, 1 dataset');
    expect(
      def.summarize({
        type: 'knowledge-retrieval',
        multiple_retrieval_config: { top_k: 3 },
        dataset_ids: ['a', 'b'],
      }),
    ).toBe('k=3, 2 datasets');
    expect(def.summarize({ type: 'knowledge-retrieval' })).toBe('k=4, 0 datasets');
  });

  it('tool summarizes to the tool name', () => {
    expect(nodeRegistry.tool.summarize({ type: 'tool', tool_name: 'get_weather' })).toBe(
      'get_weather',
    );
  });
});

describe('unknown type fallback', () => {
  it('returns a generic definition for unknown types', () => {
    const def = getNodeDefinition('list-operator');
    expect(def.type).toBe('list-operator');
    expect(def.label).toBe('list-operator');
    expect(def.isContainer).toBe(false);
    expect(def.hasTargetHandle).toBe(true);
    expect(def.createDefaultData()).toEqual({ type: 'list-operator' });
    expect(def.getSourceHandles({ type: 'list-operator' }).map((h) => h.id)).toEqual(['source']);
    expect(typeof def.summarize({ type: 'list-operator' })).toBe('string');
  });

  it('returns the registry definition for known types', () => {
    for (const type of NODE_TYPES) {
      expect(getNodeDefinition(type)).toBe(nodeRegistry[type]);
    }
  });

  it('accent colors are distinct across the 17 types', () => {
    const accents = NODE_TYPES.map((type) => nodeRegistry[type].accent);
    expect(new Set(accents).size).toBe(NODE_TYPES.length);
  });
});
