/**
 * Typed fetch client for the RAGSpine service API.
 *
 * All requests use relative URLs (proxied to the backend in dev via Vite,
 * served same-origin in production).
 */

import type {
  AnalyzeResponse,
  ApiErrorBody,
  AskRequest,
  AskResponse,
  CompileResponse,
  HealthState,
  JobRef,
  JobStatus,
  N8nConvertDirection,
  N8nConvertResponse,
  NarrativeIngestRequest,
  RunResponse,
  StructuredIngestRequest,
  TopologyGraph,
  TopologyResponse,
  TopologyScope,
  WorkflowScaffoldRequest,
  WorkflowScaffoldResponse,
  WorkflowTemplateDetail,
  WorkflowTemplateListResponse,
} from './types';

export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly requestId?: string;
  /** Parsed JSON error body, when the response carried one (extra keys included). */
  readonly body?: unknown;

  constructor(
    message: string,
    status: number,
    type: string,
    requestId?: string,
    body?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.type = type;
    this.requestId = requestId;
    this.body = body;
  }
}

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  if (typeof value !== 'object' || value === null) return false;
  const err = (value as Record<string, unknown>)['error'];
  if (typeof err !== 'object' || err === null) return false;
  const e = err as Record<string, unknown>;
  return typeof e['type'] === 'string' && typeof e['message'] === 'string';
}

async function toApiError(res: Response): Promise<ApiError> {
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    body = undefined;
  }
  if (isApiErrorBody(body)) {
    const { type, message, request_id } = body.error;
    return new ApiError(message, res.status, type, request_id, body);
  }
  const fallback = res.statusText || `HTTP ${res.status}`;
  return new ApiError(fallback, res.status, 'http_error', undefined, body);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (err) {
    const message =
      err instanceof Error ? err.message : 'Network request failed';
    throw new ApiError(message, 0, 'network_error');
  }
  if (!res.ok) {
    throw await toApiError(res);
  }
  return (await res.json()) as T;
}

function postJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/* ---------------------------- Dify workflow ---------------------------- */

export function analyzeWorkflow(yaml: string): Promise<AnalyzeResponse> {
  return postJson<AnalyzeResponse>('/v1/dify/analyze', { yaml });
}

export function compileWorkflow(
  yaml: string,
  foldAnswerQuestion = true,
): Promise<CompileResponse> {
  return postJson<CompileResponse>('/v1/dify/compile', {
    yaml,
    target: 'ragspine',
    fold_answer_question: foldAnswerQuestion,
  });
}

export function runWorkflow(
  yaml: string,
  inputs: Record<string, unknown>,
  foldAnswerQuestion = true,
): Promise<RunResponse> {
  return postJson<RunResponse>('/v1/dify/run', {
    yaml,
    inputs,
    fold_answer_question: foldAnswerQuestion,
  });
}

export function runWorkflowAsync(
  yaml: string,
  inputs: Record<string, unknown>,
  foldAnswerQuestion = true,
): Promise<JobRef> {
  return postJson<JobRef>('/v1/dify/run/jobs', {
    yaml,
    inputs,
    fold_answer_question: foldAnswerQuestion,
  });
}

/* -------------------------------- Jobs --------------------------------- */

export function getJob(jobId: string): Promise<JobStatus> {
  return request<JobStatus>(`/v1/jobs/${encodeURIComponent(jobId)}`);
}

/* ------------------------------ Playground ----------------------------- */

export function ask(req: AskRequest): Promise<AskResponse> {
  return postJson<AskResponse>('/v1/ask', req);
}

/* ------------------------------- Topology ------------------------------ */

export async function fetchTopology(
  scope: TopologyScope,
): Promise<{ requestId?: string; graph: TopologyGraph }> {
  const res = await request<TopologyResponse>(
    `/v1/topology?scope=${encodeURIComponent(scope)}`,
  );
  const graph: TopologyGraph = res.graph ?? {
    title: res.title,
    nodes: res.nodes ?? [],
    edges: res.edges ?? [],
  };
  return {
    requestId: res.request_id,
    graph: {
      title: graph.title,
      nodes: graph.nodes ?? [],
      edges: graph.edges ?? [],
    },
  };
}

/* ------------------------------ Ingestion ------------------------------ */

export function submitStructuredIngest(
  req: StructuredIngestRequest,
): Promise<JobRef> {
  return postJson<JobRef>('/v1/ingest/structured/jobs', req);
}

export function submitNarrativeIngest(
  req: NarrativeIngestRequest,
): Promise<JobRef> {
  return postJson<JobRef>('/v1/ingest/narrative/jobs', req);
}

/* -------------------------------- Health ------------------------------- */

export async function checkHealth(): Promise<HealthState> {
  const [healthy, ready] = await Promise.all([
    fetch('/healthz')
      .then((res) => res.ok)
      .catch(() => false),
    fetch('/readyz')
      .then((res) => res.ok)
      .catch(() => false),
  ]);
  return { healthy, ready };
}

/* ---------------------------- n8n conversion --------------------------- */

export function convertN8n(
  direction: N8nConvertDirection,
  workflow: Record<string, unknown> | string,
): Promise<N8nConvertResponse> {
  return postJson<N8nConvertResponse>('/v1/n8n/convert', {
    direction,
    workflow,
  });
}

/* ------------------------- Workflow template catalog ------------------ */

export function listWorkflowTemplates(
  signal?: AbortSignal,
): Promise<WorkflowTemplateListResponse> {
  return request<WorkflowTemplateListResponse>('/v1/workflow-templates', {
    method: 'GET',
    ...(signal !== undefined ? { signal } : {}),
  });
}

export function getWorkflowTemplate(
  templateId: string,
  signal?: AbortSignal,
): Promise<WorkflowTemplateDetail> {
  return request<WorkflowTemplateDetail>(
    `/v1/workflow-templates/${encodeURIComponent(templateId)}`,
    {
      method: 'GET',
      ...(signal !== undefined ? { signal } : {}),
    },
  );
}

export function scaffoldWorkflow(
  payload: WorkflowScaffoldRequest,
  signal?: AbortSignal,
): Promise<WorkflowScaffoldResponse> {
  return request<WorkflowScaffoldResponse>('/v1/workflow-scaffold', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    ...(signal !== undefined ? { signal } : {}),
  });
}
