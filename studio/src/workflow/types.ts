/**
 * Pure canvas model for Dify workflow documents.
 *
 * Design contract (do not weaken):
 * - The model is React-free so the yaml <-> graph conversion layer stays
 *   testable as pure functions.
 * - Unknown fields are ALWAYS preserved: node `data` keeps the full Dify data
 *   object (known + unknown keys), and every level carries a `passthrough`
 *   bag for fields the studio does not model. Import -> export must be
 *   lossless for fields we do not understand.
 */

export interface XY {
  x: number;
  y: number;
}

export const NODE_TYPES = [
  'start',
  'end',
  'answer',
  'llm',
  'code',
  'if-else',
  'question-classifier',
  'template-transform',
  'iteration',
  'knowledge-retrieval',
  'parameter-extractor',
  'tool',
] as const;

export type DifyNodeType = (typeof NODE_TYPES)[number];

export function isDifyNodeType(value: unknown): value is DifyNodeType {
  return typeof value === 'string' && (NODE_TYPES as readonly string[]).includes(value);
}

/** Dify variable selector, e.g. ["start_1", "question"]. */
export type ValueSelector = string[];

/**
 * The full Dify node `data` object. Known fields are accessed through the
 * typed views below; unknown fields ride along untouched.
 *
 * `type` is intentionally `string`, not DifyNodeType: imported YAML may
 * contain node types outside the 12 palette types. Those must be preserved
 * and rendered with a generic fallback, never dropped or rejected.
 */
export interface StudioNodeData {
  type: string;
  title?: string;
  [key: string]: unknown;
}

export interface StudioNode {
  id: string;
  /** Mirrors data.type for convenience. Keep in sync with data.type. */
  type: string;
  position: XY;
  /** Iteration container id, derived from data.iteration_id on import. */
  parentId?: string;
  data: StudioNodeData;
  /** Node-level fields other than id/position/data (width, height, ...). */
  passthrough: Record<string, unknown>;
}

export interface StudioEdge {
  id: string;
  source: string;
  target: string;
  /** Defaults to "source". Branch handles: if-else case_id, classifier class id. */
  sourceHandle: string;
  /** Defaults to "target". */
  targetHandle: string;
  /** Edge-level fields other than id/source/target/sourceHandle/targetHandle. */
  passthrough: Record<string, unknown>;
}

export interface StudioWorkflow {
  /** app.name */
  name: string;
  /** app.mode */
  mode: 'workflow' | 'advanced-chat';
  /** Top-level version (default "0.1.5"). */
  version: string;
  /** app.* fields other than name/mode. */
  appPassthrough: Record<string, unknown>;
  /** Top-level fields other than app/kind/version/workflow. */
  docPassthrough: Record<string, unknown>;
  /** workflow.* fields other than graph (features, environment_variables, ...). */
  workflowPassthrough: Record<string, unknown>;
  /** workflow.graph.* fields other than nodes/edges (viewport, ...). */
  graphPassthrough: Record<string, unknown>;
  nodes: StudioNode[];
  edges: StudioEdge[];
}

/* ------------------------------------------------------------------ */
/* Typed views over StudioNodeData known fields (per node type).       */
/* Access via `node.data as unknown as LlmNodeData` style casts or the */
/* registry helpers; extra keys are still present at runtime.          */
/* ------------------------------------------------------------------ */

export interface StartVariable {
  variable: string;
  label?: string;
  type?: string; // text-input | number | paragraph | ...
  required?: boolean;
  [key: string]: unknown;
}

export interface StartNodeData extends StudioNodeData {
  variables?: StartVariable[];
}

export interface EndOutput {
  variable: string;
  value_selector: ValueSelector;
  [key: string]: unknown;
}

export interface EndNodeData extends StudioNodeData {
  outputs?: EndOutput[];
}

export interface AnswerNodeData extends StudioNodeData {
  /** Template string with {{#nodeId.field#}} interpolation. */
  answer?: string;
}

export interface PromptMessage {
  role: 'system' | 'user' | 'assistant';
  text: string;
  [key: string]: unknown;
}

export interface ModelConfig {
  provider?: string;
  name?: string;
  completion_params?: { max_tokens?: number; [key: string]: unknown };
  [key: string]: unknown;
}

export interface LlmNodeData extends StudioNodeData {
  prompt_template?: PromptMessage[];
  model?: ModelConfig;
  context?: { enabled?: boolean; variable_selector?: ValueSelector; [key: string]: unknown };
}

export interface CodeVariable {
  variable: string;
  value_selector: ValueSelector;
  [key: string]: unknown;
}

export interface CodeNodeData extends StudioNodeData {
  code?: string;
  code_language?: string; // default "python3"
  variables?: CodeVariable[];
  outputs?: Record<string, unknown>;
}

export const COMPARISON_OPERATORS = [
  '==',
  '!=',
  '>',
  '<',
  '>=',
  '<=',
  'contains',
  'not contains',
  'empty',
  'not empty',
  'start with',
  'end with',
  'is',
  'is not',
] as const;

export type ComparisonOperator = (typeof COMPARISON_OPERATORS)[number];

export interface IfElseCondition {
  variable_selector: ValueSelector;
  comparison_operator: string;
  value?: string;
  [key: string]: unknown;
}

export interface IfElseCase {
  case_id: string;
  logical_operator: 'and' | 'or';
  conditions: IfElseCondition[];
  [key: string]: unknown;
}

export interface IfElseNodeData extends StudioNodeData {
  cases?: IfElseCase[];
}

export interface ClassifierClass {
  id: string;
  name: string;
  [key: string]: unknown;
}

export interface QuestionClassifierNodeData extends StudioNodeData {
  classes?: ClassifierClass[];
}

export interface TemplateTransformNodeData extends StudioNodeData {
  template?: string;
  variables?: CodeVariable[];
}

export interface IterationNodeData extends StudioNodeData {
  iterator_selector?: ValueSelector;
  output_selector?: ValueSelector;
  is_parallel?: boolean; // default false
  parallel_nums?: number; // default 1
  iteration_id?: string; // present on CHILD nodes, not on the container
}

export interface KnowledgeRetrievalNodeData extends StudioNodeData {
  query_variable_selector?: ValueSelector;
  dataset_ids?: string[];
  top_k?: number; // default 4; may instead live in multiple_retrieval_config.top_k
  multiple_retrieval_config?: { top_k?: number; [key: string]: unknown };
}

export interface ExtractorParameter {
  name: string;
  type?: string; // default "string"
  description?: string;
  required?: boolean;
  [key: string]: unknown;
}

export interface ParameterExtractorNodeData extends StudioNodeData {
  query?: ValueSelector;
  parameters?: ExtractorParameter[];
  instruction?: string;
  model?: ModelConfig;
}

export interface ToolParameterValue {
  type: 'variable' | 'mixed' | 'constant';
  value: unknown;
  [key: string]: unknown;
}

export interface ToolNodeData extends StudioNodeData {
  tool_name?: string;
  tool_parameters?: Record<string, ToolParameterValue>;
}

/* ------------------------------------------------------------------ */
/* Node type registry contract.                                        */
/* ------------------------------------------------------------------ */

export interface HandleSpec {
  id: string;
  label: string;
}

export interface NodeTypeDefinition {
  /** One of the 12 DifyNodeType values, or the raw type for the fallback def. */
  type: string;
  label: string;
  description: string;
  category: 'flow' | 'model' | 'logic' | 'transform' | 'rag' | 'tools';
  /** Hex accent color used by the canvas node and the palette. */
  accent: string;
  /** True only for iteration (renders as a container/group node). */
  isContainer: boolean;
  /** start has no incoming handle. */
  hasTargetHandle: boolean;
  /** Fresh default data for a node dropped from the palette. */
  createDefaultData: () => StudioNodeData;
  /**
   * Source handles derived from data: single [{id: "source"}] for plain
   * nodes, one per case (+ implicit "false" ELSE) for if-else, one per
   * class for question-classifier, [] for end.
   */
  getSourceHandles: (data: StudioNodeData) => HandleSpec[];
  /** Short one-line summary of the node config shown on the canvas card. */
  summarize: (data: StudioNodeData) => string;
}
