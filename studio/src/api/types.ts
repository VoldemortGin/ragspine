/**
 * Typed contracts for the RAGSpine service API.
 *
 * All error responses share the shape:
 *   { "error": { "type": string, "message": string, "request_id": string } }
 */

export interface ApiErrorBody {
  error: {
    type: string;
    message: string;
    request_id?: string;
  };
}

/* ---------------------------- Dify workflow ---------------------------- */

export type SuggestionSeverity = 'high' | 'medium' | 'low' | 'info';

export interface Suggestion {
  rule_id: string;
  severity: SuggestionSeverity;
  category: string;
  title: string;
  detail: string;
  node_ids: string[];
}

export interface AnalyzeRequest {
  yaml: string;
}

export interface AnalyzeResponse {
  request_id: string;
  suggestions: Suggestion[];
}

export interface CompileRequest {
  yaml: string;
  target: 'ragspine';
  fold_answer_question: boolean;
}

export interface CompileResponse {
  request_id: string;
  code: string;
  entrypoint: string;
  imports: string[];
  warnings: string[];
  suggestions: Suggestion[];
}

export interface RunRequest {
  yaml: string;
  inputs: Record<string, unknown>;
  fold_answer_question: boolean;
}

export type NodeTraceStatus = 'succeeded' | 'failed' | 'skipped';

/**
 * Per-node execution record of a workflow run. `node_id` maps back to the
 * canvas node; `node_type` comes from the IR layer and may differ from the
 * Dify type (e.g. question-classifier reports "if-else") — never match on it.
 */
export interface NodeTrace {
  index: number;
  node_id: string;
  title: string;
  node_type: string;
  status: NodeTraceStatus;
  elapsed_ms: number;
  inputs: Record<string, unknown> | null;
  outputs: Record<string, unknown> | null;
  error: string | null;
}

export interface RunResponse {
  request_id: string;
  result: unknown;
  warnings: string[];
  node_traces?: NodeTrace[] | null;
}

/** Shape of `JobStatus.result` for async dify-run jobs. */
export interface DifyRunJobResult {
  result: unknown;
  warnings?: string[];
  node_traces?: NodeTrace[] | null;
}

/* -------------------------------- Jobs --------------------------------- */

export interface JobRef {
  job_id: string;
}

export type JobState = 'queued' | 'started' | 'finished' | 'failed';

export interface JobStatus {
  id: string;
  status: JobState;
  result?: unknown;
  error?: string | null;
}

/* ------------------------------ Playground ----------------------------- */

export interface AskRequest {
  question: string;
  reference_date?: string;
}

export type AnswerKind = 'normal' | 'clarification' | 'refusal';

export interface Clarification {
  mode?: string;
  question?: string;
  narrowing_options?: string[];
  assumption_note?: string;
  [key: string]: unknown;
}

export interface AskSource {
  doc?: string;
  locator?: string;
  [key: string]: unknown;
}

export interface ToolStatusSummary {
  found?: number;
  not_found?: number;
  unrecognized?: number;
  [key: string]: unknown;
}

export interface CacheInfo {
  hit?: boolean;
  type?: string;
  faq_id?: string;
  [key: string]: unknown;
}

export interface AskResponse {
  request_id: string;
  answer: string;
  route?: string;
  answer_kind: AnswerKind;
  clarification: Clarification | null;
  sources: AskSource[];
  tool_status_summary?: ToolStatusSummary;
  cache?: CacheInfo;
}

/* ------------------------------- Topology ------------------------------ */

export type TopologyScope = 'agent' | 'service';

export type TopologyNodeKind = 'stage' | 'store' | 'external' | 'gate' | 'channel';

export interface TopologyNode {
  id: string;
  label: string;
  kind: TopologyNodeKind | string;
  domain?: string;
  symbol?: string;
  [key: string]: unknown;
}

export type TopologyEdgeKind = 'flow' | 'conditional' | 'data';

export interface TopologyEdge {
  src: string;
  dst: string;
  label?: string;
  kind?: TopologyEdgeKind | string;
  [key: string]: unknown;
}

export interface TopologyGraph {
  title?: string;
  nodes: TopologyNode[];
  edges: TopologyEdge[];
}

/**
 * The graph may be nested under a `graph` key or flattened at the top level;
 * the client must normalize both shapes into TopologyGraph.
 */
export interface TopologyResponse {
  request_id?: string;
  graph?: TopologyGraph;
  title?: string;
  nodes?: TopologyNode[];
  edges?: TopologyEdge[];
}

/* ------------------------------ Ingestion ------------------------------ */

export interface StructuredIngestRequest {
  /** Server-side path (restricted by allowed_upload_root; .xlsx/.xlsm/.pptx/.pdf). */
  file: string;
  dry_run?: boolean;
  valid_as_of?: string;
  batch_id?: string;
}

export interface NarrativeIngestRequest {
  inputs: string[] | string;
  dry_run?: boolean;
  meta_by_doc?: Record<string, unknown>;
}

/* -------------------------------- Health ------------------------------- */

export interface HealthState {
  healthy: boolean;
  ready: boolean;
}

/* ---------------------------- n8n conversion --------------------------- */

export type N8nConvertDirection = 'n8n_to_dify' | 'dify_to_n8n';

export interface N8nConvertRequest {
  direction: N8nConvertDirection;
  /** n8n workflow JSON (n8n_to_dify) or Dify DSL YAML string (dify_to_n8n). */
  workflow: Record<string, unknown> | string;
}

export interface N8nConvertResponse {
  request_id: string;
  /** Dify workflow dict (n8n_to_dify) or n8n workflow JSON (dify_to_n8n). */
  workflow: Record<string, unknown>;
  /** Converted Dify DSL YAML (n8n_to_dify only; null for dify_to_n8n). */
  yaml: string | null;
  warnings: string[];
}
