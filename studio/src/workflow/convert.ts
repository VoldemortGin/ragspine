/**
 * Dify workflow YAML <-> StudioWorkflow conversion. Pure functions.
 *
 * Losslessness contract: import -> export may ADD fields (node positions
 * where missing, edge id/targetHandle defaults) but never loses or alters
 * an original field. Node `data` objects are carried through verbatim;
 * everything the studio does not model rides in passthrough bags.
 */

import { dump, load } from 'js-yaml';

import { autoLayoutWorkflow, hasFinitePosition, missingPosition } from './layout';
import type { StudioEdge, StudioNode, StudioNodeData, StudioWorkflow, XY } from './types';

/** Raised for documents the studio cannot understand as a Dify workflow. */
export class WorkflowParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'WorkflowParseError';
  }
}

const NODE_OWN_KEYS = ['id', 'position', 'data'] as const;
const EDGE_OWN_KEYS = ['id', 'source', 'target', 'sourceHandle', 'targetHandle'] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function omit(record: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(record)) {
    if (!keys.includes(key)) result[key] = value;
  }
  return result;
}

function parsePosition(value: unknown): XY | undefined {
  if (
    isRecord(value) &&
    typeof value['x'] === 'number' &&
    Number.isFinite(value['x']) &&
    typeof value['y'] === 'number' &&
    Number.isFinite(value['y'])
  ) {
    return { x: value['x'], y: value['y'] };
  }
  return undefined;
}

function parseNode(raw: unknown, index: number): StudioNode {
  if (!isRecord(raw)) {
    throw new WorkflowParseError(`Node #${index} must be a mapping, got ${typeof raw}.`);
  }
  const id = raw['id'];
  if (typeof id !== 'string' || id === '') {
    throw new WorkflowParseError(`Node #${index} is missing a string "id".`);
  }
  const data = raw['data'];
  if (!isRecord(data)) {
    throw new WorkflowParseError(`Node "${id}" is missing its "data" mapping.`);
  }
  const type = data['type'];
  if (typeof type !== 'string' || type === '') {
    throw new WorkflowParseError(`Node "${id}" is missing a string "data.type".`);
  }
  const iterationId = data['iteration_id'];
  const loopId = data['loop_id'];
  const parentId =
    typeof iterationId === 'string' && iterationId !== ''
      ? iterationId
      : typeof loopId === 'string' && loopId !== ''
        ? loopId
        : undefined;
  return {
    id,
    type,
    position: parsePosition(raw['position']) ?? missingPosition(),
    ...(parentId !== undefined ? { parentId } : {}),
    data: data as StudioNodeData,
    passthrough: omit(raw, NODE_OWN_KEYS),
  };
}

function parseEdges(rawEdges: unknown[]): StudioEdge[] {
  // Pre-collect explicit ids so generated ids can never collide with them.
  const usedIds = new Set<string>();
  for (const raw of rawEdges) {
    if (isRecord(raw) && typeof raw['id'] === 'string' && raw['id'] !== '') usedIds.add(raw['id']);
  }
  return rawEdges.map((raw, index) => {
    if (!isRecord(raw)) {
      throw new WorkflowParseError(`Edge #${index} must be a mapping, got ${typeof raw}.`);
    }
    const source = raw['source'];
    if (typeof source !== 'string' || source === '') {
      throw new WorkflowParseError(`Edge #${index} is missing a string "source".`);
    }
    const target = raw['target'];
    if (typeof target !== 'string' || target === '') {
      throw new WorkflowParseError(`Edge #${index} is missing a string "target".`);
    }
    const rawSourceHandle = raw['sourceHandle'];
    const sourceHandle =
      typeof rawSourceHandle === 'string' && rawSourceHandle !== '' ? rawSourceHandle : 'source';
    const rawTargetHandle = raw['targetHandle'];
    const targetHandle =
      typeof rawTargetHandle === 'string' && rawTargetHandle !== '' ? rawTargetHandle : 'target';
    let id: string;
    if (typeof raw['id'] === 'string' && raw['id'] !== '') {
      id = raw['id'];
    } else {
      const base = `${source}__${sourceHandle}__${target}`;
      id = base;
      for (let n = 2; usedIds.has(id); n += 1) id = `${base}__${n}`;
      usedIds.add(id);
    }
    return { id, source, target, sourceHandle, targetHandle, passthrough: omit(raw, EDGE_OWN_KEYS) };
  });
}

/**
 * Parse a Dify workflow YAML document into a StudioWorkflow.
 * Throws WorkflowParseError with a human-readable message on malformed input.
 * Nodes lacking a position are auto-laid-out; explicit positions are kept.
 */
export function parseWorkflowYaml(text: string): StudioWorkflow {
  let doc: unknown;
  try {
    doc = load(text);
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new WorkflowParseError(`Not a valid YAML document: ${reason}`);
  }
  if (!isRecord(doc)) {
    throw new WorkflowParseError('Document root must be a mapping (app/kind/version/workflow).');
  }

  const app = doc['app'];
  if (!isRecord(app)) {
    throw new WorkflowParseError('Missing or invalid top-level "app" section.');
  }
  const rawMode = app['mode'] ?? 'workflow';
  if (rawMode !== 'workflow' && rawMode !== 'advanced-chat') {
    throw new WorkflowParseError(
      `Unsupported app.mode "${String(rawMode)}" (expected "workflow" or "advanced-chat").`,
    );
  }
  const rawName = app['name'];
  const name = typeof rawName === 'string' ? rawName : rawName === undefined ? '' : String(rawName);

  const rawVersion = doc['version'];
  const version =
    typeof rawVersion === 'string' ? rawVersion : rawVersion === undefined ? '0.1.5' : String(rawVersion);

  const workflow = doc['workflow'];
  if (!isRecord(workflow)) {
    throw new WorkflowParseError('Missing or invalid top-level "workflow" section.');
  }
  const graph = workflow['graph'];
  if (!isRecord(graph)) {
    throw new WorkflowParseError('Missing or invalid "workflow.graph" section.');
  }
  const rawNodes = graph['nodes'] ?? [];
  if (!Array.isArray(rawNodes)) {
    throw new WorkflowParseError('"workflow.graph.nodes" must be a list.');
  }
  const rawEdges = graph['edges'] ?? [];
  if (!Array.isArray(rawEdges)) {
    throw new WorkflowParseError('"workflow.graph.edges" must be a list.');
  }

  const nodes = rawNodes.map((raw, index) => parseNode(raw, index));
  const edges = parseEdges(rawEdges);

  const wf: StudioWorkflow = {
    name,
    mode: rawMode,
    version,
    appPassthrough: omit(app, ['mode', 'name']),
    docPassthrough: omit(doc, ['app', 'version', 'workflow']), // includes `kind`
    workflowPassthrough: omit(workflow, ['graph']),
    graphPassthrough: omit(graph, ['nodes', 'edges']),
    nodes,
    edges,
  };
  return nodes.some((n) => !hasFinitePosition(n.position)) ? autoLayoutWorkflow(wf) : wf;
}

/** Container-child id field on node data: iteration children carry iteration_id, loop children loop_id. */
export type ContainerIdField = 'iteration_id' | 'loop_id';

const CONTAINER_ID_FIELDS: readonly ContainerIdField[] = ['iteration_id', 'loop_id'];

/** Container id field used on children of a container node of `parentType`. */
export function containerIdField(parentType: string): ContainerIdField {
  return parentType === 'loop' ? 'loop_id' : 'iteration_id';
}

/**
 * Return `data` with the container id field kept in sync with the node's
 * parentId: `idField` set/overwritten (and the other container id field
 * dropped) when the node lives in a container, both fields dropped when the
 * node was detached from one. Returns the same object when nothing changes.
 */
export function withSyncedContainerId(
  data: StudioNodeData,
  parentId: string | undefined,
  idField: ContainerIdField,
): StudioNodeData {
  const otherField: ContainerIdField = idField === 'loop_id' ? 'iteration_id' : 'loop_id';
  if (parentId !== undefined) {
    if (data[idField] === parentId && !(otherField in data)) return data;
    const next = { ...data };
    delete next[otherField];
    next[idField] = parentId;
    return next;
  }
  if (CONTAINER_ID_FIELDS.some((field) => field in data)) {
    const next = { ...data };
    for (const field of CONTAINER_ID_FIELDS) delete next[field];
    return next;
  }
  return data;
}

/** Serialize a StudioWorkflow back into Dify workflow YAML. */
export function serializeWorkflowYaml(wf: StudioWorkflow): string {
  const typeById = new Map(wf.nodes.map((n) => [n.id, n.type] as const));
  const nodes = wf.nodes.map((node) => {
    // Fall back to iteration_id when the parent node is missing (legacy behavior).
    const parentType = node.parentId === undefined ? '' : (typeById.get(node.parentId) ?? '');
    return {
      id: node.id,
      position: { x: node.position.x, y: node.position.y },
      data: withSyncedContainerId(node.data, node.parentId, containerIdField(parentType)),
      ...omit(node.passthrough, NODE_OWN_KEYS),
    };
  });
  const edges = wf.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.sourceHandle,
    targetHandle: edge.targetHandle,
    ...omit(edge.passthrough, EDGE_OWN_KEYS),
  }));
  const { kind, ...docExtras } = wf.docPassthrough;
  const doc = {
    app: { mode: wf.mode, name: wf.name, ...omit(wf.appPassthrough, ['mode', 'name']) },
    kind: kind ?? 'app',
    version: wf.version,
    workflow: {
      ...omit(wf.workflowPassthrough, ['graph']),
      graph: { ...omit(wf.graphPassthrough, ['nodes', 'edges']), nodes, edges },
    },
    ...docExtras,
  };
  return dump(doc, { noRefs: true, lineWidth: -1 });
}
