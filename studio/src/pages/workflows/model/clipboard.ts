/**
 * In-memory clipboard for canvas nodes/edges. Copy captures deep clones of
 * the selection (container children included, fully-internal edges kept);
 * paste materializes them with fresh ids, remapped parentId/iteration_id,
 * rewritten {{#nodeId.var#}} references and an offset that grows on each
 * paste of the same content. Module-level state survives workflow switches
 * (SPA), enabling cross-workflow paste; it is not persisted anywhere.
 */

import { withSyncedIterationId } from '../../../workflow/convert';
import type { StudioFlowEdge, StudioFlowNode } from '../../../workflow/reactflow';
import type { StudioNodeData, XY } from '../../../workflow/types';
import { uniqueEdgeId, uniqueNodeId } from './ids';

export const PASTE_OFFSET = 40;

/** Mirrors the template-ref pattern in model/variables.ts. */
const TEMPLATE_REF = /\{\{#\s*([^#}]+?)\s*#\}\}/g;

interface ClipboardNode {
  /** Deep node clone (whitelisted fields, selection flag stripped). */
  node: StudioFlowNode;
  /** Canvas-absolute position at copy time (for detached pastes). */
  absolute: XY;
}

export interface ClipboardContent {
  nodes: ClipboardNode[];
  edges: StudioFlowEdge[];
}

export interface MaterializedClones {
  nodes: StudioFlowNode[];
  edges: StudioFlowEdge[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Selected ids expanded with every descendant of a selected container. */
function expandWithChildren(
  nodes: readonly StudioFlowNode[],
  ids: readonly string[],
): Set<string> {
  const included = new Set(ids);
  let grew = true;
  while (grew) {
    grew = false;
    for (const n of nodes) {
      if (n.parentId !== undefined && included.has(n.parentId) && !included.has(n.id)) {
        included.add(n.id);
        grew = true;
      }
    }
  }
  return included;
}

/** Canvas-absolute position of a node (walks the parent chain, cycle-safe). */
function absolutePosition(byId: Map<string, StudioFlowNode>, node: StudioFlowNode): XY {
  let x = node.position.x;
  let y = node.position.y;
  const seen = new Set<string>([node.id]);
  let parentId = node.parentId;
  while (parentId !== undefined && !seen.has(parentId)) {
    const parent = byId.get(parentId);
    if (parent === undefined) break;
    seen.add(parent.id);
    x += parent.position.x;
    y += parent.position.y;
    parentId = parent.parentId;
  }
  return { x, y };
}

function cloneNode(src: StudioFlowNode): StudioFlowNode {
  const clone: StudioFlowNode = {
    id: src.id,
    type: src.type,
    position: { x: src.position.x, y: src.position.y },
    data: {
      dify: structuredClone(src.data.dify),
      passthrough: structuredClone(src.data.passthrough),
    },
  };
  if (src.parentId !== undefined) {
    clone.parentId = src.parentId;
    clone.extent = src.extent;
  }
  if (src.style !== undefined) clone.style = { ...src.style };
  return clone;
}

/** Rewrite {{#nodeId.var#}} references whose node id was remapped. */
function rewriteRefs(text: string, idMap: ReadonlyMap<string, string>): string {
  return text.replace(TEMPLATE_REF, (full, inner: string) => {
    const segments = inner.trim().split('.');
    const mapped = idMap.get(segments[0] ?? '');
    return mapped === undefined ? full : `{{#${[mapped, ...segments.slice(1)].join('.')}#}}`;
  });
}

/** Deep-copy `value`, rewriting remapped node ids inside string leaves. */
function rewriteDeep(value: unknown, idMap: ReadonlyMap<string, string>): unknown {
  if (typeof value === 'string') return rewriteRefs(value, idMap);
  if (Array.isArray(value)) return value.map((item) => rewriteDeep(item, idMap));
  if (isRecord(value)) {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) out[key] = rewriteDeep(item, idMap);
    return out;
  }
  return value;
}

/**
 * Collect a self-contained payload for `selectedIds`: children of selected
 * containers are pulled in, edges are kept only when both endpoints are in
 * the set. Payload keeps store order, so parents stay ahead of children.
 * Returns null when nothing matches.
 */
export function collectClipboardContent(
  nodes: readonly StudioFlowNode[],
  edges: readonly StudioFlowEdge[],
  selectedIds: readonly string[],
): ClipboardContent | null {
  const included = expandWithChildren(nodes, selectedIds);
  const payload = nodes.filter((n) => included.has(n.id));
  if (payload.length === 0) return null;
  const byId = new Map(nodes.map((n) => [n.id, n] as const));
  return {
    nodes: payload.map((n) => ({ node: cloneNode(n), absolute: absolutePosition(byId, n) })),
    edges: edges
      .filter((e) => included.has(e.source) && included.has(e.target))
      .map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        sourceHandle: e.sourceHandle,
        targetHandle: e.targetHandle,
        data: structuredClone(e.data) ?? { passthrough: {} },
      })),
  };
}

/**
 * Materialize `content` against the current canvas: fresh ids, remapped
 * parentId + iteration_id, rewritten internal references, `offset` applied
 * to top-level positions. Every clone comes back selected. A copied child
 * whose container was not copied stays in that container when it still
 * exists, otherwise it is detached at its absolute position.
 */
export function materializeClones(
  content: ClipboardContent,
  existingNodes: readonly StudioFlowNode[],
  existingEdges: readonly StudioFlowEdge[],
  offset: number,
): MaterializedClones {
  const presentIds = new Set(existingNodes.map((n) => n.id));
  const usedNodeIds = new Set(presentIds);
  const idMap = new Map<string, string>();
  for (const { node } of content.nodes) {
    const id = uniqueNodeId(node.data.dify.type, usedNodeIds);
    usedNodeIds.add(id);
    idMap.set(node.id, id);
  }

  const nodes = content.nodes.map(({ node, absolute }): StudioFlowNode => {
    const clone = cloneNode(node);
    clone.id = idMap.get(node.id)!;
    clone.selected = true;
    const parentId = node.parentId;
    const mappedParent = parentId !== undefined ? idMap.get(parentId) : undefined;
    if (mappedParent !== undefined) {
      clone.parentId = mappedParent; // container copied along: keep relative position
    } else if (parentId !== undefined && presentIds.has(parentId)) {
      clone.position = { x: node.position.x + offset, y: node.position.y + offset };
    } else {
      delete clone.parentId;
      delete clone.extent;
      clone.position = { x: absolute.x + offset, y: absolute.y + offset };
    }
    clone.data = {
      dify: withSyncedIterationId(
        rewriteDeep(node.data.dify, idMap) as StudioNodeData,
        clone.parentId,
      ),
      passthrough: structuredClone(node.data.passthrough),
    };
    return clone;
  });

  const usedEdgeIds = new Set(existingEdges.map((e) => e.id));
  const edges = content.edges.map((edge): StudioFlowEdge => {
    const source = idMap.get(edge.source)!;
    const target = idMap.get(edge.target)!;
    const sourceHandle = edge.sourceHandle ?? 'source';
    const id = uniqueEdgeId(source, sourceHandle, target, usedEdgeIds);
    usedEdgeIds.add(id);
    return {
      id,
      source,
      target,
      sourceHandle,
      targetHandle: edge.targetHandle ?? 'target',
      data: structuredClone(edge.data) ?? { passthrough: {} },
    };
  });

  return { nodes, edges };
}

let clipboard: ClipboardContent | null = null;
let pasteSerial = 0;

/** Copy the selection into the module clipboard. False when nothing matched. */
export function copyToClipboard(
  nodes: readonly StudioFlowNode[],
  edges: readonly StudioFlowEdge[],
  selectedIds: readonly string[],
): boolean {
  const content = collectClipboardContent(nodes, edges, selectedIds);
  if (content === null) return false;
  clipboard = content;
  pasteSerial = 0;
  return true;
}

/** Materialize the clipboard; the offset grows on each consecutive paste. */
export function pasteFromClipboard(
  existingNodes: readonly StudioFlowNode[],
  existingEdges: readonly StudioFlowEdge[],
): MaterializedClones | null {
  if (clipboard === null) return null;
  pasteSerial += 1;
  return materializeClones(clipboard, existingNodes, existingEdges, PASTE_OFFSET * pasteSerial);
}

/** Reset module clipboard state (tests). */
export function clearClipboard(): void {
  clipboard = null;
  pasteSerial = 0;
}
