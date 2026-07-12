/**
 * Upstream-variable model for the workflow editor: which nodes are
 * topologically upstream of a node, which output variables each node type
 * exposes (Dify semantics), and validation of {{#nodeId.field#}} references.
 *
 * Pure module: no React, no store access.
 */

import type { StudioNodeData } from '../../../workflow/types';

/** Minimal node shape (StudioNode and adapted React Flow nodes satisfy it). */
export interface VariableNode {
  id: string;
  /** Iteration container id when the node lives inside one. */
  parentId?: string | undefined;
  data: StudioNodeData;
}

/** Minimal edge shape (StudioEdge / StudioFlowEdge both satisfy it). */
export interface VariableEdge {
  source: string;
  target: string;
}

/**
 * Marker `variable` value for upstream nodes whose output set is not
 * statically known (tool / end / answer / if-else / unknown types): pickers
 * skip these entries and the validator accepts any field on that node.
 */
export const OPEN_OUTPUTS = '';

export interface AvailableVariable {
  nodeId: string;
  /** Node title, falling back to the node id. */
  nodeTitle: string;
  nodeType: string;
  /** Output variable name, or OPEN_OUTPUTS ('') for open-output nodes. */
  variable: string;
}

export interface InvalidVariableRef {
  /** The full matched reference, e.g. "{{#llm_1.text#}}". */
  ref: string;
  nodeId: string;
  variable: string;
  reason: 'unknown-node' | 'unknown-variable';
}

/** Ancestor container chain of a node (nearest first), cycle-safe. */
function ancestorIds(byId: Map<string, VariableNode>, nodeId: string): string[] {
  const chain: string[] = [];
  const seen = new Set<string>([nodeId]);
  let current = byId.get(nodeId);
  while (current !== undefined) {
    const parentId = current.parentId;
    if (parentId === undefined || parentId === '' || seen.has(parentId)) break;
    seen.add(parentId);
    chain.push(parentId);
    current = byId.get(parentId);
  }
  return chain;
}

/**
 * All nodes topologically upstream of `nodeId`, in document order: reverse
 * BFS over the edges, plus (for nodes inside an iteration container) the
 * container itself and everything upstream of it. Children of an upstream
 * container are NOT included — inner nodes are not addressable from outside.
 */
export function upstreamNodes(
  nodes: readonly VariableNode[],
  edges: readonly VariableEdge[],
  nodeId: string,
): VariableNode[] {
  const byId = new Map(nodes.map((n) => [n.id, n] as const));
  const preds = new Map<string, string[]>();
  for (const edge of edges) {
    const list = preds.get(edge.target);
    if (list === undefined) preds.set(edge.target, [edge.source]);
    else list.push(edge.source);
  }

  const visited = new Set<string>();
  const queue: string[] = [];
  const enqueue = (id: string) => {
    if (id !== nodeId && !visited.has(id) && byId.has(id)) {
      visited.add(id);
      queue.push(id);
    }
  };

  for (const pred of preds.get(nodeId) ?? []) enqueue(pred);
  for (const ancestor of ancestorIds(byId, nodeId)) enqueue(ancestor);
  for (let i = 0; i < queue.length; i += 1) {
    for (const pred of preds.get(queue[i]!) ?? []) enqueue(pred);
  }
  return nodes.filter((n) => visited.has(n.id));
}

/** Collect non-empty string values of `key` from a list of mappings. */
function namesFrom(list: unknown, key: string): string[] {
  if (!Array.isArray(list)) return [];
  const names: string[] = [];
  for (const item of list) {
    if (typeof item === 'object' && item !== null && !Array.isArray(item)) {
      const name = (item as Record<string, unknown>)[key];
      if (typeof name === 'string' && name !== '') names.push(name);
    }
  }
  return names;
}

/**
 * Output variable names a node exposes to downstream nodes (Dify semantics).
 * Types without a statically known output set (tool, end, answer, if-else,
 * assigner — which officially has no outputs — and unknown imported types)
 * return [] — treat their outputs as open.
 */
export function nodeOutputVariables(node: VariableNode): string[] {
  const data = node.data;
  switch (data.type) {
    case 'start':
      return [...namesFrom(data['variables'], 'variable'), 'sys.query', 'sys.files'];
    case 'llm':
      return ['text'];
    case 'code': {
      const outputs = data['outputs'];
      return typeof outputs === 'object' && outputs !== null && !Array.isArray(outputs)
        ? Object.keys(outputs)
        : [];
    }
    case 'parameter-extractor':
      return [...namesFrom(data['parameters'], 'name'), '__is_success', '__reason'];
    case 'question-classifier':
      return ['class_name'];
    case 'knowledge-retrieval':
      return ['result'];
    case 'template-transform':
      return ['output'];
    case 'iteration':
      return ['output'];
    case 'http-request':
      return ['body', 'status_code', 'headers', 'files'];
    case 'variable-aggregator': {
      const advanced = data['advanced_settings'];
      if (
        typeof advanced === 'object' &&
        advanced !== null &&
        !Array.isArray(advanced) &&
        (advanced as Record<string, unknown>)['group_enabled'] === true
      ) {
        const groups = (advanced as Record<string, unknown>)['groups'];
        return namesFrom(groups, 'group_name').map((name) => `${name}.output`);
      }
      return ['output'];
    }
    case 'document-extractor':
      return ['text'];
    case 'loop':
      return [...namesFrom(data['loop_variables'], 'label'), 'loop_round'];
    default:
      return [];
  }
}

/** Variables an iteration container exposes to the nodes inside it (iteration-specific; loop containers expose their loop_variables labels instead). */
const CONTAINER_ITEM_VARIABLES = ['item', 'index'] as const;

/** Variables a container exposes to the nodes inside it, per container type. */
function containerScopedVariables(node: VariableNode): string[] {
  return node.data.type === 'loop'
    ? namesFrom(node.data['loop_variables'], 'label')
    : [...CONTAINER_ITEM_VARIABLES];
}

/**
 * Flat list of variables referenceable from `nodeId`: for every upstream
 * node, its output variables (ancestor iteration containers expose item/index
 * instead of output). Open-output nodes contribute a single OPEN_OUTPUTS
 * marker entry so the validator still knows the node is upstream.
 */
export function availableVariables(
  nodes: readonly VariableNode[],
  edges: readonly VariableEdge[],
  nodeId: string,
): AvailableVariable[] {
  const byId = new Map(nodes.map((n) => [n.id, n] as const));
  const ancestors = new Set(ancestorIds(byId, nodeId));
  const result: AvailableVariable[] = [];
  for (const node of upstreamNodes(nodes, edges, nodeId)) {
    const title = typeof node.data.title === 'string' && node.data.title !== '' ? node.data.title : node.id;
    const variables = ancestors.has(node.id)
      ? containerScopedVariables(node)
      : nodeOutputVariables(node);
    if (variables.length === 0) {
      result.push({ nodeId: node.id, nodeTitle: title, nodeType: node.data.type, variable: OPEN_OUTPUTS });
      continue;
    }
    for (const variable of variables) {
      result.push({ nodeId: node.id, nodeTitle: title, nodeType: node.data.type, variable });
    }
  }
  return result;
}

/** Mirrors the backend template-ref pattern (src/ragspine/dify/ir/lower.py). */
const TEMPLATE_REF = /\{\{#\s*([^#}]+?)\s*#\}\}/g;

/** System namespaces resolved by the runtime, never validated as node ids. */
const SYSTEM_NAMESPACES = new Set(['sys', 'env', 'conversation']);

/**
 * Extract every {{#nodeId.field#}} reference in `text` and report those that
 * do not resolve against `available`: node not upstream, or (for nodes with a
 * statically known output set) unknown variable name. Open-output nodes,
 * single-segment refs to known nodes, and system namespaces (sys/env/
 * conversation) are never flagged. Duplicate failing refs are reported once.
 */
export function validateVariableRefs(
  text: string,
  available: readonly AvailableVariable[],
): InvalidVariableRef[] {
  const byNode = new Map<string, { open: boolean; variables: Set<string> }>();
  for (const item of available) {
    let entry = byNode.get(item.nodeId);
    if (entry === undefined) {
      entry = { open: false, variables: new Set() };
      byNode.set(item.nodeId, entry);
    }
    if (item.variable === OPEN_OUTPUTS) entry.open = true;
    else entry.variables.add(item.variable);
  }

  const invalid: InvalidVariableRef[] = [];
  const seen = new Set<string>();
  for (const match of text.matchAll(TEMPLATE_REF)) {
    const segments = (match[1] ?? '').split('.').filter((s) => s !== '');
    const nodeId = segments[0] ?? '';
    if (nodeId === '' || SYSTEM_NAMESPACES.has(nodeId)) continue;
    const ref = match[0];
    if (seen.has(ref)) continue;
    const variable = segments.slice(1).join('.');
    const entry = byNode.get(nodeId);
    if (entry === undefined) {
      seen.add(ref);
      invalid.push({ ref, nodeId, variable, reason: 'unknown-node' });
    } else if (!entry.open && variable !== '' && !entry.variables.has(variable)) {
      seen.add(ref);
      invalid.push({ ref, nodeId, variable, reason: 'unknown-variable' });
    }
  }
  return invalid;
}
