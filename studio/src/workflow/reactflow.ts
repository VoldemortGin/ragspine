/**
 * Adapters between the pure StudioWorkflow model and React Flow (@xyflow/react)
 * canvas state. Type-only imports from @xyflow/react keep this module
 * React-free at runtime.
 *
 * RF node data payload shape (StudioFlowNodeData): the FULL Dify `data`
 * object rides in `data.dify` and the node-level passthrough bag rides in
 * `data.passthrough`, so a toFlow -> fromFlow round trip loses nothing.
 * Container width/height live in RF `style` (sourced from passthrough) and
 * are written back to passthrough by fromFlow.
 */

import type { Edge, Node } from '@xyflow/react';

import { withSyncedIterationId } from './convert';
import { DEFAULT_CONTAINER_HEIGHT, DEFAULT_CONTAINER_WIDTH } from './layout';
import { getNodeDefinition } from './registry';
import type { StudioEdge, StudioNode, StudioNodeData, StudioWorkflow } from './types';

/** React Flow component-type key for regular Dify nodes. */
export const DIFY_NODE = 'dify-node';
/** React Flow component-type key for iteration container (group) nodes. */
export const DIFY_ITERATION = 'dify-iteration';

export interface StudioFlowNodeData extends Record<string, unknown> {
  /** The full Dify node data object (known + unknown keys), never trimmed. */
  dify: StudioNodeData;
  /** Node-level passthrough bag (width, height, positionAbsolute, ...). */
  passthrough: Record<string, unknown>;
}

export type StudioFlowNode = Node<StudioFlowNodeData>;

export interface StudioFlowEdgeData extends Record<string, unknown> {
  /** Edge-level passthrough bag (data, type, zIndex, ...). */
  passthrough: Record<string, unknown>;
}

export type StudioFlowEdge = Edge<StudioFlowEdgeData>;

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

/**
 * Convert a StudioWorkflow into React Flow nodes/edges. Container children
 * are emitted AFTER their parent (React Flow requirement) with
 * parentId + extent 'parent' and container-relative positions.
 */
export function toFlow(wf: StudioWorkflow): { nodes: StudioFlowNode[]; edges: StudioFlowEdge[] } {
  const parentIds = new Set<string>();
  for (const node of wf.nodes) {
    if (node.parentId !== undefined) parentIds.add(node.parentId);
  }
  const isContainer = (node: StudioNode): boolean =>
    getNodeDefinition(node.type).isContainer || parentIds.has(node.id);

  // Parents are top-level in Dify, so "top-level first" puts every child
  // after its parent while otherwise keeping document order.
  const ordered = [
    ...wf.nodes.filter((n) => n.parentId === undefined),
    ...wf.nodes.filter((n) => n.parentId !== undefined),
  ];

  const nodes = ordered.map((node): StudioFlowNode => {
    const container = isContainer(node);
    const rf: StudioFlowNode = {
      id: node.id,
      type: container ? DIFY_ITERATION : DIFY_NODE,
      position: { x: node.position.x, y: node.position.y },
      data: { dify: node.data, passthrough: node.passthrough },
    };
    if (node.parentId !== undefined) {
      rf.parentId = node.parentId;
      rf.extent = 'parent';
    }
    if (container) {
      rf.style = {
        width: numberOr(node.passthrough['width'], DEFAULT_CONTAINER_WIDTH),
        height: numberOr(node.passthrough['height'], DEFAULT_CONTAINER_HEIGHT),
      };
    }
    return rf;
  });

  const edges = wf.edges.map(
    (edge): StudioFlowEdge => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.sourceHandle,
      targetHandle: edge.targetHandle,
      data: { passthrough: edge.passthrough },
    }),
  );

  return { nodes, edges };
}

/**
 * Rebuild a StudioWorkflow from live React Flow canvas state. Doc-level
 * meta and passthrough bags come from `base`; parentId is synced back into
 * data.iteration_id (set when contained, dropped when detached).
 */
export function fromFlow(
  nodes: StudioFlowNode[],
  edges: StudioFlowEdge[],
  base: StudioWorkflow,
): StudioWorkflow {
  const outNodes = nodes.map((rf): StudioNode => {
    const parentId = rf.parentId;
    const data = withSyncedIterationId(rf.data.dify, parentId);
    const passthrough = { ...rf.data.passthrough };
    if (rf.type === DIFY_ITERATION) {
      const width = rf.style?.width;
      const height = rf.style?.height;
      if (typeof width === 'number') passthrough['width'] = width;
      if (typeof height === 'number') passthrough['height'] = height;
    }
    return {
      id: rf.id,
      type: data.type,
      position: { x: rf.position.x, y: rf.position.y },
      ...(parentId !== undefined ? { parentId } : {}),
      data,
      passthrough,
    };
  });

  const outEdges = edges.map(
    (rf): StudioEdge => ({
      id: rf.id,
      source: rf.source,
      target: rf.target,
      sourceHandle: rf.sourceHandle ?? 'source',
      targetHandle: rf.targetHandle ?? 'target',
      passthrough: rf.data?.passthrough ?? {},
    }),
  );

  return {
    name: base.name,
    mode: base.mode,
    version: base.version,
    appPassthrough: { ...base.appPassthrough },
    docPassthrough: { ...base.docPassthrough },
    workflowPassthrough: { ...base.workflowPassthrough },
    graphPassthrough: { ...base.graphPassthrough },
    nodes: outNodes,
    edges: outEdges,
  };
}
