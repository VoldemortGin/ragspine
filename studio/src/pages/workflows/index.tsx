/** RAGSpine Studio — visual Dify workflow editor page. */

import { ReactFlowProvider, useReactFlow } from '@xyflow/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import type { Suggestion } from '../../api/types';
import type { StudioFlowEdge, StudioFlowNode } from '../../workflow/reactflow';
import type { StartNodeData, StartVariable } from '../../workflow/types';
import { Canvas } from './Canvas';
import { ExecutionInspector } from './ExecutionInspector';
import { Palette } from './Palette';
import { PropertyPanel } from './PropertyPanel';
import { Toolbar } from './Toolbar';
import {
  deleteRunHistory,
  loadRunHistory,
  orderedNodeTraces,
  workflowFingerprint,
} from './model/execution';
import type { RunHistoryEntry, RunSlice } from './model/execution';
import { CompileModal } from './modals/CompileModal';
import { ImportModal } from './modals/ImportModal';
import { RunModal } from './modals/RunModal';
import { SuggestionsDrawer } from './modals/SuggestionsDrawer';
import { describeApiError, isEditableTarget, useElementVisible } from './shared';
import { useEditorStore } from './store';

/** Start-node input variables of the current workflow (for the Run form). */
function currentStartVariables(nodes: readonly StudioFlowNode[]): StartVariable[] {
  const start = nodes.find((n) => n.data.dify.type === 'start');
  if (start === undefined) return [];
  const variables = (start.data.dify as StartNodeData).variables;
  if (!Array.isArray(variables)) return [];
  return variables.filter(
    (v): v is StartVariable =>
      typeof v === 'object' && v !== null && typeof v.variable === 'string' && v.variable !== '',
  );
}

function snapshotCurrentRun(
  run: RunSlice,
  persisted: RunHistoryEntry | null,
  fallbackFingerprint: string | null,
): RunHistoryEntry | null {
  if (run.name !== 'result' && run.name !== 'error') return null;
  if (run.name === 'result') {
    return {
      at: persisted?.at ?? new Date().toISOString(),
      status: 'succeeded',
      inputs: persisted?.inputs ?? {},
      result: run.result,
      warnings: run.warnings,
      node_traces: run.traces,
      ...(persisted?.workflowFingerprint !== undefined
        ? { workflowFingerprint: persisted.workflowFingerprint }
        : fallbackFingerprint !== null
          ? { workflowFingerprint: fallbackFingerprint }
          : {}),
    };
  }
  const error =
    run.failure.kind === 'job'
      ? run.failure.message
      : describeApiError(run.failure.error).message;
  return {
    at: persisted?.at ?? new Date().toISOString(),
    status: 'failed',
    inputs: persisted?.inputs ?? {},
    result: null,
    warnings: [],
    node_traces: run.traces,
    error,
    ...(persisted?.workflowFingerprint !== undefined
      ? { workflowFingerprint: persisted.workflowFingerprint }
      : fallbackFingerprint !== null
        ? { workflowFingerprint: fallbackFingerprint }
        : {}),
  };
}

function canProjectRun(
  entry: RunHistoryEntry | null,
  currentFingerprint: string | null,
): boolean {
  return (
    entry !== null &&
    currentFingerprint !== null &&
    entry.workflowFingerprint === currentFingerprint
  );
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window === 'undefined' ? false : window.matchMedia(query).matches,
  );
  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, [query]);
  return matches;
}

function EditorShell() {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const canvasWrapRef = useRef<HTMLDivElement | null>(null);
  const historyHeadBeforeRunRef = useRef<string | null>(null);
  const workflowFingerprintBeforeRunRef = useRef<string | null>(null);
  const visible = useElementVisible(rootRef);
  const compactInspector = useMediaQuery('(max-width: 1023px)');

  const [importOpen, setImportOpen] = useState(false);
  const [compileOpen, setCompileOpen] = useState(false);
  const [runOpen, setRunOpen] = useState(false);
  const [runMode, setRunMode] = useState<'sync' | 'async'>('sync');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [runHistory, setRunHistory] = useState<RunHistoryEntry[]>([]);
  const [selectedHistoryAt, setSelectedHistoryAt] = useState<string | null>(null);
  const [replayStep, setReplayStep] = useState<number | null>(null);
  const [replayPlaying, setReplayPlaying] = useState(false);
  const [currentRunSnapshot, setCurrentRunSnapshot] = useState<RunHistoryEntry | null>(null);

  const activeId = useEditorStore((s) => s.activeId);
  const documentVersion = useEditorStore((s) => s.documentVersion);
  const fold = useEditorStore((s) => s.fold);
  const setFold = useEditorStore((s) => s.setFold);
  const analysis = useEditorStore((s) => s.analysis);
  const execution = useEditorStore((s) => s.execution);
  const run = useEditorStore((s) => s.run);
  const selection = useEditorStore((s) => s.selection);
  const selectSingleNode = useEditorStore((s) => s.selectSingleNode);
  const previewRunHistory = useEditorStore((s) => s.previewRunHistory);
  const restoreLatestExecution = useEditorStore((s) => s.restoreLatestExecution);
  const setHighlight = useEditorStore((s) => s.setHighlight);
  const importYaml = useEditorStore((s) => s.importYaml);
  const getYaml = useEditorStore((s) => s.getYaml);
  const addNodeAtPosition = useEditorStore((s) => s.addNodeAtPosition);
  const [runVariables, setRunVariables] = useState<StartVariable[]>([]);

  const { fitView, screenToFlowPosition } = useReactFlow<StudioFlowNode, StudioFlowEdge>();

  const activeWorkflowFingerprint = useMemo(() => {
    try {
      return workflowFingerprint(getYaml());
    } catch {
      return null;
    }
  }, [activeId, documentVersion, getYaml]);

  const selectedHistory = useMemo(
    () =>
      selectedHistoryAt === null
        ? null
        : (runHistory.find((entry) => entry.at === selectedHistoryAt) ?? null),
    [runHistory, selectedHistoryAt],
  );
  const replayEntry = selectedHistoryAt === null ? currentRunSnapshot : selectedHistory;
  const replayTraces = useMemo(
    () => orderedNodeTraces(replayEntry?.node_traces),
    [replayEntry],
  );
  const canvasProjectionAvailable = canProjectRun(replayEntry, activeWorkflowFingerprint);
  const projectionWarning =
    replayEntry !== null && replayTraces.length > 0 && !canvasProjectionAvailable
      ? replayEntry.workflowFingerprint === undefined
        ? 'This run predates workflow version tracking. Its trace is viewable here, but it cannot be projected safely onto the current canvas.'
        : 'The workflow has changed since this run. Its trace is viewable here, but canvas highlighting and node focusing are disabled.'
      : null;
  const selectedNodeId = selection?.kind === 'node' ? selection.id : null;

  // Refresh the per-workflow browser history whenever the active document changes.
  useEffect(() => {
    const next = loadRunHistory(activeId);
    setRunHistory(next);
    setSelectedHistoryAt(next[0]?.at ?? null);
    setReplayStep(null);
    setReplayPlaying(false);
    setCurrentRunSnapshot(null);
    historyHeadBeforeRunRef.current = next[0]?.at ?? null;
    workflowFingerprintBeforeRunRef.current = null;
  }, [activeId]);

  // A finished run leaves the input modal and opens the persistent inspector,
  // keeping the workflow graph visible beside its recorded execution.
  useEffect(() => {
    if (run.name === 'running' || run.name === 'job') {
      setCurrentRunSnapshot(null);
      setSelectedHistoryAt(null);
      setReplayStep(null);
      setReplayPlaying(false);
      return;
    }
    if (run.name !== 'result' && run.name !== 'error') return;
    const next = loadRunHistory(activeId);
    setRunHistory(next);
    const recorded =
      next[0] !== undefined && next[0].at !== historyHeadBeforeRunRef.current ? next[0] : null;
    const snapshot = snapshotCurrentRun(run, recorded, workflowFingerprintBeforeRunRef.current);
    setCurrentRunSnapshot(snapshot);
    setSelectedHistoryAt(null);
    setReplayStep(null);
    setReplayPlaying(false);
    setRunOpen(false);
    setInspectorOpen(true);
  }, [activeId, run]);

  // Duplicate the selected node with Cmd/Ctrl+D (ignored while typing or
  // while another page is shown — the app keeps hidden pages mounted).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || (e.key !== 'd' && e.key !== 'D')) return;
      if (!visible || isEditableTarget(e.target)) return;
      const state = useEditorStore.getState();
      if (state.selection?.kind === 'node') {
        e.preventDefault();
        state.duplicateNode(state.selection.id);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [visible]);

  // Clipboard: Cmd/Ctrl+C copy, X cut, V paste, A select-all (ignored while
  // typing so native clipboard/select-all keeps working in text fields).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const key = e.key.toLowerCase();
      if (key !== 'c' && key !== 'x' && key !== 'v' && key !== 'a') return;
      if (!visible || isEditableTarget(e.target)) return;
      const state = useEditorStore.getState();
      if (key === 'c') state.copySelection();
      else if (key === 'x') state.cutSelection();
      else if (key === 'v') state.pasteClipboard();
      else state.selectAll();
      // Copy stays non-preventing so selected page text still reaches the
      // system clipboard; the others must not fall through to the browser.
      if (key !== 'c') e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [visible]);

  // Undo/redo with Cmd/Ctrl+Z, Shift+Cmd/Ctrl+Z or Ctrl+Y (ignored while
  // typing so native text-field undo keeps working).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const key = e.key.toLowerCase();
      const undo = (e.metaKey || e.ctrlKey) && key === 'z' && !e.shiftKey;
      const redo =
        ((e.metaKey || e.ctrlKey) && key === 'z' && e.shiftKey) || (e.ctrlKey && key === 'y');
      if (!undo && !redo) return;
      if (!visible || isEditableTarget(e.target)) return;
      e.preventDefault();
      const state = useEditorStore.getState();
      if (undo) state.undo();
      else state.redo();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [visible]);

  // Never lose edits on tab close: flush the debounced save.
  useEffect(() => {
    const flush = () => useEditorStore.getState().flushSave();
    window.addEventListener('beforeunload', flush);
    return () => {
      window.removeEventListener('beforeunload', flush);
      flush();
    };
  }, []);

  const addAtViewportCenter = useCallback(
    (type: string) => {
      const rect = canvasWrapRef.current?.getBoundingClientRect();
      if (rect === undefined) return;
      const center = screenToFlowPosition({
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
      });
      addNodeAtPosition(type, center);
    },
    [addNodeAtPosition, screenToFlowPosition],
  );

  const highlightSuggestion = useCallback(
    (s: Suggestion | null) => {
      setHighlight(s === null ? null : { nodeIds: s.node_ids, severity: s.severity });
    },
    [setHighlight],
  );

  const focusSuggestion = useCallback(
    (s: Suggestion) => {
      highlightSuggestion(s);
      const existing = new Set(useEditorStore.getState().nodes.map((n) => n.id));
      const ids = s.node_ids.filter((id) => existing.has(id));
      if (ids.length > 0) {
        void fitView({
          nodes: ids.map((id) => ({ id })),
          duration: 300,
          padding: 0.4,
          maxZoom: 1.25,
        });
      }
    },
    [fitView, highlightSuggestion],
  );

  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
    setHighlight(null);
  }, [setHighlight]);

  const openRun = useCallback(
    (mode: 'sync' | 'async') => {
      // Snapshot the CURRENT start-node variables and history head. The latter
      // lets completion distinguish a newly persisted run from an older one
      // even when localStorage is unavailable.
      setRunVariables(currentStartVariables(useEditorStore.getState().nodes));
      historyHeadBeforeRunRef.current = loadRunHistory(activeId)[0]?.at ?? null;
      try {
        workflowFingerprintBeforeRunRef.current = workflowFingerprint(getYaml());
      } catch {
        workflowFingerprintBeforeRunRef.current = null;
      }
      setRunMode(mode);
      setRunOpen(true);
    },
    [activeId, getYaml],
  );

  const focusExecutionNode = useCallback(
    (nodeId: string) => {
      const exists = useEditorStore.getState().nodes.some((node) => node.id === nodeId);
      if (!exists) return;
      selectSingleNode(nodeId);
      const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      void fitView({
        nodes: [{ id: nodeId }],
        duration: reduceMotion ? 0 : 240,
        padding: 0.65,
        maxZoom: 1.35,
      });
    },
    [fitView, selectSingleNode],
  );

  const applyReplayStep = useCallback(
    (step: number) => {
      if (replayEntry === null) return;
      const traces = orderedNodeTraces(replayEntry.node_traces);
      if (traces.length === 0) return;
      const next = Math.min(Math.max(Math.round(step), 0), traces.length - 1);
      setReplayStep(next);
      if (!canProjectRun(replayEntry, activeWorkflowFingerprint)) return;
      previewRunHistory(replayEntry, next);
      const trace = traces[next];
      if (trace !== undefined) focusExecutionNode(trace.node_id);
    },
    [activeWorkflowFingerprint, focusExecutionNode, previewRunHistory, replayEntry],
  );

  const selectReplayStep = useCallback(
    (step: number) => {
      setReplayPlaying(false);
      applyReplayStep(step);
    },
    [applyReplayStep],
  );

  const selectExecutionHistory = useCallback(
    (startedAt: string | null) => {
      setReplayPlaying(false);
      setReplayStep(null);
      setSelectedHistoryAt(startedAt);
      const entry =
        startedAt === null
          ? currentRunSnapshot
          : (runHistory.find((candidate) => candidate.at === startedAt) ?? null);
      if (entry === null) restoreLatestExecution();
      else if (canProjectRun(entry, activeWorkflowFingerprint)) previewRunHistory(entry, null);
      else restoreLatestExecution();
    },
    [
      activeWorkflowFingerprint,
      currentRunSnapshot,
      previewRunHistory,
      restoreLatestExecution,
      runHistory,
    ],
  );

  const openExecutionInspector = useCallback(() => {
    const next = loadRunHistory(activeId);
    setRunHistory(next);
    setReplayPlaying(false);
    setReplayStep(null);
    if (run.name === 'running' || run.name === 'job') {
      setSelectedHistoryAt(null);
    } else if (run.name === 'result' || run.name === 'error') {
      setSelectedHistoryAt(null);
      if (
        currentRunSnapshot !== null &&
        canProjectRun(currentRunSnapshot, activeWorkflowFingerprint)
      ) {
        previewRunHistory(currentRunSnapshot, null);
      }
    } else {
      const latest = next[0] ?? null;
      setSelectedHistoryAt(latest?.at ?? null);
      if (latest !== null && canProjectRun(latest, activeWorkflowFingerprint)) {
        previewRunHistory(latest, null);
      }
    }
    setInspectorOpen(true);
  }, [activeId, activeWorkflowFingerprint, currentRunSnapshot, previewRunHistory, run]);

  const restoreDisplayedExecution = useCallback(() => {
    if (
      currentRunSnapshot !== null &&
      (run.name === 'result' || run.name === 'error')
    ) {
      if (canProjectRun(currentRunSnapshot, activeWorkflowFingerprint)) {
        previewRunHistory(currentRunSnapshot, null);
      } else {
        restoreLatestExecution();
      }
    } else {
      restoreLatestExecution();
    }
  }, [
    activeWorkflowFingerprint,
    currentRunSnapshot,
    previewRunHistory,
    restoreLatestExecution,
    run.name,
  ]);

  const closeExecutionInspector = useCallback(() => {
    setReplayPlaying(false);
    setReplayStep(null);
    setInspectorOpen(false);
    restoreDisplayedExecution();
  }, [restoreDisplayedExecution]);

  // Hidden pages stay mounted in App.tsx. Close the global compact dialog as
  // soon as Workflows loses visibility so its document-level focus handling
  // cannot survive navigation to another page.
  useEffect(() => {
    if (!visible && inspectorOpen) closeExecutionInspector();
  }, [closeExecutionInspector, inspectorOpen, visible]);

  // The compact inspector is portalled outside #root. Marking the application
  // root inert therefore isolates the whole sidebar, toolbar, and canvas while
  // leaving the dialog itself interactive.
  useEffect(() => {
    if (!inspectorOpen || !compactInspector) return;
    const appRoot = document.getElementById('root');
    if (appRoot === null) return;
    const previousInert = appRoot.inert;
    const previousAriaHidden = appRoot.getAttribute('aria-hidden');
    appRoot.inert = true;
    appRoot.setAttribute('aria-hidden', 'true');
    return () => {
      appRoot.inert = previousInert;
      if (previousAriaHidden === null) appRoot.removeAttribute('aria-hidden');
      else appRoot.setAttribute('aria-hidden', previousAriaHidden);
    };
  }, [compactInspector, inspectorOpen]);

  const toggleReplay = useCallback(() => {
    if (replayTraces.length < 2) return;
    if (replayPlaying) {
      setReplayPlaying(false);
      return;
    }
    const active = replayStep === null ? replayTraces.length - 1 : replayStep;
    if (active >= replayTraces.length - 1) applyReplayStep(0);
    setReplayPlaying(true);
  }, [applyReplayStep, replayPlaying, replayStep, replayTraces.length]);

  useEffect(() => {
    if (!replayPlaying || replayTraces.length < 2) return;
    const active = replayStep === null ? replayTraces.length - 1 : replayStep;
    if (active >= replayTraces.length - 1) {
      setReplayPlaying(false);
      return;
    }
    const timer = window.setTimeout(() => applyReplayStep(active + 1), 850);
    return () => window.clearTimeout(timer);
  }, [applyReplayStep, replayPlaying, replayStep, replayTraces.length]);

  const clearExecutionHistory = useCallback(() => {
    deleteRunHistory(activeId);
    setRunHistory([]);
    setSelectedHistoryAt(null);
    setReplayStep(null);
    setReplayPlaying(false);
    restoreDisplayedExecution();
  }, [activeId, restoreDisplayedExecution]);

  const executionInspector = (
    <ExecutionInspector
      open={inspectorOpen}
      run={run}
      history={runHistory}
      selectedHistoryAt={selectedHistoryAt}
      replayStep={replayStep}
      selectedNodeId={canvasProjectionAvailable ? selectedNodeId : null}
      currentRunKey={currentRunSnapshot?.at ?? null}
      canvasProjectionAvailable={canvasProjectionAvailable}
      projectionWarning={projectionWarning}
      modal={compactInspector}
      isPlaying={replayPlaying}
      onClose={closeExecutionInspector}
      onSelectHistory={selectExecutionHistory}
      onSelectStep={selectReplayStep}
      onTogglePlayback={toggleReplay}
      onClearHistory={clearExecutionHistory}
    />
  );

  return (
    <div ref={rootRef} className="flex h-full flex-col">
      <Toolbar
        onImport={() => setImportOpen(true)}
        onCompile={() => setCompileOpen(true)}
        onRun={openRun}
        onOpenSuggestions={() => setDrawerOpen(true)}
        onOpenExecution={openExecutionInspector}
        executionAvailable={
          runHistory.length > 0 || run.name !== 'idle' || execution.status !== 'idle'
        }
        executionCount={runHistory.length}
      />
      <div className="relative flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1">
          <Palette onAdd={addAtViewportCenter} />
          <div ref={canvasWrapRef} className="relative min-w-0 flex-1">
            <Canvas
              hotkeysEnabled={visible && !(inspectorOpen && compactInspector)}
              onImportClick={() => setImportOpen(true)}
            />
            {drawerOpen && analysis !== null && (
              <div className="absolute inset-x-0 bottom-0 z-20">
                <SuggestionsDrawer
                  slice={analysis}
                  onClose={closeDrawer}
                  onHighlight={highlightSuggestion}
                  onFocus={focusSuggestion}
                />
              </div>
            )}
          </div>
        </div>
        {inspectorOpen ? (
          compactInspector ? (
            createPortal(
              <div className="fixed inset-0 z-[100] overflow-hidden bg-zinc-950">
                {executionInspector}
              </div>,
              document.body,
            )
          ) : (
            <div className="h-full min-w-0">{executionInspector}</div>
          )
        ) : (
          <PropertyPanel />
        )}
      </div>

      <ImportModal
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onImport={importYaml}
      />
      <CompileModal
        open={compileOpen}
        onClose={() => setCompileOpen(false)}
        getYaml={getYaml}
        fold={fold}
      />
      <RunModal
        open={runOpen}
        onClose={() => setRunOpen(false)}
        mode={runMode}
        workflowId={activeId}
        startVariables={runVariables}
        fold={fold}
        onFoldChange={setFold}
      />
    </div>
  );
}

export function WorkflowsPage() {
  return (
    <ReactFlowProvider>
      <EditorShell />
    </ReactFlowProvider>
  );
}
