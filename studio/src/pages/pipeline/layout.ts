/**
 * Pure layout helper: TopologyGraph -> positioned React Flow nodes/edges
 * using dagre (rankdir LR).
 */

import dagre from '@dagrejs/dagre';
import { MarkerType, Position } from '@xyflow/react';
import type { Edge, Node } from '@xyflow/react';

import type { TopologyGraph } from '../../api/types';

export interface TopologyNodeData extends Record<string, unknown> {
  label: string;
  kind: string;
  domain?: string;
  symbol?: string;
}

export type TopologyFlowNode = Node<TopologyNodeData, 'topology'>;

const EDGE_COLOR = 'rgba(255, 255, 255, 0.28)';
const MARKER_COLOR = 'rgba(255, 255, 255, 0.4)';

function nodeSize(label: string, symbol: string | undefined, hasDomain: boolean, kind: string) {
  const textLen = Math.max(label.length, (symbol?.length ?? 0) + 2);
  const base = kind === 'gate' ? 170 : 150;
  const width = Math.min(280, Math.max(base, 32 + textLen * 7.2));
  const height = hasDomain || symbol ? 64 : 48;
  return { width, height };
}

function edgeDash(kind: string | undefined): string | undefined {
  if (kind === 'conditional') return '7 5';
  if (kind === 'data') return '2 4';
  return undefined; // flow: solid
}

export function layoutTopology(graph: TopologyGraph): {
  nodes: TopologyFlowNode[];
  edges: Edge[];
} {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'LR', ranksep: 90, nodesep: 36, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));

  const knownIds = new Set(graph.nodes.map((n) => n.id));
  const sizes = new Map<string, { width: number; height: number }>();

  for (const node of graph.nodes) {
    const size = nodeSize(node.label, node.symbol, Boolean(node.domain), node.kind);
    sizes.set(node.id, size);
    g.setNode(node.id, size);
  }

  const validEdges = graph.edges.filter((e) => knownIds.has(e.src) && knownIds.has(e.dst));
  for (const edge of validEdges) {
    g.setEdge(edge.src, edge.dst);
  }

  dagre.layout(g);

  const nodes: TopologyFlowNode[] = graph.nodes.map((node) => {
    const size = sizes.get(node.id) ?? { width: 150, height: 48 };
    const pos = g.node(node.id);
    return {
      id: node.id,
      type: 'topology',
      position: { x: pos.x - size.width / 2, y: pos.y - size.height / 2 },
      width: size.width,
      height: size.height,
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      draggable: false,
      connectable: false,
      selectable: false,
      data: {
        label: node.label,
        kind: node.kind,
        domain: node.domain,
        symbol: node.symbol,
      },
    };
  });

  const edges: Edge[] = validEdges.map((edge, i) => {
    const dash = edgeDash(edge.kind);
    return {
      id: `e${i}:${edge.src}->${edge.dst}`,
      source: edge.src,
      target: edge.dst,
      label: edge.label,
      selectable: false,
      style: {
        stroke: EDGE_COLOR,
        strokeWidth: 1.5,
        ...(dash ? { strokeDasharray: dash } : {}),
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        width: 14,
        height: 14,
        color: MARKER_COLOR,
      },
    };
  });

  return { nodes, edges };
}
