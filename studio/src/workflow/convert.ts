/**
 * Dify workflow YAML <-> StudioWorkflow conversion. Pure functions.
 *
 * Losslessness contract: import -> export preserves all non-derived fields.
 * ReactFlow-derived wrappers (absolute position, parent/container membership,
 * edge endpoint types) are recomputed from the current graph so edits cannot
 * export stale metadata. Everything else rides in passthrough bags untouched.
 */

import { dump, load } from 'js-yaml';
import { parse as parseToml } from 'smol-toml';

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
const NODE_DERIVED_KEYS = ['parentId', 'positionAbsolute'] as const;
export const MAX_WORKFLOW_BYTES = 1024 * 1024;
const MAX_YAML_ALIASES = 64;
const MAX_WORKFLOW_DEPTH = 32;
const MAX_YAML_MERGE_KEYS = 1024;
const MAX_WORKFLOW_VALUES = 20_000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function omit(record: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  const result: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
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
  const wrapperParentId = raw['parentId'];
  const parentId =
    typeof iterationId === 'string' && iterationId !== ''
      ? iterationId
      : typeof loopId === 'string' && loopId !== ''
        ? loopId
        : typeof wrapperParentId === 'string' && wrapperParentId !== ''
          ? wrapperParentId
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
    return {
      id,
      source,
      target,
      sourceHandle,
      targetHandle,
      passthrough: omit(raw, EDGE_OWN_KEYS),
    };
  });
}

/**
 * Parse a Dify workflow YAML document into a StudioWorkflow.
 * Throws WorkflowParseError with a human-readable message on malformed input.
 * Nodes lacking a position are auto-laid-out; explicit positions are kept.
 */
export function workflowTextExceedsLimit(text: string): boolean {
  return (
    text.length > MAX_WORKFLOW_BYTES ||
    new TextEncoder().encode(text).byteLength > MAX_WORKFLOW_BYTES
  );
}

function assertWorkflowTextSize(text: string): void {
  if (workflowTextExceedsLimit(text)) {
    throw new WorkflowParseError('Workflow document exceeds the 1 MiB import limit.');
  }
}

export function parseWorkflowYaml(text: string): StudioWorkflow {
  assertWorkflowTextSize(text);
  let doc: unknown;
  try {
    doc = load(text, {
      maxAliases: MAX_YAML_ALIASES,
      maxDepth: MAX_WORKFLOW_DEPTH,
      maxTotalMergeKeys: MAX_YAML_MERGE_KEYS,
    });
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new WorkflowParseError(`Not a valid YAML document: ${reason}`);
  }
  return parseWorkflowObject(doc);
}

/** Parse TOML at the file/UI boundary, then enter the same canonical object path as JSON/YAML. */
export function parseWorkflowToml(text: string): StudioWorkflow {
  assertWorkflowTextSize(text);
  assertTomlContainerDepth(text);
  let document: unknown;
  try {
    document = parseToml(text, {
      integersAsBigInt: 'asNeeded',
      maxDepth: MAX_WORKFLOW_DEPTH,
    });
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new WorkflowParseError(`Not a valid TOML document: ${reason}`);
  }
  return parseWorkflowObject(normalizeTomlJson(document));
}

/** Bound recursive TOML arrays/inline tables before invoking the parser. */
function assertTomlContainerDepth(text: string): void {
  let quote: '"' | "'" | '"""' | "'''" | null = null;
  let comment = false;
  let depth = 0;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (comment) {
      if (character === '\n' || character === '\r') comment = false;
      continue;
    }
    if (quote !== null) {
      if ((quote === '"' || quote === '"""') && character === '\\') {
        index += 1;
        continue;
      }
      if (text.startsWith(quote, index)) {
        index += quote.length - 1;
        quote = null;
      }
      continue;
    }
    if (character === '#') {
      comment = true;
      continue;
    }
    if (text.startsWith('"""', index) || text.startsWith("'''", index)) {
      quote = text.slice(index, index + 3) as '"""' | "'''";
      index += 2;
      continue;
    }
    if (character === '"' || character === "'") {
      quote = character;
      continue;
    }
    if (character === '[' || character === '{') {
      depth += 1;
      if (depth > MAX_WORKFLOW_DEPTH) {
        throw new WorkflowParseError(
          `Workflow exceeds the maximum depth of ${MAX_WORKFLOW_DEPTH}.`,
        );
      }
    } else if ((character === ']' || character === '}') && depth > 0) {
      depth -= 1;
    }
  }
}

function normalizeTomlJson(value: unknown): unknown {
  let count = 0;

  const visit = (item: unknown, depth: number): unknown => {
    count += 1;
    if (count > MAX_WORKFLOW_VALUES) {
      throw new WorkflowParseError(`Workflow contains more than ${MAX_WORKFLOW_VALUES} values.`);
    }
    if (depth > MAX_WORKFLOW_DEPTH) {
      throw new WorkflowParseError(
        `Workflow exceeds the maximum depth of ${MAX_WORKFLOW_DEPTH}.`,
      );
    }
    if (item === null || typeof item === 'string' || typeof item === 'boolean') return item;
    if (typeof item === 'number') {
      if (!Number.isFinite(item)) throw new WorkflowParseError('Workflow cannot contain NaN/Inf.');
      return item;
    }
    if (Array.isArray(item)) return item.map((child) => visit(child, depth + 1));
    if (isRecord(item)) {
      const prototype = Object.getPrototypeOf(item) as unknown;
      if (prototype !== null && prototype !== Object.prototype) {
        throw new WorkflowParseError('TOML workflow must contain JSON-compatible values only.');
      }
      const normalized: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
      for (const [key, child] of Object.entries(item)) {
        normalized[key] = visit(child, depth + 1);
      }
      return normalized;
    }
    throw new WorkflowParseError('TOML workflow must contain JSON-compatible values only.');
  };

  return visit(value, 0);
}

function parseWorkflowObject(doc: unknown): StudioWorkflow {
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
    typeof rawVersion === 'string'
      ? rawVersion
      : rawVersion === undefined
        ? '0.1.5'
        : String(rawVersion);

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

/** Container id field used on children of a real container node. */
export function containerIdField(parentType: string): ContainerIdField | undefined {
  if (parentType === 'iteration') return 'iteration_id';
  if (parentType === 'loop') return 'loop_id';
  return undefined;
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
  idField: ContainerIdField | undefined,
): StudioNodeData {
  if (parentId !== undefined) {
    // A dangling/non-container parent must not be invented as iteration
    // membership. Keep the imported data untouched until the graph has a real
    // iteration/loop parent again.
    if (idField === undefined) return data;
    const otherField: ContainerIdField = idField === 'loop_id' ? 'iteration_id' : 'loop_id';
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

interface ContainerMembership {
  field: ContainerIdField;
  parentId: string;
}

function containerMembership(
  node: StudioNode,
  typeById: ReadonlyMap<string, string>,
): ContainerMembership | undefined {
  if (node.parentId === undefined) return undefined;
  const field = containerIdField(typeById.get(node.parentId) ?? '');
  return field === undefined ? undefined : { field, parentId: node.parentId };
}

function absolutePosition(
  node: StudioNode,
  nodeById: ReadonlyMap<string, StudioNode>,
  typeById: ReadonlyMap<string, string>,
): XY {
  let x = node.position.x;
  let y = node.position.y;
  let current = node;
  const seen = new Set<string>([node.id]);
  for (;;) {
    const membership = containerMembership(current, typeById);
    if (membership === undefined || seen.has(membership.parentId)) break;
    const parent = nodeById.get(membership.parentId);
    if (parent === undefined) break;
    seen.add(parent.id);
    x += parent.position.x;
    y += parent.position.y;
    current = parent;
  }
  return { x, y };
}

function edgeMembership(
  edge: StudioEdge,
  nodeById: ReadonlyMap<string, StudioNode>,
  typeById: ReadonlyMap<string, string>,
): ContainerMembership | undefined {
  const source = nodeById.get(edge.source);
  const target = nodeById.get(edge.target);
  if (source === undefined || target === undefined) return undefined;
  const sourceMembership = containerMembership(source, typeById);
  const targetMembership = containerMembership(target, typeById);
  return sourceMembership !== undefined &&
    targetMembership !== undefined &&
    sourceMembership.parentId === targetMembership.parentId &&
    sourceMembership.field === targetMembership.field
    ? sourceMembership
    : undefined;
}

function syncedEdgeData(
  edge: StudioEdge,
  nodeById: ReadonlyMap<string, StudioNode>,
  typeById: ReadonlyMap<string, string>,
): Record<string, unknown> {
  const raw = isRecord(edge.passthrough['data']) ? edge.passthrough['data'] : {};
  const data = { ...raw };
  const sourceType = typeById.get(edge.source);
  const targetType = typeById.get(edge.target);
  if (sourceType !== undefined) data['sourceType'] = sourceType;
  if (targetType !== undefined) data['targetType'] = targetType;

  delete data['iteration_id'];
  delete data['loop_id'];
  const membership = edgeMembership(edge, nodeById, typeById);
  data['isInIteration'] = membership?.field === 'iteration_id';
  data['isInLoop'] = membership?.field === 'loop_id';
  if (membership !== undefined) data[membership.field] = membership.parentId;
  return data;
}

/** Serialize a StudioWorkflow back into Dify workflow YAML. */
export function serializeWorkflowYaml(wf: StudioWorkflow): string {
  const typeById = new Map(wf.nodes.map((n) => [n.id, n.type] as const));
  const nodeById = new Map(wf.nodes.map((n) => [n.id, n] as const));
  const nodes = wf.nodes.map((node) => {
    const membership = containerMembership(node, typeById);
    const passthrough = omit(node.passthrough, [...NODE_OWN_KEYS, ...NODE_DERIVED_KEYS]);
    // `extent` is a React Flow editing hint. Preserve it only while the node
    // still belongs to a real container; never export a stale detached hint.
    if (membership === undefined) delete passthrough['extent'];
    if (
      membership === undefined &&
      node.parentId === undefined &&
      ('iteration_id' in node.data || 'loop_id' in node.data)
    ) {
      delete passthrough['zIndex'];
    }
    return {
      type: 'custom',
      selected: false,
      sourcePosition: 'right',
      targetPosition: 'left',
      width: 244,
      height: 90,
      ...passthrough,
      id: node.id,
      position: { x: node.position.x, y: node.position.y },
      positionAbsolute: absolutePosition(node, nodeById, typeById),
      data: withSyncedContainerId(node.data, node.parentId, membership?.field),
      ...(membership !== undefined ? { parentId: membership.parentId, zIndex: 1002 } : {}),
    };
  });
  const edges = wf.edges.map((edge) => {
    const rawData = isRecord(edge.passthrough['data']) ? edge.passthrough['data'] : {};
    const membership = edgeMembership(edge, nodeById, typeById);
    const wasInContainer = rawData['isInIteration'] === true || rawData['isInLoop'] === true;
    const passthrough = omit(edge.passthrough, [...EDGE_OWN_KEYS, 'data', 'zIndex']);
    const rawZIndex = edge.passthrough['zIndex'];
    const zIndex =
      membership !== undefined
        ? wasInContainer && typeof rawZIndex === 'number'
          ? rawZIndex
          : 1002
        : wasInContainer
          ? 0
          : typeof rawZIndex === 'number'
            ? rawZIndex
            : 0;
    return {
      type: 'custom',
      ...passthrough,
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.sourceHandle,
      targetHandle: edge.targetHandle,
      data: syncedEdgeData(edge, nodeById, typeById),
      zIndex,
    };
  });
  const { kind, ...docExtras } = wf.docPassthrough;
  const doc = {
    ...omit(docExtras, ['app', 'kind', 'version', 'workflow']),
    app: {
      description: '',
      icon: '🧩',
      icon_background: '#E4FBCC',
      use_icon_as_answer_icon: false,
      ...omit(wf.appPassthrough, ['mode', 'name']),
      mode: wf.mode,
      name: wf.name,
    },
    kind: kind ?? 'app',
    version: wf.version,
    workflow: {
      conversation_variables: [],
      environment_variables: [],
      features: {},
      ...omit(wf.workflowPassthrough, ['graph']),
      graph: {
        viewport: { x: 0, y: 0, zoom: 0.7 },
        ...omit(wf.graphPassthrough, ['nodes', 'edges']),
        nodes,
        edges,
      },
    },
  };
  return dump(doc, { noRefs: true, lineWidth: -1 });
}
