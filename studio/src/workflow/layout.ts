/**
 * Dagre-based auto-layout for StudioWorkflow graphs. Pure and immutable:
 * the input workflow is never mutated.
 *
 * Two-level scheme:
 * 1. Each iteration container's children are laid out as an independent
 *    left-to-right subgraph with positions RELATIVE to the container
 *    (offset below a ~40px header). The container is sized to fit its
 *    children; the size is stored in the container node's passthrough
 *    (`width`/`height`) unless already present.
 * 2. The top-level graph is laid out left-to-right with containers treated
 *    as single large boxes.
 *
 * Nodes with an explicitly known position keep it; only nodes with a
 * missing position (non-finite x/y sentinel) are filled, unless `force`.
 */

import { Graph, layout as runDagre } from '@dagrejs/dagre';
import type { EdgeLabel, GraphLabel, NodeLabel } from '@dagrejs/dagre';

import { getNodeDefinition } from './registry';
import type { StudioEdge, StudioNode, StudioWorkflow, XY } from './types';

export const DEFAULT_NODE_WIDTH = 240;
export const DEFAULT_NODE_HEIGHT = 80;
export const DEFAULT_CONTAINER_WIDTH = 480;
export const DEFAULT_CONTAINER_HEIGHT = 240;
export const CONTAINER_HEADER_HEIGHT = 40;

const NODE_SEP = 40;
const RANK_SEP = 80;
const CONTAINER_PADDING_X = 40;
const CONTAINER_PADDING_TOP = CONTAINER_HEADER_HEIGHT + 24;
const CONTAINER_PADDING_BOTTOM = 24;

/** Sentinel for "position not given in the source document". */
export function missingPosition(): XY {
  return { x: Number.NaN, y: Number.NaN };
}

/** True when the position is real (finite), false for the missing sentinel. */
export function hasFinitePosition(position: XY): boolean {
  return Number.isFinite(position.x) && Number.isFinite(position.y);
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

interface Dims {
  width: number;
  height: number;
}

/** Dimensions of a plain (non-container) node, honoring passthrough width/height. */
function plainNodeDims(node: StudioNode): Dims {
  return {
    width: numberOr(node.passthrough['width'], DEFAULT_NODE_WIDTH),
    height: numberOr(node.passthrough['height'], DEFAULT_NODE_HEIGHT),
  };
}

/**
 * Run dagre (rankdir LR) over the given nodes/edges and return TOP-LEFT
 * corner positions (dagre reports centers), rounded to integers.
 */
function runLayout(
  nodes: StudioNode[],
  edges: readonly Pick<StudioEdge, 'source' | 'target'>[],
  dims: (node: StudioNode) => Dims,
): Map<string, XY> {
  const g = new Graph<GraphLabel, NodeLabel, EdgeLabel>();
  g.setGraph({ rankdir: 'LR', nodesep: NODE_SEP, ranksep: RANK_SEP });
  g.setDefaultEdgeLabel(() => ({}));
  const ids = new Set(nodes.map((n) => n.id));
  for (const node of nodes) {
    const { width, height } = dims(node);
    g.setNode(node.id, { width, height });
  }
  for (const edge of edges) {
    if (ids.has(edge.source) && ids.has(edge.target) && edge.source !== edge.target) {
      g.setEdge(edge.source, edge.target);
    }
  }
  runDagre(g);
  const positions = new Map<string, XY>();
  for (const node of nodes) {
    const label = g.node(node.id);
    positions.set(node.id, {
      x: Math.round((label.x ?? 0) - label.width / 2),
      y: Math.round((label.y ?? 0) - label.height / 2),
    });
  }
  return positions;
}

/**
 * Fill missing node positions via dagre auto-layout. With `force`, all
 * positions are recomputed. Returns a new StudioWorkflow; the input (and
 * its node `data` objects, which are never touched) stays intact.
 */
export function autoLayoutWorkflow(wf: StudioWorkflow, opts?: { force?: boolean }): StudioWorkflow {
  const force = opts?.force ?? false;
  const nodes: StudioNode[] = wf.nodes.map((node) => ({
    ...node,
    position: { ...node.position },
    passthrough: { ...node.passthrough },
  }));
  const byId = new Map(nodes.map((n) => [n.id, n] as const));

  const childrenByParent = new Map<string, StudioNode[]>();
  for (const node of nodes) {
    if (node.parentId !== undefined && byId.has(node.parentId)) {
      const siblings = childrenByParent.get(node.parentId) ?? [];
      siblings.push(node);
      childrenByParent.set(node.parentId, siblings);
    }
  }
  const isContainer = (node: StudioNode): boolean =>
    getNodeDefinition(node.type).isContainer || childrenByParent.has(node.id);

  // Level 1: children of each container, in container-relative coordinates.
  for (const [parentId, children] of childrenByParent) {
    const childIds = new Set(children.map((c) => c.id));
    if (force || children.some((c) => !hasFinitePosition(c.position))) {
      const innerEdges = wf.edges.filter((e) => childIds.has(e.source) && childIds.has(e.target));
      const laidOut = runLayout(children, innerEdges, plainNodeDims);
      let minX = Number.POSITIVE_INFINITY;
      let minY = Number.POSITIVE_INFINITY;
      for (const pos of laidOut.values()) {
        minX = Math.min(minX, pos.x);
        minY = Math.min(minY, pos.y);
      }
      for (const child of children) {
        if (force || !hasFinitePosition(child.position)) {
          const pos = laidOut.get(child.id);
          if (pos !== undefined) {
            child.position = {
              x: pos.x - minX + CONTAINER_PADDING_X,
              y: pos.y - minY + CONTAINER_PADDING_TOP,
            };
          }
        }
      }
    }
    // Size the container to fit its children (final positions).
    let maxRight = 0;
    let maxBottom = 0;
    for (const child of children) {
      if (!hasFinitePosition(child.position)) continue;
      const { width, height } = plainNodeDims(child);
      maxRight = Math.max(maxRight, child.position.x + width);
      maxBottom = Math.max(maxBottom, child.position.y + height);
    }
    const parent = byId.get(parentId);
    if (parent !== undefined) {
      const fittedWidth = maxRight > 0 ? Math.round(maxRight + CONTAINER_PADDING_X) : DEFAULT_CONTAINER_WIDTH;
      const fittedHeight =
        maxBottom > 0 ? Math.round(maxBottom + CONTAINER_PADDING_BOTTOM) : DEFAULT_CONTAINER_HEIGHT;
      if (typeof parent.passthrough['width'] !== 'number') parent.passthrough['width'] = fittedWidth;
      if (typeof parent.passthrough['height'] !== 'number') parent.passthrough['height'] = fittedHeight;
    }
  }

  // Containers without children still get a default size recorded.
  for (const node of nodes) {
    if (isContainer(node)) {
      if (typeof node.passthrough['width'] !== 'number') {
        node.passthrough['width'] = DEFAULT_CONTAINER_WIDTH;
      }
      if (typeof node.passthrough['height'] !== 'number') {
        node.passthrough['height'] = DEFAULT_CONTAINER_HEIGHT;
      }
    }
  }

  // Level 2: top-level graph, containers as single boxes.
  const topNodes = nodes.filter((n) => n.parentId === undefined || !byId.has(n.parentId));
  if (force || topNodes.some((n) => !hasFinitePosition(n.position))) {
    // Edges whose endpoint sits inside a container are remapped to the container.
    const resolveTop = (id: string): string => {
      let current = byId.get(id);
      const seen = new Set<string>();
      while (
        current !== undefined &&
        current.parentId !== undefined &&
        byId.has(current.parentId) &&
        !seen.has(current.id)
      ) {
        seen.add(current.id);
        current = byId.get(current.parentId);
      }
      return current?.id ?? id;
    };
    const topEdges = wf.edges.map((e) => ({
      source: resolveTop(e.source),
      target: resolveTop(e.target),
    }));
    const topDims = (node: StudioNode): Dims =>
      isContainer(node)
        ? {
            width: numberOr(node.passthrough['width'], DEFAULT_CONTAINER_WIDTH),
            height: numberOr(node.passthrough['height'], DEFAULT_CONTAINER_HEIGHT),
          }
        : plainNodeDims(node);
    const laidOut = runLayout(topNodes, topEdges, topDims);
    for (const node of topNodes) {
      if (force || !hasFinitePosition(node.position)) {
        const pos = laidOut.get(node.id);
        if (pos !== undefined) node.position = pos;
      }
    }
  }

  return { ...wf, nodes };
}
