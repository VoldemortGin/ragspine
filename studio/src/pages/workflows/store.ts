/**
 * Zustand store for the workflow editor.
 *
 * Canvas state lives as React Flow nodes/edges (StudioFlowNode/Edge); the
 * document-level meta + passthrough bags live in `base` (StudioWorkflow) so
 * fromFlow(nodes, edges, base) always reproduces a lossless document.
 * The workflow library persists serialized YAML in localStorage, debounced
 * ~800ms after any graph/data change.
 */

import { applyEdgeChanges, applyNodeChanges } from '@xyflow/react';
import type { Connection, EdgeChange, NodeChange } from '@xyflow/react';
import { create } from 'zustand';

import { getJob, runWorkflow, runWorkflowAsync } from '../../api/client';
import type { NodeTrace, SuggestionSeverity } from '../../api/types';
import { parseWorkflowYaml, serializeWorkflowYaml } from '../../workflow/convert';
import {
  DEFAULT_CONTAINER_HEIGHT,
  DEFAULT_CONTAINER_WIDTH,
  autoLayoutWorkflow,
} from '../../workflow/layout';
import { getNodeDefinition } from '../../workflow/registry';
import { DIFY_ITERATION, DIFY_NODE, fromFlow, toFlow } from '../../workflow/reactflow';
import type { StudioFlowEdge, StudioFlowNode } from '../../workflow/reactflow';
import type { StudioNodeData, StudioWorkflow, XY } from '../../workflow/types';
import type { AnalysisSlice } from './model/analysis';
import {
  PASTE_OFFSET,
  collectClipboardContent,
  copyToClipboard,
  materializeClones,
  pasteFromClipboard,
} from './model/clipboard';
import type { MaterializedClones } from './model/clipboard';
import {
  appendRunHistory,
  deleteRunHistory,
  executionFromHistory,
  executionFromRunHistory,
  IDLE_EXECUTION,
  nodeTracesFromError,
  parseJobRunResult,
  tracesToMap,
  workflowFingerprint,
} from './model/execution';
import type {
  ExecutionState,
  RunFailure,
  RunHistoryEntry,
  RunSlice,
} from './model/execution';
import { uniqueEdgeId, uniqueNodeId } from './model/ids';
import {
  deleteRunInputs,
  loadActiveId,
  loadFold,
  loadLibrary,
  newEntryId,
  saveActiveId,
  saveFold,
  saveLibrary,
} from './model/library';
import type { LibraryEntry } from './model/library';
import { copyName, createTemplateWorkflow, uniqueName, untitledName } from './model/template';

const SAVE_DELAY_MS = 800;
const NODE_W = 240;
const NODE_H = 72;
const HISTORY_LIMIT = 100;
const HISTORY_COALESCE_MS = 800;
const JOB_POLL_MS = 2000;

export interface EditorSelection {
  kind: 'node' | 'edge';
  id: string;
}

/** Origin handle of a connection drag that ended on empty canvas. */
export interface ConnectionOrigin {
  nodeId: string;
  /** Type of the handle the drag started from. */
  handleType: 'source' | 'target';
  handleId: string | null;
}

export interface HighlightState {
  nodeIds: readonly string[];
  severity: SuggestionSeverity;
}

/** One undo/redo step: the immutable canvas triple at a point in time. */
interface HistorySnapshot {
  nodes: StudioFlowNode[];
  edges: StudioFlowEdge[];
  base: StudioWorkflow;
}

interface EditorState {
  library: LibraryEntry[];
  activeId: string;
  nodes: StudioFlowNode[];
  edges: StudioFlowEdge[];
  base: StudioWorkflow;
  /** Bumped whenever a whole document is (re)loaded — canvas re-fits view. */
  revision: number;
  /** Bumped on every document mutation for run/graph compatibility checks. */
  documentVersion: number;
  saveState: 'saved' | 'dirty';
  selection: EditorSelection | null;
  /** Node ids in the current React Flow multi-selection (>1 => multi UI). */
  multiSelection: string[];
  panelOpen: boolean;
  paletteOpen: boolean;
  fold: boolean;
  analysis: AnalysisSlice | null;
  highlight: HighlightState | null;
  /** Undo/redo stacks (bounded); cleared when another document is opened. */
  past: HistorySnapshot[];
  future: HistorySnapshot[];
  /**
   * Node-level state of the current / most recent workflow run. Transient:
   * never enters the undo stacks and never persists into the workflow YAML
   * (the run history in localStorage is separate — see model/execution).
   */
  execution: ExecutionState;
  /** Lifecycle of the in-flight or last run attempt (drives the Run modal). */
  run: RunSlice;

  /* canvas */
  onNodesChange: (changes: NodeChange<StudioFlowNode>[]) => void;
  onEdgesChange: (changes: EdgeChange<StudioFlowEdge>[]) => void;
  connect: (conn: Connection) => void;
  addNodeAtPosition: (type: string, flowPos: XY) => void;
  /** Quick-add (edge drop on empty canvas): create a node at flowPos AND wire
   * it to the drag origin as ONE undo step. From a source handle the new node
   * is the edge target; from a target handle it is the edge source. */
  addNodeWithConnection: (type: string, flowPos: XY, from: ConnectionOrigin) => void;
  updateNodeData: (id: string, next: StudioNodeData) => void;
  updateContainerLayout: (id: string, rect: { x: number; y: number; width: number; height: number }) => void;
  deleteNodes: (ids: readonly string[]) => void;
  deleteEdge: (id: string) => void;
  duplicateNode: (id: string) => void;
  duplicateNodes: (ids: readonly string[]) => void;
  setSelection: (sel: EditorSelection | null) => void;
  /** Programmatic focus that intentionally collapses any pointer multi-select. */
  selectSingleNode: (id: string) => void;
  setMultiSelection: (ids: readonly string[]) => void;
  selectAll: () => void;

  /* clipboard */
  copySelection: () => void;
  cutSelection: () => void;
  pasteClipboard: () => void;

  /* history */
  undo: () => void;
  redo: () => void;
  /** Record an undo step now (React Flow-initiated deletes call this). */
  recordSnapshot: () => void;

  /* execution */
  /** Kick off a run; the fetch lifecycle lives here so closing the Run modal
   * does not cancel it. Both outcomes update `execution` + the run history. */
  startRun: (mode: 'sync' | 'async', inputs: Record<string, unknown>) => void;
  /** Return the run slice to idle (canvas badges from `execution` persist). */
  resetRun: () => void;
  /** Project a stored run (or one replay frame) onto the canvas. */
  previewRunHistory: (entry: RunHistoryEntry, throughStep?: number | null) => void;
  /** Restore the active workflow's latest completed run on the canvas. */
  restoreLatestExecution: () => void;

  /* panels + toggles */
  setPanelOpen: (open: boolean) => void;
  setPaletteOpen: (open: boolean) => void;
  setFold: (fold: boolean) => void;
  setAnalysis: (slice: AnalysisSlice | null) => void;
  setHighlight: (highlight: HighlightState | null) => void;

  /* document */
  autoLayout: () => void;
  importYaml: (text: string) => void;
  setAppMeta: (meta: { name: string; mode: StudioWorkflow['mode'] }) => void;
  getYaml: () => string;
  flushSave: () => void;

  /* library */
  createWorkflow: () => void;
  /** New workflow from a template document (falls back to blank on bad yaml). */
  createFromTemplate: (template: { name: string; yaml: string }) => void;
  switchWorkflow: (id: string) => void;
  renameWorkflow: (id: string, name: string) => void;
  duplicateWorkflow: (id: string) => void;
  deleteWorkflow: (id: string) => void;
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function parseEntry(entry: LibraryEntry): StudioWorkflow {
  try {
    return parseWorkflowYaml(entry.yaml);
  } catch {
    // Corrupt stored yaml: fall back to a fresh template rather than crashing.
    return createTemplateWorkflow(entry.name);
  }
}

interface DocumentSlice {
  library: LibraryEntry[];
  activeId: string;
  nodes: StudioFlowNode[];
  edges: StudioFlowEdge[];
  base: StudioWorkflow;
}

function initialDocument(): DocumentSlice {
  let library = loadLibrary();
  let activeId = loadActiveId();
  if (library.length === 0) {
    const name = untitledName([]);
    const wf = createTemplateWorkflow(name);
    const entry: LibraryEntry = {
      id: newEntryId(),
      name,
      updatedAt: new Date().toISOString(),
      yaml: serializeWorkflowYaml(wf),
    };
    library = [entry];
    activeId = entry.id;
    saveLibrary(library);
    saveActiveId(activeId);
    const { nodes, edges } = toFlow(wf);
    return { library, activeId, nodes, edges, base: wf };
  }
  const entry = library.find((e) => e.id === activeId) ?? library[0]!;
  const wf = parseEntry(entry);
  const { nodes, edges } = toFlow(wf);
  return { library, activeId: entry.id, nodes, edges, base: wf };
}

/** Rewrite app.name inside a stored yaml document; on parse failure keep it. */
function yamlWithName(yaml: string, name: string): string {
  try {
    const wf = parseWorkflowYaml(yaml);
    return serializeWorkflowYaml({ ...wf, name });
  } catch {
    return yaml;
  }
}

function containsPoint(node: StudioFlowNode, p: XY): boolean {
  const w = numberOr(node.style?.['width'], DEFAULT_CONTAINER_WIDTH);
  const h = numberOr(node.style?.['height'], DEFAULT_CONTAINER_HEIGHT);
  return (
    p.x >= node.position.x &&
    p.x <= node.position.x + w &&
    p.y >= node.position.y &&
    p.y <= node.position.y + h
  );
}

/** Build a fresh node of `type` centered on flowPos; a non-container point
 * inside an iteration container becomes that container's child. */
function buildNodeAt(type: string, flowPos: XY, nodes: readonly StudioFlowNode[]): StudioFlowNode {
  const def = getNodeDefinition(type);
  const id = uniqueNodeId(type, nodes.map((n) => n.id));
  const data: StudioNodeData = { ...def.createDefaultData() };
  if (typeof data.title !== 'string' || data.title === '') data.title = def.label;

  const rf: StudioFlowNode = {
    id,
    type: def.isContainer ? DIFY_ITERATION : DIFY_NODE,
    position: { x: Math.round(flowPos.x - NODE_W / 2), y: Math.round(flowPos.y - NODE_H / 2) },
    data: { dify: data, passthrough: {} },
    selected: true,
  };

  if (def.isContainer) {
    rf.style = { width: DEFAULT_CONTAINER_WIDTH, height: DEFAULT_CONTAINER_HEIGHT };
  } else {
    // Dropped inside an iteration container -> becomes its child.
    const parent = [...nodes]
      .reverse()
      .find((n) => n.type === DIFY_ITERATION && n.parentId === undefined && containsPoint(n, flowPos));
    if (parent !== undefined) {
      const pw = numberOr(parent.style?.['width'], DEFAULT_CONTAINER_WIDTH);
      const ph = numberOr(parent.style?.['height'], DEFAULT_CONTAINER_HEIGHT);
      rf.parentId = parent.id;
      rf.extent = 'parent';
      rf.position = {
        x: Math.round(Math.min(Math.max(flowPos.x - parent.position.x - NODE_W / 2, 8), Math.max(pw - NODE_W - 8, 8))),
        y: Math.round(Math.min(Math.max(flowPos.y - parent.position.y - NODE_H / 2, 44), Math.max(ph - NODE_H - 8, 44))),
      };
    }
  }
  return rf;
}

/** First shallow key whose value differs between two dify data bags. */
function changedField(prev: StudioNodeData, next: StudioNodeData): string {
  for (const key of new Set([...Object.keys(prev), ...Object.keys(next)])) {
    if (!Object.is(prev[key], next[key])) return key;
  }
  return '';
}

/** Keep a selection only if its node/edge still exists in the snapshot. */
function pruneSelection(sel: EditorSelection | null, snap: HistorySnapshot): EditorSelection | null {
  if (sel === null) return null;
  const exists =
    sel.kind === 'node'
      ? snap.nodes.some((n) => n.id === sel.id)
      : snap.edges.some((e) => e.id === sel.id);
  return exists ? sel : null;
}

/** Keep only multi-selection ids whose nodes still exist in the snapshot. */
function pruneMultiSelection(ids: readonly string[], snap: HistorySnapshot): string[] {
  const existing = new Set(snap.nodes.map((n) => n.id));
  return ids.filter((id) => existing.has(id));
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;
/** Coalescing anchor for updateNodeData snapshots (same node + field). */
let lastSnapshotMeta: { nodeId: string; field: string; time: number } | null = null;
/** True between the first and last position change of a node drag. */
let dragInProgress = false;
/** Bumped per run start / document load: stale run callbacks are ignored. */
let runToken = 0;
let jobPollTimer: ReturnType<typeof setInterval> | null = null;

/** Outcome payload passed from the fetch callbacks to finishRun. */
type RunOutcome =
  | {
      status: 'succeeded';
      result: unknown;
      warnings: string[];
      requestId: string | null;
      traces: NodeTrace[] | null;
    }
  | { status: 'failed'; failure: RunFailure; traces: NodeTrace[] | null };

function failureMessage(failure: RunFailure): string {
  if (failure.kind === 'job') return failure.message;
  return failure.error instanceof Error ? failure.error.message : String(failure.error);
}

export const useEditorStore = create<EditorState>()((set, get) => {
  const cancelPendingSave = () => {
    if (saveTimer !== null) {
      clearTimeout(saveTimer);
      saveTimer = null;
    }
  };

  const scheduleSave = () => {
    cancelPendingSave();
    saveTimer = setTimeout(() => {
      saveTimer = null;
      get().flushSave();
    }, SAVE_DELAY_MS);
  };

  /** Mark the document dirty and schedule the debounced auto-save. */
  const touch = () => {
    set((state) => ({
      saveState: 'dirty',
      documentVersion: state.documentVersion + 1,
      ...(state.execution.status === 'running' ? {} : { execution: IDLE_EXECUTION }),
    }));
    scheduleSave();
  };

  const pushSnapshot = () => {
    const { nodes, edges, base, past } = get();
    const next = [...past, { nodes, edges, base }];
    if (next.length > HISTORY_LIMIT) next.shift();
    set({ past: next, future: [] });
  };

  /** Record the current state onto the undo stack before a structural change. */
  const snapshot = () => {
    lastSnapshotMeta = null;
    pushSnapshot();
  };

  /** Snapshot before a node-data edit, coalescing rapid edits to one field. */
  const snapshotForDataEdit = (nodeId: string, prev: StudioNodeData, next: StudioNodeData) => {
    const field = changedField(prev, next);
    const now = Date.now();
    const meta = lastSnapshotMeta;
    if (
      meta !== null &&
      meta.nodeId === nodeId &&
      meta.field === field &&
      now - meta.time <= HISTORY_COALESCE_MS
    ) {
      return;
    }
    pushSnapshot();
    lastSnapshotMeta = { nodeId, field, time: now };
  };

  /** Append materialized clones as the new (and only) selection. */
  const insertClones = (clones: MaterializedClones) => {
    set((state) => ({
      nodes: [
        ...state.nodes.map((n) => (n.selected === true ? { ...n, selected: false } : n)),
        ...clones.nodes,
      ],
      edges: [
        ...state.edges.map((e) => (e.selected === true ? { ...e, selected: false } : e)),
        ...clones.edges,
      ],
      selection: { kind: 'node', id: clones.nodes[clones.nodes.length - 1]!.id },
      multiSelection: clones.nodes.map((n) => n.id),
      panelOpen: true,
    }));
    touch();
  };

  const stopJobPolling = () => {
    if (jobPollTimer !== null) {
      clearInterval(jobPollTimer);
      jobPollTimer = null;
    }
  };

  /** Load a workflow document into the canvas, resetting transient UI state.
   * Execution state is rehydrated from the workflow's most recent stored run;
   * any in-flight run is detached (it still lands in that run's history). */
  const loadIntoCanvas = (wf: StudioWorkflow, markDirty: boolean, clearHistory: boolean) => {
    lastSnapshotMeta = null;
    runToken += 1;
    stopJobPolling();
    const { nodes, edges } = toFlow(wf);
    set((state) => ({
      nodes,
      edges,
      base: wf,
      revision: state.revision + 1,
      documentVersion: state.documentVersion + 1,
      selection: null,
      multiSelection: [],
      panelOpen: false,
      analysis: null,
      highlight: null,
      execution: executionFromHistory(
        state.activeId,
        workflowFingerprint(serializeWorkflowYaml(wf)),
      ),
      run: { name: 'idle' },
      saveState: markDirty ? state.saveState : 'saved',
      ...(clearHistory ? { past: [], future: [] } : {}),
    }));
    if (markDirty) touch();
  };

  /** Complete a run: record it in the history, then (unless the document was
   * switched or a newer run started) publish the outcome to the slices. */
  const finishRun = (
    token: number,
    workflowId: string,
    inputs: Record<string, unknown>,
    startedAt: string,
    fingerprint: string,
    outcome: RunOutcome,
  ) => {
    const finishedAt = new Date().toISOString();
    const error = outcome.status === 'failed' ? failureMessage(outcome.failure) : undefined;
    appendRunHistory(workflowId, {
      at: startedAt,
      status: outcome.status,
      inputs,
      result: outcome.status === 'succeeded' ? outcome.result : null,
      warnings: outcome.status === 'succeeded' ? outcome.warnings : [],
      node_traces: outcome.traces,
      workflowFingerprint: fingerprint,
      ...(error !== undefined ? { error } : {}),
    });
    if (token !== runToken) return;
    let canvasCompatible = false;
    try {
      canvasCompatible = workflowFingerprint(get().getYaml()) === fingerprint;
    } catch {
      // A trace must never be projected when the current document cannot be
      // serialized and compared with the workflow that actually ran.
    }
    set({
      run:
        outcome.status === 'succeeded'
          ? {
              name: 'result',
              result: outcome.result,
              warnings: outcome.warnings,
              requestId: outcome.requestId,
              traces: outcome.traces,
            }
          : { name: 'error', failure: outcome.failure, traces: outcome.traces },
      execution: canvasCompatible
        ? {
            status: outcome.status,
            traces: tracesToMap(outcome.traces),
            startedAt,
            finishedAt,
            ...(error !== undefined ? { error } : {}),
          }
        : IDLE_EXECUTION,
    });
  };

  const doc = initialDocument();

  return {
    ...doc,
    revision: 0,
    documentVersion: 0,
    saveState: 'saved',
    selection: null,
    multiSelection: [],
    panelOpen: false,
    paletteOpen: true,
    fold: loadFold(),
    analysis: null,
    highlight: null,
    past: [],
    future: [],
    execution: executionFromHistory(
      doc.activeId,
      workflowFingerprint(serializeWorkflowYaml(fromFlow(doc.nodes, doc.edges, doc.base))),
    ),
    run: { name: 'idle' },

    /* ------------------------------ canvas ------------------------------ */

    onNodesChange: (changes) => {
      // Snapshot once at the start of a node drag, not per position change.
      const dragging = changes.some((c) => c.type === 'position' && c.dragging === true);
      if (dragging && !dragInProgress) {
        dragInProgress = true;
        snapshot();
      } else if (!dragging && changes.some((c) => c.type === 'position')) {
        dragInProgress = false;
      }
      set((state) => ({ nodes: applyNodeChanges(changes, state.nodes) }));
      const dirtying = changes.some(
        (c) =>
          c.type === 'remove' ||
          c.type === 'add' ||
          c.type === 'replace' ||
          c.type === 'position' ||
          (c.type === 'dimensions' && c.resizing === true),
      );
      if (dirtying) touch();
    },

    onEdgesChange: (changes) => {
      set((state) => ({ edges: applyEdgeChanges(changes, state.edges) }));
      if (changes.some((c) => c.type !== 'select')) touch();
    },

    connect: (conn) => {
      snapshot();
      const { edges } = get();
      const sourceHandle = conn.sourceHandle ?? 'source';
      const edge: StudioFlowEdge = {
        id: uniqueEdgeId(conn.source, sourceHandle, conn.target, edges.map((e) => e.id)),
        source: conn.source,
        target: conn.target,
        sourceHandle,
        targetHandle: conn.targetHandle ?? 'target',
        data: { passthrough: {} },
      };
      set({ edges: [...edges, edge] });
      touch();
    },

    addNodeAtPosition: (type, flowPos) => {
      snapshot();
      const rf = buildNodeAt(type, flowPos, get().nodes);
      set((state) => ({
        nodes: [...state.nodes.map((n) => ({ ...n, selected: false })), rf],
        edges: state.edges.map((e) => (e.selected === true ? { ...e, selected: false } : e)),
        selection: { kind: 'node', id: rf.id },
        multiSelection: [rf.id],
        panelOpen: true,
      }));
      touch();
    },

    addNodeWithConnection: (type, flowPos, from) => {
      const { nodes, edges } = get();
      if (!nodes.some((n) => n.id === from.nodeId)) {
        get().addNodeAtPosition(type, flowPos);
        return;
      }
      snapshot();
      const rf = buildNodeAt(type, flowPos, nodes);
      const def = getNodeDefinition(type);
      const edgeIds = edges.map((e) => e.id);

      // The picker filters out incompatible types; if one slips through
      // (no target handle / no source handles), add the node without an edge.
      let edge: StudioFlowEdge | null = null;
      if (from.handleType === 'source') {
        // Dragged out of a source handle: the new node is the edge target.
        if (def.hasTargetHandle) {
          const sourceHandle = from.handleId ?? 'source';
          edge = {
            id: uniqueEdgeId(from.nodeId, sourceHandle, rf.id, edgeIds),
            source: from.nodeId,
            target: rf.id,
            sourceHandle,
            targetHandle: 'target',
            data: { passthrough: {} },
          };
        }
      } else {
        // Dragged out of a target handle: the new node is the edge source,
        // wired from its first source handle.
        const first = def.getSourceHandles(rf.data.dify)[0];
        if (first !== undefined) {
          edge = {
            id: uniqueEdgeId(rf.id, first.id, from.nodeId, edgeIds),
            source: rf.id,
            target: from.nodeId,
            sourceHandle: first.id,
            targetHandle: from.handleId ?? 'target',
            data: { passthrough: {} },
          };
        }
      }

      set((state) => ({
        nodes: [...state.nodes.map((n) => ({ ...n, selected: false })), rf],
        edges: [
          ...state.edges.map((e) => (e.selected === true ? { ...e, selected: false } : e)),
          ...(edge !== null ? [edge] : []),
        ],
        selection: { kind: 'node', id: rf.id },
        multiSelection: [rf.id],
        panelOpen: true,
      }));
      touch();
    },

    updateNodeData: (id, next) => {
      const prev = get().nodes.find((n) => n.id === id);
      if (prev !== undefined) snapshotForDataEdit(id, prev.data.dify, next);
      set((state) => ({
        nodes: state.nodes.map((n) => (n.id === id ? { ...n, data: { ...n.data, dify: next } } : n)),
      }));
      touch();
    },

    updateContainerLayout: (id, rect) => {
      snapshot();
      set((state) => ({
        nodes: state.nodes.map((n) =>
          n.id === id
            ? {
                ...n,
                position: { x: rect.x, y: rect.y },
                width: rect.width,
                height: rect.height,
                style: { ...n.style, width: rect.width, height: rect.height },
              }
            : n,
        ),
      }));
      touch();
    },

    deleteNodes: (ids) => {
      snapshot();
      const { nodes } = get();
      const doomed = new Set(ids);
      let grew = true;
      while (grew) {
        grew = false;
        for (const n of nodes) {
          if (n.parentId !== undefined && doomed.has(n.parentId) && !doomed.has(n.id)) {
            doomed.add(n.id);
            grew = true;
          }
        }
      }
      set((state) => ({
        nodes: state.nodes.filter((n) => !doomed.has(n.id)),
        edges: state.edges.filter((e) => !doomed.has(e.source) && !doomed.has(e.target)),
        selection:
          state.selection !== null && state.selection.kind === 'node' && doomed.has(state.selection.id)
            ? null
            : state.selection,
        multiSelection: state.multiSelection.filter((id) => !doomed.has(id)),
      }));
      touch();
    },

    deleteEdge: (id) => {
      snapshot();
      set((state) => ({
        edges: state.edges.filter((e) => e.id !== id),
        selection:
          state.selection !== null && state.selection.kind === 'edge' && state.selection.id === id
            ? null
            : state.selection,
      }));
      touch();
    },

    duplicateNode: (id) => {
      const { nodes } = get();
      const src = nodes.find((n) => n.id === id);
      if (src === undefined) return;
      snapshot();
      const newId = uniqueNodeId(src.data.dify.type, nodes.map((n) => n.id));
      const clone: StudioFlowNode = {
        id: newId,
        type: src.type,
        position: { x: src.position.x + 40, y: src.position.y + 40 },
        data: {
          dify: { ...structuredClone(src.data.dify), title: src.data.dify.title },
          passthrough: structuredClone(src.data.passthrough),
        },
        selected: true,
      };
      if (src.parentId !== undefined) {
        clone.parentId = src.parentId;
        clone.extent = src.extent;
      }
      if (src.style !== undefined) clone.style = { ...src.style };
      set((state) => ({
        nodes: [...state.nodes.map((n) => ({ ...n, selected: false })), clone],
        edges: state.edges.map((e) => (e.selected === true ? { ...e, selected: false } : e)),
        selection: { kind: 'node', id: newId },
        multiSelection: [newId],
        panelOpen: true,
      }));
      touch();
    },

    duplicateNodes: (ids) => {
      const { nodes, edges } = get();
      const content = collectClipboardContent(nodes, edges, ids);
      if (content === null) return;
      snapshot();
      insertClones(materializeClones(content, nodes, edges, PASTE_OFFSET));
    },

    setSelection: (sel) => {
      set((state) => {
        // React Flow has already applied node flags for pointer multi-select.
        // Programmatic selection (timeline/history) still needs to update
        // those flags so the focused card receives its visible selected ring.
        const preserveMulti =
          sel?.kind === 'node' &&
          state.multiSelection.length > 1 &&
          state.multiSelection.includes(sel.id);
        return {
          selection: sel,
          panelOpen: sel !== null && sel.kind === 'node' ? true : state.panelOpen,
          nodes: preserveMulti
            ? state.nodes
            : state.nodes.map((node) => {
                const selected = sel?.kind === 'node' && node.id === sel.id;
                return node.selected === selected ? node : { ...node, selected };
              }),
          edges: preserveMulti
            ? state.edges
            : state.edges.map((edge) => {
                const selected = sel?.kind === 'edge' && edge.id === sel.id;
                return edge.selected === selected ? edge : { ...edge, selected };
              }),
          multiSelection: preserveMulti
            ? state.multiSelection
            : sel?.kind === 'node'
              ? [sel.id]
              : [],
        };
      });
    },

    selectSingleNode: (id) => {
      set((state) => {
        if (!state.nodes.some((node) => node.id === id)) return state;
        return {
          nodes: state.nodes.map((node) => {
            const selected = node.id === id;
            return node.selected === selected ? node : { ...node, selected };
          }),
          edges: state.edges.map((edge) =>
            edge.selected === true ? { ...edge, selected: false } : edge,
          ),
          selection: { kind: 'node', id },
          multiSelection: [id],
          panelOpen: true,
        };
      });
    },

    setMultiSelection: (ids) => set({ multiSelection: [...ids] }),

    selectAll: () => {
      const { nodes } = get();
      const last = nodes[nodes.length - 1];
      if (last === undefined) return;
      set((state) => ({
        nodes: state.nodes.map((n) => (n.selected === true ? n : { ...n, selected: true })),
        edges: state.edges.map((e) => (e.selected === true ? { ...e, selected: false } : e)),
        selection: { kind: 'node', id: last.id },
        multiSelection: state.nodes.map((n) => n.id),
        panelOpen: true,
      }));
    },

    /* ----------------------------- clipboard ---------------------------- */

    copySelection: () => {
      const { nodes, edges } = get();
      const ids = nodes.filter((n) => n.selected === true).map((n) => n.id);
      copyToClipboard(nodes, edges, ids);
    },

    cutSelection: () => {
      const { nodes, edges } = get();
      const ids = nodes.filter((n) => n.selected === true).map((n) => n.id);
      if (!copyToClipboard(nodes, edges, ids)) return;
      get().deleteNodes(ids);
    },

    pasteClipboard: () => {
      const { nodes, edges } = get();
      const clones = pasteFromClipboard(nodes, edges);
      if (clones === null) return;
      snapshot();
      insertClones(clones);
    },

    /* ------------------------------ history ----------------------------- */

    undo: () => {
      const { past, future, nodes, edges, base, selection, multiSelection } = get();
      const prev = past[past.length - 1];
      if (prev === undefined) return;
      lastSnapshotMeta = null;
      set({
        past: past.slice(0, -1),
        future: [...future, { nodes, edges, base }],
        nodes: prev.nodes,
        edges: prev.edges,
        base: prev.base,
        selection: pruneSelection(selection, prev),
        multiSelection: pruneMultiSelection(multiSelection, prev),
      });
      touch();
    },

    redo: () => {
      const { past, future, nodes, edges, base, selection, multiSelection } = get();
      const next = future[future.length - 1];
      if (next === undefined) return;
      lastSnapshotMeta = null;
      set({
        past: [...past, { nodes, edges, base }],
        future: future.slice(0, -1),
        nodes: next.nodes,
        edges: next.edges,
        base: next.base,
        selection: pruneSelection(selection, next),
        multiSelection: pruneMultiSelection(multiSelection, next),
      });
      touch();
    },

    recordSnapshot: () => snapshot(),

    /* ----------------------------- execution ---------------------------- */

    startRun: (mode, inputs) => {
      const token = ++runToken;
      stopJobPolling();
      const workflowId = get().activeId;
      let yaml: string;
      try {
        yaml = get().getYaml();
      } catch (err) {
        // Serialization failed before anything ran: report it in the modal
        // without touching the canvas execution state.
        set({ run: { name: 'error', failure: { kind: 'api', error: err }, traces: null } });
        return;
      }
      const startedAt = new Date().toISOString();
      const fingerprint = workflowFingerprint(yaml);
      set({
        run: { name: 'running' },
        execution: { status: 'running', traces: {}, startedAt },
      });
      const fold = get().fold;

      const fail = (err: unknown) =>
        finishRun(token, workflowId, inputs, startedAt, fingerprint, {
          status: 'failed',
          failure: { kind: 'api', error: err },
          traces: nodeTracesFromError(err),
        });

      if (mode === 'sync') {
        runWorkflow(yaml, inputs, fold)
          .then((res) =>
            finishRun(token, workflowId, inputs, startedAt, fingerprint, {
              status: 'succeeded',
              result: res.result,
              warnings: res.warnings,
              requestId: res.request_id,
              traces: res.node_traces ?? null,
            }),
          )
          .catch(fail);
        return;
      }

      runWorkflowAsync(yaml, inputs, fold)
        .then((job) => {
          if (token !== runToken) return;
          set({ run: { name: 'job', jobId: job.job_id, jobState: 'queued' } });
          jobPollTimer = setInterval(() => {
            getJob(job.job_id)
              .then((status) => {
                // Stale token: this run was detached and its own timer already
                // cleared — never touch the (possibly newer) active timer.
                if (token !== runToken) return;
                if (status.status === 'finished') {
                  stopJobPolling();
                  const parsed = parseJobRunResult(status.result);
                  finishRun(token, workflowId, inputs, startedAt, fingerprint, {
                    status: 'succeeded',
                    result: parsed.result,
                    warnings: parsed.warnings,
                    requestId: null,
                    traces: parsed.node_traces,
                  });
                } else if (status.status === 'failed') {
                  stopJobPolling();
                  finishRun(token, workflowId, inputs, startedAt, fingerprint, {
                    status: 'failed',
                    failure: {
                      kind: 'job',
                      message: status.error ?? 'The job failed without an error message.',
                    },
                    traces: null,
                  });
                } else {
                  set({ run: { name: 'job', jobId: job.job_id, jobState: status.status } });
                }
              })
              .catch((err: unknown) => {
                if (token !== runToken) return;
                stopJobPolling();
                fail(err);
              });
          }, JOB_POLL_MS);
        })
        .catch(fail);
    },

    resetRun: () => set({ run: { name: 'idle' } }),

    previewRunHistory: (entry, throughStep = null) => {
      // A historical replay must never replace the in-flight pulse state.
      if (get().execution.status === 'running') return;
      let currentFingerprint: string;
      try {
        currentFingerprint = workflowFingerprint(get().getYaml());
      } catch {
        set({ execution: IDLE_EXECUTION });
        return;
      }
      if (entry.workflowFingerprint !== currentFingerprint) {
        set({ execution: IDLE_EXECUTION });
        return;
      }
      set({ execution: executionFromRunHistory(entry, throughStep) });
    },

    restoreLatestExecution: () => {
      if (get().execution.status === 'running') return;
      let currentFingerprint: string;
      try {
        currentFingerprint = workflowFingerprint(get().getYaml());
      } catch {
        set({ execution: IDLE_EXECUTION });
        return;
      }
      set({ execution: executionFromHistory(get().activeId, currentFingerprint) });
    },

    /* ------------------------- panels + toggles ------------------------- */

    setPanelOpen: (open) => set({ panelOpen: open }),
    setPaletteOpen: (open) => set({ paletteOpen: open }),
    setFold: (fold) => {
      saveFold(fold);
      set({ fold });
    },
    setAnalysis: (analysis) => set(analysis === null ? { analysis, highlight: null } : { analysis }),
    setHighlight: (highlight) => set({ highlight }),

    /* ------------------------------ document ---------------------------- */

    autoLayout: () => {
      snapshot();
      const { nodes, edges, base } = get();
      const laidOut = autoLayoutWorkflow(fromFlow(nodes, edges, base), { force: true });
      const flow = toFlow(laidOut);
      const selectedIds = new Set(nodes.filter((n) => n.selected === true).map((n) => n.id));
      set((state) => ({
        nodes: flow.nodes.map((n) => (selectedIds.has(n.id) ? { ...n, selected: true } : n)),
        edges: flow.edges,
        revision: state.revision + 1,
      }));
      touch();
    },

    importYaml: (text) => {
      const wf = parseWorkflowYaml(text); // throws WorkflowParseError for the caller
      snapshot();
      loadIntoCanvas(wf, true, false);
    },

    setAppMeta: (meta) => {
      set((state) => ({ base: { ...state.base, name: meta.name, mode: meta.mode } }));
      touch();
    },

    getYaml: () => {
      const { nodes, edges, base } = get();
      return serializeWorkflowYaml(fromFlow(nodes, edges, base));
    },

    flushSave: () => {
      cancelPendingSave();
      const { nodes, edges, base, library, activeId, saveState } = get();
      if (saveState === 'saved') return;
      const yaml = serializeWorkflowYaml(fromFlow(nodes, edges, base));
      const updated = library.map((e) =>
        e.id === activeId
          ? {
              ...e,
              name: base.name.trim() !== '' ? base.name : e.name,
              yaml,
              updatedAt: new Date().toISOString(),
            }
          : e,
      );
      saveLibrary(updated);
      set({ library: updated, saveState: 'saved' });
    },

    /* ------------------------------ library ----------------------------- */

    createWorkflow: () => {
      get().flushSave();
      const name = untitledName(get().library);
      const wf = createTemplateWorkflow(name);
      const entry: LibraryEntry = {
        id: newEntryId(),
        name,
        updatedAt: new Date().toISOString(),
        yaml: serializeWorkflowYaml(wf),
      };
      const library = [...get().library, entry];
      saveLibrary(library);
      saveActiveId(entry.id);
      set({ library, activeId: entry.id });
      loadIntoCanvas(wf, false, true);
    },

    createFromTemplate: (template) => {
      get().flushSave();
      const name = uniqueName(template.name, get().library);
      let wf: StudioWorkflow;
      try {
        wf = { ...parseWorkflowYaml(template.yaml), name };
      } catch {
        wf = createTemplateWorkflow(name);
      }
      const entry: LibraryEntry = {
        id: newEntryId(),
        name,
        updatedAt: new Date().toISOString(),
        yaml: serializeWorkflowYaml(wf),
      };
      const library = [...get().library, entry];
      saveLibrary(library);
      saveActiveId(entry.id);
      set({ library, activeId: entry.id });
      loadIntoCanvas(wf, false, true);
    },

    switchWorkflow: (id) => {
      if (id === get().activeId) return;
      get().flushSave();
      const entry = get().library.find((e) => e.id === id);
      if (entry === undefined) return;
      saveActiveId(id);
      set({ activeId: id });
      loadIntoCanvas(parseEntry(entry), false, true);
    },

    renameWorkflow: (id, rawName) => {
      const name = rawName.trim();
      if (name === '') return;
      const { library, activeId } = get();
      const updated = library.map((e) =>
        e.id === id
          ? {
              ...e,
              name,
              yaml: id === activeId ? e.yaml : yamlWithName(e.yaml, name),
              updatedAt: new Date().toISOString(),
            }
          : e,
      );
      saveLibrary(updated);
      set({ library: updated });
      if (id === activeId) {
        set((state) => ({ base: { ...state.base, name } }));
        touch();
      }
    },

    duplicateWorkflow: (id) => {
      get().flushSave();
      const { library } = get();
      const entry = library.find((e) => e.id === id);
      if (entry === undefined) return;
      const name = copyName(entry.name, library);
      const dup: LibraryEntry = {
        id: newEntryId(),
        name,
        updatedAt: new Date().toISOString(),
        yaml: yamlWithName(entry.yaml, name),
      };
      const updated = [...library, dup];
      saveLibrary(updated);
      saveActiveId(dup.id);
      set({ library: updated, activeId: dup.id });
      loadIntoCanvas(parseEntry(dup), false, true);
    },

    deleteWorkflow: (id) => {
      cancelPendingSave();
      const { library, activeId } = get();
      deleteRunInputs(id);
      deleteRunHistory(id);
      const remaining = library.filter((e) => e.id !== id);
      if (remaining.length === 0) {
        const name = untitledName([]);
        const wf = createTemplateWorkflow(name);
        const entry: LibraryEntry = {
          id: newEntryId(),
          name,
          updatedAt: new Date().toISOString(),
          yaml: serializeWorkflowYaml(wf),
        };
        saveLibrary([entry]);
        saveActiveId(entry.id);
        set({ library: [entry], activeId: entry.id });
        loadIntoCanvas(wf, false, true);
        return;
      }
      saveLibrary(remaining);
      if (id === activeId) {
        const next = remaining[0]!;
        saveActiveId(next.id);
        set({ library: remaining, activeId: next.id });
        loadIntoCanvas(parseEntry(next), false, true);
      } else {
        set({ library: remaining });
      }
    },
  };
});
