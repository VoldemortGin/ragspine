/** React Flow canvas: rendering, connection rules, DnD from the palette,
 * cascade-delete for containers, minimap/controls/background chrome. */

import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  useReactFlow,
} from '@xyflow/react';
import type {
  IsValidConnection,
  OnBeforeDelete,
  OnConnectEnd,
  OnSelectionChangeFunc,
} from '@xyflow/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { DragEvent, MouseEvent as ReactMouseEvent } from 'react';

import { Button, EmptyState, IconUpload, IconWorkflow, KeyHint } from '../../components';
import { DIFY_ITERATION } from '../../workflow/reactflow';
import type { StudioFlowEdge, StudioFlowNode } from '../../workflow/reactflow';
import { getNodeDefinition } from '../../workflow/registry';
import type { NodeTypeDefinition, XY } from '../../workflow/types';
import { NodePicker } from './NodePicker';
import { EDITOR_NODE_TYPES } from './nodes';
import { NODE_TYPE_MIME, isEditableTarget } from './shared';
import { useEditorStore } from './store';
import type { ConnectionOrigin } from './store';

const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);

/** Panel footprint used to clamp the quick-add picker inside the canvas. */
const PICKER_W = 256; // w-64
const PICKER_H = 340; // search row + max-h-72 list

/** Quick-add picker state: where the panel sits, where the node will land,
 * and (for edge drops) which handle the connection was dragged from. */
interface PickerState {
  panel: XY;
  flow: XY;
  from: ConnectionOrigin | null;
}

/** True when the element is the pane or sits inside an iteration container
 * (so an edge dropped inside a container still quick-adds into it). */
function isQuickAddSurface(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  if (target.classList.contains('react-flow__pane')) return true;
  const nodeEl = target.closest<HTMLElement>('.react-flow__node');
  if (nodeEl === null) return false;
  const id = nodeEl.dataset['id'];
  return useEditorStore.getState().nodes.some((n) => n.id === id && n.type === DIFY_ITERATION);
}

function edgeLabel(edge: StudioFlowEdge, nodesById: Map<string, StudioFlowNode>): string | undefined {
  const handle = edge.sourceHandle;
  if (typeof handle !== 'string' || handle === '' || handle === 'source') return undefined;
  const source = nodesById.get(edge.source);
  if (source !== undefined) {
    const spec = getNodeDefinition(source.data.dify.type)
      .getSourceHandles(source.data.dify)
      .find((h) => h.id === handle);
    if (spec !== undefined && spec.label !== '') return spec.label;
  }
  return handle;
}

export interface CanvasProps {
  /** False while the page is hidden — disables delete/duplicate hotkeys. */
  hotkeysEnabled: boolean;
  onImportClick: () => void;
}

export function Canvas({ hotkeysEnabled, onImportClick }: CanvasProps) {
  const nodes = useEditorStore((s) => s.nodes);
  const edges = useEditorStore((s) => s.edges);
  const execution = useEditorStore((s) => s.execution);
  const revision = useEditorStore((s) => s.revision);
  const onNodesChange = useEditorStore((s) => s.onNodesChange);
  const onEdgesChange = useEditorStore((s) => s.onEdgesChange);
  const connect = useEditorStore((s) => s.connect);
  const setSelection = useEditorStore((s) => s.setSelection);
  const setMultiSelection = useEditorStore((s) => s.setMultiSelection);
  const addNodeAtPosition = useEditorStore((s) => s.addNodeAtPosition);
  const addNodeWithConnection = useEditorStore((s) => s.addNodeWithConnection);

  const { fitView, screenToFlowPosition } = useReactFlow<StudioFlowNode, StudioFlowEdge>();

  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [picker, setPicker] = useState<PickerState | null>(null);

  /** Open the quick-add picker at a screen point (panel clamped to canvas). */
  const openPicker = useCallback(
    (clientX: number, clientY: number, from: ConnectionOrigin | null) => {
      const rect = wrapRef.current?.getBoundingClientRect();
      if (rect === undefined) return;
      setPicker({
        panel: {
          x: Math.max(Math.min(clientX - rect.left, rect.width - PICKER_W - 8), 8),
          y: Math.max(Math.min(clientY - rect.top, rect.height - PICKER_H - 8), 8),
        },
        flow: screenToFlowPosition({ x: clientX, y: clientY }),
        from,
      });
    },
    [screenToFlowPosition],
  );

  // Dropping a connection on empty canvas (or inside an iteration container)
  // opens the picker; the chosen node is wired to the dragged handle.
  const onConnectEnd: OnConnectEnd = useCallback(
    (event, connectionState) => {
      // Ended on/near a valid handle: React Flow completes it via onConnect.
      if (connectionState.isValid === true) return;
      const { fromNode, fromHandle } = connectionState;
      if (fromNode === null || fromHandle === null) return;
      if (!(event instanceof MouseEvent) || !isQuickAddSurface(event.target)) return;
      openPicker(event.clientX, event.clientY, {
        nodeId: fromNode.id,
        handleType: fromHandle.type,
        handleId: fromHandle.id ?? null,
      });
    },
    [openPicker],
  );

  // Double-clicking empty canvas opens the picker (plain add, no wiring);
  // zoomOnDoubleClick is disabled below so the gestures don't conflict.
  const onDoubleClick = useCallback(
    (event: ReactMouseEvent<HTMLDivElement>) => {
      if (!(event.target instanceof Element)) return;
      if (!event.target.classList.contains('react-flow__pane')) return;
      openPicker(event.clientX, event.clientY, null);
    },
    [openPicker],
  );

  // Tab opens the picker at the viewport center (skipped while typing or
  // while another page is shown — the app keeps hidden pages mounted).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Tab' || !hotkeysEnabled || isEditableTarget(e.target)) return;
      const rect = wrapRef.current?.getBoundingClientRect();
      if (rect === undefined) return;
      e.preventDefault();
      openPicker(rect.left + rect.width / 2, rect.top + rect.height / 2, null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [hotkeysEnabled, openPicker]);

  // Edge drops constrain the offered types to ones the new edge can attach to.
  const pickerInclude = useMemo(() => {
    const from = picker?.from ?? null;
    if (from === null) return undefined;
    if (from.handleType === 'source') {
      // The new node receives the edge: it needs a target handle (not start).
      return (def: NodeTypeDefinition) => def.hasTargetHandle;
    }
    // The new node emits the edge: it needs a source handle (not end).
    return (def: NodeTypeDefinition) => def.getSourceHandles(def.createDefaultData()).length > 0;
  }, [picker]);

  const onPick = useCallback(
    (type: string) => {
      if (picker === null) return;
      if (picker.from !== null) addNodeWithConnection(type, picker.flow, picker.from);
      else addNodeAtPosition(type, picker.flow);
      setPicker(null);
    },
    [picker, addNodeAtPosition, addNodeWithConnection],
  );

  const closePicker = useCallback(() => setPicker(null), []);

  // Re-fit whenever a whole document is (re)loaded.
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      void fitView({ padding: 0.15, duration: 250, maxZoom: 1.25 });
    });
    return () => cancelAnimationFrame(frame);
  }, [revision, fitView]);

  // Branch edges get their handle label + all edges get an arrow marker.
  // After a finished run, edges whose BOTH endpoints were recorded are shown
  // as a dashed contextual hint and the rest are dimmed. This is deliberately
  // not styled as an exact traversed path: the backend does not emit edge or
  // source-handle events yet.
  const displayEdges = useMemo(() => {
    const nodesById = new Map(nodes.map((n) => [n.id, n] as const));
    const finished = execution.status === 'succeeded' || execution.status === 'failed';
    return edges.map((edge): StudioFlowEdge => {
      const selected = edge.selected === true;
      const label = edgeLabel(edge, nodesById);
      let stroke = selected ? '#818cf8' : '#52525b';
      let marker = selected ? '#818cf8' : '#71717a';
      let strokeWidth = 1.5;
      let strokeDasharray: string | undefined;
      if (finished && !selected) {
        const src = execution.traces[edge.source];
        const dst = execution.traces[edge.target];
        const walked =
          src !== undefined &&
          dst !== undefined &&
          src.status !== 'skipped' &&
          dst.status !== 'skipped';
        stroke = walked ? '#818cf8' : '#3f3f46';
        marker = stroke;
        if (walked) {
          strokeWidth = 1.75;
          strokeDasharray = '5 4';
        }
      }
      return {
        ...edge,
        ...(label !== undefined ? { label } : {}),
        markerEnd: {
          type: MarkerType.ArrowClosed,
          width: 16,
          height: 16,
          color: marker,
        },
        style: { stroke, strokeWidth, ...(strokeDasharray !== undefined ? { strokeDasharray } : {}) },
      };
    });
  }, [nodes, edges, execution]);

  const isValidConnection: IsValidConnection<StudioFlowEdge> = useCallback((conn) => {
    const source = conn.source;
    const target = conn.target;
    if (typeof source !== 'string' || typeof target !== 'string' || source === target) return false;
    const all = useEditorStore.getState().nodes;
    const sourceNode = all.find((n) => n.id === source);
    const targetNode = all.find((n) => n.id === target);
    if (sourceNode === undefined || targetNode === undefined) return false;
    if (!getNodeDefinition(targetNode.data.dify.type).hasTargetHandle) return false;
    const handle = typeof conn.sourceHandle === 'string' && conn.sourceHandle !== '' ? conn.sourceHandle : 'source';
    return getNodeDefinition(sourceNode.data.dify.type)
      .getSourceHandles(sourceNode.data.dify)
      .some((h) => h.id === handle);
  }, []);

  // Deleting an iteration container also deletes its children (and their edges).
  const onBeforeDelete: OnBeforeDelete<StudioFlowNode, StudioFlowEdge> = useCallback(
    ({ nodes: doomedNodes, edges: doomedEdges }) => {
      const state = useEditorStore.getState();
      const doomed = new Set(doomedNodes.map((n) => n.id));
      let grew = true;
      while (grew) {
        grew = false;
        for (const n of state.nodes) {
          if (n.parentId !== undefined && doomed.has(n.parentId) && !doomed.has(n.id)) {
            doomed.add(n.id);
            grew = true;
          }
        }
      }
      const doomedEdgeIds = new Set(doomedEdges.map((e) => e.id));
      const nodes = state.nodes.filter((n) => doomed.has(n.id));
      const edges = state.edges.filter(
        (e) => doomedEdgeIds.has(e.id) || doomed.has(e.source) || doomed.has(e.target),
      );
      // React Flow applies the deletion as remove changes, bypassing
      // store.deleteNodes — record the undo step here, where each delete
      // gesture triggers exactly once.
      if (nodes.length > 0 || edges.length > 0) state.recordSnapshot();
      return Promise.resolve({ nodes, edges });
    },
    [],
  );

  const onSelectionChange: OnSelectionChangeFunc<StudioFlowNode, StudioFlowEdge> = useCallback(
    ({ nodes: selectedNodes, edges: selectedEdges }) => {
      setMultiSelection(selectedNodes.map((n) => n.id));
      const node = selectedNodes[selectedNodes.length - 1];
      const edge = selectedEdges[0];
      setSelection(
        node !== undefined
          ? { kind: 'node', id: node.id }
          : edge !== undefined
            ? { kind: 'edge', id: edge.id }
            : null,
      );
    },
    [setSelection, setMultiSelection],
  );

  const onDragOver = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (event.dataTransfer.types.includes(NODE_TYPE_MIME)) {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
    }
  }, []);

  const onDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      const type = event.dataTransfer.getData(NODE_TYPE_MIME);
      if (type === '') return;
      event.preventDefault();
      addNodeAtPosition(type, screenToFlowPosition({ x: event.clientX, y: event.clientY }));
    },
    [addNodeAtPosition, screenToFlowPosition],
  );

  return (
    <div ref={wrapRef} className="relative h-full w-full" onDoubleClick={onDoubleClick}>
      <ReactFlow<StudioFlowNode, StudioFlowEdge>
        className="dark"
        nodes={nodes}
        edges={displayEdges}
        nodeTypes={EDITOR_NODE_TYPES}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={connect}
        onConnectEnd={onConnectEnd}
        isValidConnection={isValidConnection}
        onBeforeDelete={onBeforeDelete}
        onSelectionChange={onSelectionChange}
        onDragOver={onDragOver}
        onDrop={onDrop}
        deleteKeyCode={hotkeysEnabled ? ['Backspace', 'Delete'] : null}
        fitView
        minZoom={0.15}
        maxZoom={2}
        connectionRadius={28}
        snapToGrid
        snapGrid={[5, 5]}
        zoomOnDoubleClick={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#3f3f46" />
        <Controls position="bottom-left" showInteractive={false} />
        <MiniMap<StudioFlowNode>
          pannable
          zoomable
          position="bottom-right"
          nodeColor={(n) => getNodeDefinition(n.data.dify.type).accent}
          nodeStrokeWidth={2}
        />
      </ReactFlow>

      {picker !== null && (
        <NodePicker
          position={picker.panel}
          {...(pickerInclude !== undefined ? { include: pickerInclude } : {})}
          onPick={onPick}
          onClose={closePicker}
        />
      )}

      {nodes.length === 0 && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <div className="pointer-events-auto">
            <EmptyState
              icon={<IconWorkflow size={18} />}
              title="Empty canvas"
              hint="Drag a node from the palette, or import a YAML file."
              action={
                <div className="flex flex-col items-center gap-3">
                  <Button size="sm" onClick={onImportClick}>
                    <IconUpload size={13} />
                    Import YAML
                  </Button>
                  <div className="flex items-center gap-3 text-[11px] text-zinc-600">
                    <span className="flex items-center gap-1.5">
                      <KeyHint keys={['Del']} /> delete
                    </span>
                    <span className="flex items-center gap-1.5">
                      <KeyHint keys={IS_MAC ? ['⌘', 'D'] : ['Ctrl', 'D']} /> duplicate
                    </span>
                  </div>
                </div>
              }
            />
          </div>
        </div>
      )}
    </div>
  );
}
