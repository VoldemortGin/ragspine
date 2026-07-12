/** RAGSpine Studio — visual Dify workflow editor page. */

import { ReactFlowProvider, useReactFlow } from '@xyflow/react';
import { useCallback, useEffect, useRef, useState } from 'react';

import type { Suggestion } from '../../api/types';
import type { StudioFlowEdge, StudioFlowNode } from '../../workflow/reactflow';
import type { StartNodeData, StartVariable } from '../../workflow/types';
import { Canvas } from './Canvas';
import { Palette } from './Palette';
import { PropertyPanel } from './PropertyPanel';
import { Toolbar } from './Toolbar';
import { CompileModal } from './modals/CompileModal';
import { ImportModal } from './modals/ImportModal';
import { RunModal } from './modals/RunModal';
import { SuggestionsDrawer } from './modals/SuggestionsDrawer';
import { isEditableTarget, useElementVisible } from './shared';
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

function EditorShell() {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const canvasWrapRef = useRef<HTMLDivElement | null>(null);
  const visible = useElementVisible(rootRef);

  const [importOpen, setImportOpen] = useState(false);
  const [compileOpen, setCompileOpen] = useState(false);
  const [runOpen, setRunOpen] = useState(false);
  const [runMode, setRunMode] = useState<'sync' | 'async'>('sync');
  const [drawerOpen, setDrawerOpen] = useState(false);

  const activeId = useEditorStore((s) => s.activeId);
  const fold = useEditorStore((s) => s.fold);
  const setFold = useEditorStore((s) => s.setFold);
  const analysis = useEditorStore((s) => s.analysis);
  const setHighlight = useEditorStore((s) => s.setHighlight);
  const importYaml = useEditorStore((s) => s.importYaml);
  const getYaml = useEditorStore((s) => s.getYaml);
  const addNodeAtPosition = useEditorStore((s) => s.addNodeAtPosition);
  const [runVariables, setRunVariables] = useState<StartVariable[]>([]);

  const { fitView, screenToFlowPosition } = useReactFlow<StudioFlowNode, StudioFlowEdge>();

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

  const openRun = useCallback((mode: 'sync' | 'async') => {
    // Snapshot the CURRENT start-node variables when the modal opens.
    setRunVariables(currentStartVariables(useEditorStore.getState().nodes));
    setRunMode(mode);
    setRunOpen(true);
  }, []);

  return (
    <div ref={rootRef} className="flex h-full flex-col">
      <Toolbar
        onImport={() => setImportOpen(true)}
        onCompile={() => setCompileOpen(true)}
        onRun={openRun}
        onOpenSuggestions={() => setDrawerOpen(true)}
      />
      <div className="flex min-h-0 flex-1">
        <Palette onAdd={addAtViewportCenter} />
        <div ref={canvasWrapRef} className="relative min-w-0 flex-1">
          <Canvas hotkeysEnabled={visible} onImportClick={() => setImportOpen(true)} />
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
        <PropertyPanel />
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
