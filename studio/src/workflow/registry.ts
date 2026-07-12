/**
 * Node type registry: per-type metadata (label, category, accent, handles,
 * default data, summary line) for the 17 Dify palette types, plus a generic
 * fallback definition for unknown node types found in imported YAML.
 *
 * Pure module: no React, no side effects.
 */

import { isDifyNodeType } from './types';
import type {
  AssignerNodeData,
  ClassifierClass,
  CodeNodeData,
  DifyNodeType,
  DocumentExtractorNodeData,
  EndNodeData,
  ExtractorParameter,
  HandleSpec,
  HttpRequestNodeData,
  IfElseCase,
  IfElseNodeData,
  IterationNodeData,
  KnowledgeRetrievalNodeData,
  LlmNodeData,
  LoopNodeData,
  NodeTypeDefinition,
  ParameterExtractorNodeData,
  QuestionClassifierNodeData,
  StartNodeData,
  StartVariable,
  StudioNodeData,
  TemplateTransformNodeData,
  ToolNodeData,
  VariableAggregatorNodeData,
} from './types';

/** Single unlabeled "source" handle used by all non-branching nodes. */
function singleSourceHandle(): HandleSpec[] {
  return [{ id: 'source', label: '' }];
}

function count(n: number, noun: string, pluralSuffix = 's'): string {
  return `${n} ${noun}${n === 1 ? '' : pluralSuffix}`;
}

function truncate(text: string, max = 40): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length <= max ? flat : `${flat.slice(0, max - 3)}...`;
}

function ifElseHandles(cases: IfElseCase[]): HandleSpec[] {
  const handles: HandleSpec[] = cases.map((c, index) => ({
    id: typeof c.case_id === 'string' && c.case_id !== '' ? c.case_id : `case_${index + 1}`,
    label: index === 0 ? 'IF' : `ELIF ${index}`,
  }));
  if (!handles.some((h) => h.id === 'false')) {
    handles.push({ id: 'false', label: 'ELSE' });
  }
  return handles;
}

function classifierHandles(classes: ClassifierClass[]): HandleSpec[] {
  return classes.map((c, index) => {
    const id = typeof c.id === 'string' && c.id !== '' ? c.id : String(index + 1);
    const label = typeof c.name === 'string' && c.name !== '' ? c.name : id;
    return { id, label };
  });
}

export const nodeRegistry: Record<DifyNodeType, NodeTypeDefinition> = {
  start: {
    type: 'start',
    label: 'Start',
    description: 'Workflow entry point declaring the input variables.',
    category: 'flow',
    accent: '#34d399',
    isContainer: false,
    hasTargetHandle: false,
    createDefaultData: (): StartNodeData => ({
      type: 'start',
      variables: [{ variable: 'query', label: 'Query', type: 'text-input', required: true }],
    }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const variables = (data as StartNodeData).variables;
      const names = Array.isArray(variables)
        ? variables.map((v: StartVariable) => v.variable).filter((v) => typeof v === 'string')
        : [];
      return names.length > 0 ? names.join(', ') : 'no inputs';
    },
  },

  end: {
    type: 'end',
    label: 'End',
    description: 'Workflow exit collecting the final outputs.',
    category: 'flow',
    accent: '#94a3b8',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): EndNodeData => ({ type: 'end', outputs: [] }),
    getSourceHandles: () => [],
    summarize: (data) => {
      const outputs = (data as EndNodeData).outputs;
      return count(Array.isArray(outputs) ? outputs.length : 0, 'output');
    },
  },

  answer: {
    type: 'answer',
    label: 'Answer',
    description: 'Streams a templated reply to the user (advanced-chat).',
    category: 'flow',
    accent: '#38bdf8',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: () => ({ type: 'answer', answer: '' }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const answer = (data as { answer?: unknown }).answer;
      return typeof answer === 'string' && answer.trim() !== '' ? truncate(answer) : 'empty answer';
    },
  },

  llm: {
    type: 'llm',
    label: 'LLM',
    description: 'Calls a chat model with a prompt template.',
    category: 'model',
    accent: '#a78bfa',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): LlmNodeData => ({
      type: 'llm',
      model: { provider: 'anthropic', name: '', completion_params: {} },
      prompt_template: [{ role: 'user', text: '' }],
    }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const model = (data as LlmNodeData).model;
      const name = model && typeof model.name === 'string' ? model.name : '';
      return name !== '' ? name : 'model not set';
    },
  },

  code: {
    type: 'code',
    label: 'Code',
    description: 'Runs a code snippet over selected variables.',
    category: 'transform',
    accent: '#fbbf24',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): CodeNodeData => ({
      type: 'code',
      code: 'def main() -> dict:\n    return {"result": ""}\n',
      code_language: 'python3',
      variables: [],
      outputs: {},
    }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const language = (data as CodeNodeData).code_language;
      return typeof language === 'string' && language !== '' ? language : 'python3';
    },
  },

  'if-else': {
    type: 'if-else',
    label: 'If/Else',
    description: 'Branches on condition cases with an implicit else.',
    category: 'logic',
    accent: '#fb923c',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): IfElseNodeData => ({
      type: 'if-else',
      cases: [{ case_id: 'true', logical_operator: 'and', conditions: [] }],
    }),
    getSourceHandles: (data) => {
      const cases = (data as IfElseNodeData).cases;
      return ifElseHandles(Array.isArray(cases) ? cases : []);
    },
    summarize: (data) => {
      const cases = (data as IfElseNodeData).cases;
      return count(Array.isArray(cases) ? cases.length : 0, 'case');
    },
  },

  'question-classifier': {
    type: 'question-classifier',
    label: 'Question Classifier',
    description: 'Routes the flow by classifying the input question.',
    category: 'logic',
    accent: '#e879f9',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): QuestionClassifierNodeData => ({
      type: 'question-classifier',
      classes: [
        { id: '1', name: 'Class 1' },
        { id: '2', name: 'Class 2' },
      ],
    }),
    getSourceHandles: (data) => {
      const classes = (data as QuestionClassifierNodeData).classes;
      return classifierHandles(Array.isArray(classes) ? classes : []);
    },
    summarize: (data) => {
      const classes = (data as QuestionClassifierNodeData).classes;
      return count(Array.isArray(classes) ? classes.length : 0, 'class', 'es');
    },
  },

  'template-transform': {
    type: 'template-transform',
    label: 'Template',
    description: 'Renders a Jinja template over selected variables.',
    category: 'transform',
    accent: '#2dd4bf',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): TemplateTransformNodeData => ({
      type: 'template-transform',
      template: '',
      variables: [],
    }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const template = (data as TemplateTransformNodeData).template;
      return typeof template === 'string' && template.trim() !== ''
        ? truncate(template)
        : 'empty template';
    },
  },

  iteration: {
    type: 'iteration',
    label: 'Iteration',
    description: 'Runs an inner subgraph once per item of an array input.',
    category: 'flow',
    accent: '#22d3ee',
    isContainer: true,
    hasTargetHandle: true,
    createDefaultData: (): IterationNodeData => ({
      type: 'iteration',
      iterator_selector: [],
      output_selector: [],
      is_parallel: false,
      parallel_nums: 1,
    }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const d = data as IterationNodeData;
      return d.is_parallel === true ? `parallel x${d.parallel_nums ?? 1}` : 'sequential';
    },
  },

  'knowledge-retrieval': {
    type: 'knowledge-retrieval',
    label: 'Knowledge Retrieval',
    description: 'Retrieves top-k chunks from the selected datasets.',
    category: 'rag',
    accent: '#60a5fa',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): KnowledgeRetrievalNodeData => ({
      type: 'knowledge-retrieval',
      query_variable_selector: [],
      dataset_ids: [],
      top_k: 4,
    }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const d = data as KnowledgeRetrievalNodeData;
      const topK =
        typeof d.top_k === 'number'
          ? d.top_k
          : typeof d.multiple_retrieval_config?.top_k === 'number'
            ? d.multiple_retrieval_config.top_k
            : 4;
      const datasets = Array.isArray(d.dataset_ids) ? d.dataset_ids.length : 0;
      return `k=${topK}, ${count(datasets, 'dataset')}`;
    },
  },

  'parameter-extractor': {
    type: 'parameter-extractor',
    label: 'Parameter Extractor',
    description: 'Extracts structured parameters from text via an LLM.',
    category: 'model',
    accent: '#a3e635',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): ParameterExtractorNodeData => ({
      type: 'parameter-extractor',
      query: [],
      parameters: [],
      instruction: '',
      model: { name: '' },
    }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const parameters = (data as ParameterExtractorNodeData).parameters;
      const names = Array.isArray(parameters)
        ? parameters.map((p: ExtractorParameter) => p.name).filter((n) => typeof n === 'string')
        : [];
      return names.length > 0 ? names.join(', ') : 'no parameters';
    },
  },

  tool: {
    type: 'tool',
    label: 'Tool',
    description: 'Invokes an external tool with mapped parameters.',
    category: 'tools',
    accent: '#fb7185',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): ToolNodeData => ({ type: 'tool', tool_name: '', tool_parameters: {} }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const name = (data as ToolNodeData).tool_name;
      return typeof name === 'string' && name !== '' ? name : 'tool not set';
    },
  },

  'http-request': {
    type: 'http-request',
    label: 'HTTP Request',
    description: 'Sends an HTTP request and exposes the response.',
    category: 'tools',
    accent: '#f472b6',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): HttpRequestNodeData => ({
      type: 'http-request',
      method: 'get',
      url: '',
      headers: '',
      params: '',
      authorization: { type: 'no-auth', config: null },
      body: { type: 'none', data: [] },
      ssl_verify: true,
      variables: [],
      retry_config: { retry_enabled: true, max_retries: 3, retry_interval: 100 },
    }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const d = data as HttpRequestNodeData;
      const rawMethod: unknown = d.method;
      const method = typeof rawMethod === 'string' && rawMethod !== '' ? rawMethod.toUpperCase() : 'GET';
      const url = typeof d.url === 'string' && d.url.trim() !== '' ? truncate(d.url) : 'url not set';
      return `${method} ${url}`;
    },
  },

  'variable-aggregator': {
    type: 'variable-aggregator',
    label: 'Variable Aggregator',
    description: 'Merges variables from multiple branches into one output.',
    category: 'transform',
    accent: '#c084fc',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): VariableAggregatorNodeData => ({
      type: 'variable-aggregator',
      output_type: 'any',
      variables: [],
    }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const d = data as VariableAggregatorNodeData;
      if (d.advanced_settings?.group_enabled === true) {
        const groups = d.advanced_settings.groups;
        return count(Array.isArray(groups) ? groups.length : 0, 'group');
      }
      return count(Array.isArray(d.variables) ? d.variables.length : 0, 'variable');
    },
  },

  assigner: {
    type: 'assigner',
    label: 'Variable Assigner',
    description: 'Writes values into writable (conversation/loop) variables.',
    category: 'transform',
    accent: '#818cf8',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): AssignerNodeData => ({ type: 'assigner', version: '2', items: [] }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const items = (data as AssignerNodeData).items;
      return count(Array.isArray(items) ? items.length : 0, 'assignment');
    },
  },

  'document-extractor': {
    type: 'document-extractor',
    label: 'Document Extractor',
    description: 'Extracts plain text from an uploaded document file.',
    category: 'transform',
    accent: '#4ade80',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: (): DocumentExtractorNodeData => ({
      type: 'document-extractor',
      variable_selector: [],
      is_array_file: false,
    }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const d = data as DocumentExtractorNodeData;
      const selector = Array.isArray(d.variable_selector) ? d.variable_selector : [];
      const label = selector.length > 0 ? selector.join('.') : 'no input';
      return d.is_array_file === true ? `${label} (array)` : label;
    },
  },

  loop: {
    type: 'loop',
    label: 'Loop',
    description: 'Repeats an inner subgraph until break conditions or a max count.',
    category: 'flow',
    accent: '#67e8f9',
    isContainer: true,
    hasTargetHandle: true,
    createDefaultData: (): LoopNodeData => ({
      type: 'loop',
      loop_count: 10,
      break_conditions: [],
      logical_operator: 'and',
      loop_variables: [],
    }),
    getSourceHandles: singleSourceHandle,
    summarize: (data) => {
      const d = data as LoopNodeData;
      const loopCount = typeof d.loop_count === 'number' ? d.loop_count : 10;
      const breaks = Array.isArray(d.break_conditions) ? d.break_conditions.length : 0;
      return breaks > 0 ? `x${loopCount}, ${count(breaks, 'break condition')}` : `x${loopCount}`;
    },
  },
};

/**
 * Resolve a node type definition. Unknown types (anything outside the 17
 * palette types) get a generic fallback so imported YAML always renders.
 */
export function getNodeDefinition(type: string): NodeTypeDefinition {
  if (isDifyNodeType(type)) return nodeRegistry[type];
  return {
    type,
    label: type,
    description: `Unknown node type "${type}" (rendered generically, data preserved as-is).`,
    category: 'tools',
    accent: '#9ca3af',
    isContainer: false,
    hasTargetHandle: true,
    createDefaultData: () => ({ type }),
    getSourceHandles: singleSourceHandle,
    summarize: (data: StudioNodeData) => {
      const title = data.title;
      return typeof title === 'string' && title !== '' ? title : type;
    },
  };
}
