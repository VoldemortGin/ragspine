/** Top toolbar: workflow switcher/library, document actions (auto-layout,
 * analyze, compile, run, import/export), app settings, save indicator. */

import { useState } from 'react';

import { analyzeWorkflow, convertN8n } from '../../api/client';
import {
  Badge,
  Button,
  Field,
  IconBraces,
  IconCheck,
  IconChevronDown,
  IconClock,
  IconCode,
  IconCopy,
  IconDownload,
  IconNetwork,
  IconPencil,
  IconPlay,
  IconPlus,
  IconRedo,
  IconSearch,
  IconSettings,
  IconTrash,
  IconUndo,
  IconUpload,
  IconWorkflow,
  Modal,
  Select,
  Spinner,
  TextInput,
  cn,
  useCopy,
} from '../../components';
import { TemplatesModal } from './modals/TemplatesModal';
import type { LibraryEntry } from './model/library';
import { describeApiError, formatUpdatedAt, slugify, useDismiss } from './shared';
import { useEditorStore } from './store';

/* ---------------------------- save indicator ---------------------------- */

function SaveIndicator() {
  const saveState = useEditorStore((s) => s.saveState);
  return (
    <span
      className="inline-flex w-16 items-center gap-1.5 text-[11px]"
      title={saveState === 'saved' ? 'All changes saved locally' : 'Saving to the local library…'}
    >
      <span
        className={cn(
          'h-1.5 w-1.5 rounded-full',
          saveState === 'saved' ? 'bg-emerald-400' : 'animate-pulse bg-amber-400',
        )}
      />
      <span className={saveState === 'saved' ? 'text-zinc-600' : 'text-amber-300/80'}>
        {saveState === 'saved' ? 'Saved' : 'Saving…'}
      </span>
    </span>
  );
}

/* --------------------------- workflow switcher -------------------------- */

function SwitcherRow({
  entry,
  active,
  onDone,
}: {
  entry: LibraryEntry;
  active: boolean;
  onDone: () => void;
}) {
  const switchWorkflow = useEditorStore((s) => s.switchWorkflow);
  const renameWorkflow = useEditorStore((s) => s.renameWorkflow);
  const duplicateWorkflow = useEditorStore((s) => s.duplicateWorkflow);
  const deleteWorkflow = useEditorStore((s) => s.deleteWorkflow);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(entry.name);
  const [confirming, setConfirming] = useState(false);

  const commitRename = () => {
    renameWorkflow(entry.id, draft);
    setRenaming(false);
  };

  if (renaming) {
    return (
      <div className="px-2 py-1.5">
        <TextInput
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitRename}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commitRename();
            if (e.key === 'Escape') setRenaming(false);
          }}
          className="h-7 text-xs"
          aria-label="Workflow name"
        />
      </div>
    );
  }

  return (
    <div
      className={cn(
        'group flex items-center gap-2 rounded-md px-2 py-1.5',
        active ? 'bg-white/[0.06]' : 'hover:bg-white/[0.03]',
      )}
    >
      <button
        type="button"
        className="min-w-0 flex-1 text-left"
        onClick={() => {
          switchWorkflow(entry.id);
          onDone();
        }}
      >
        <div
          className={cn(
            'truncate text-xs',
            active ? 'font-medium text-zinc-100' : 'text-zinc-300',
          )}
        >
          {entry.name}
        </div>
        <div className="text-[10px] text-zinc-600">{formatUpdatedAt(entry.updatedAt)}</div>
      </button>
      <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
        <button
          type="button"
          title="Rename"
          onClick={() => {
            setDraft(entry.name);
            setRenaming(true);
          }}
          className="rounded p-1 text-zinc-500 hover:bg-white/5 hover:text-zinc-200"
        >
          <IconPencil size={12} />
        </button>
        <button
          type="button"
          title="Duplicate"
          onClick={() => {
            duplicateWorkflow(entry.id);
            onDone();
          }}
          className="rounded p-1 text-zinc-500 hover:bg-white/5 hover:text-zinc-200"
        >
          <IconCopy size={12} />
        </button>
        {confirming ? (
          <button
            type="button"
            title="Click again to delete"
            onClick={() => {
              deleteWorkflow(entry.id);
              onDone();
            }}
            className="rounded bg-red-500/15 px-1.5 py-1 text-[10px] font-medium text-red-300"
          >
            Confirm
          </button>
        ) : (
          <button
            type="button"
            title="Delete"
            onClick={() => setConfirming(true)}
            className="rounded p-1 text-zinc-500 hover:bg-red-400/10 hover:text-red-300"
          >
            <IconTrash size={12} />
          </button>
        )}
      </div>
    </div>
  );
}

function WorkflowSwitcher() {
  const library = useEditorStore((s) => s.library);
  const activeId = useEditorStore((s) => s.activeId);
  const name = useEditorStore((s) => s.base.name);
  const createWorkflow = useEditorStore((s) => s.createWorkflow);
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'flex h-8 max-w-64 items-center gap-2 rounded-md border border-white/10 bg-white/[0.03] px-2.5',
          'text-xs transition-colors hover:bg-white/[0.06]',
        )}
      >
        <IconWorkflow size={13} className="shrink-0 text-indigo-400" />
        <span className="truncate font-medium text-zinc-200">
          {name.trim() !== '' ? name : 'Untitled workflow'}
        </span>
        <IconChevronDown size={12} className="shrink-0 text-zinc-500" />
      </button>
      {open && (
        <div className="absolute top-full left-0 z-40 mt-1 w-80 rounded-lg border border-white/10 bg-zinc-900 p-1.5 shadow-2xl shadow-black/50">
          <div className="mb-1 px-2 pt-1 text-[10px] font-semibold tracking-[0.14em] text-zinc-600 uppercase">
            Workflows
          </div>
          <div className="max-h-72 space-y-0.5 overflow-y-auto">
            {library.map((entry) => (
              <SwitcherRow
                key={entry.id}
                entry={entry}
                active={entry.id === activeId}
                onDone={() => setOpen(false)}
              />
            ))}
          </div>
          <div className="mt-1.5 border-t border-white/5 pt-1.5">
            <button
              type="button"
              onClick={() => {
                createWorkflow();
                setOpen(false);
              }}
              className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-zinc-400 transition-colors hover:bg-white/[0.04] hover:text-zinc-200"
            >
              <IconPlus size={13} />
              New workflow
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ------------------------------ export menu ----------------------------- */

function downloadFile(content: string, filename: string, type: string) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function ExportMenu() {
  const getYaml = useEditorStore((s) => s.getYaml);
  const name = useEditorStore((s) => s.base.name);
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const [copied, copy] = useCopy();
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  /** Pending n8n download held behind a warnings confirmation modal. */
  const [n8nExport, setN8nExport] = useState<{ warnings: string[]; json: string } | null>(null);

  const download = () => {
    try {
      downloadFile(getYaml(), `${slugify(name)}.yml`, 'application/yaml');
    } finally {
      setOpen(false);
    }
  };

  const downloadN8n = (json: string) => {
    downloadFile(json, `${slugify(name)}.n8n.json`, 'application/json');
  };

  const exportN8n = () => {
    setExporting(true);
    setExportError(null);
    Promise.resolve()
      .then(() => convertN8n('dify_to_n8n', getYaml()))
      .then((res) => {
        const json = JSON.stringify(res.workflow, null, 2);
        setOpen(false);
        if (res.warnings.length > 0) {
          setN8nExport({ warnings: res.warnings, json });
        } else {
          downloadN8n(json);
        }
      })
      .catch((error: unknown) => setExportError(describeApiError(error).message))
      .finally(() => setExporting(false));
  };

  const itemClass =
    'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-zinc-300 transition-colors hover:bg-white/[0.05]';

  return (
    <div className="relative" ref={ref}>
      <Button variant="ghost" size="sm" onClick={() => setOpen((v) => !v)} title="Export workflow">
        <IconDownload size={13} />
        Export
      </Button>
      {open && (
        <div className="absolute top-full right-0 z-40 mt-1 w-44 rounded-lg border border-white/10 bg-zinc-900 p-1 shadow-2xl shadow-black/50">
          <button type="button" onClick={download} className={itemClass}>
            <IconDownload size={13} className="text-zinc-500" />
            Download .yml
          </button>
          <button type="button" onClick={() => copy(getYaml())} className={itemClass}>
            {copied ? (
              <IconCheck size={13} className="text-emerald-400" />
            ) : (
              <IconCopy size={13} className="text-zinc-500" />
            )}
            {copied ? 'Copied' : 'Copy YAML'}
          </button>
          <button
            type="button"
            onClick={exportN8n}
            disabled={exporting}
            title="Convert to n8n via the RAGSpine server and download JSON"
            className={itemClass}
          >
            {exporting ? (
              <Spinner size="sm" />
            ) : (
              <IconBraces size={13} className="text-zinc-500" />
            )}
            Export n8n JSON
          </button>
          {exportError !== null && (
            <div className="mt-1 border-t border-white/5 px-2 py-1.5 text-[11px] leading-4 text-red-300">
              {exportError}
            </div>
          )}
        </div>
      )}
      <Modal
        open={n8nExport !== null}
        onClose={() => setN8nExport(null)}
        title="n8n export warnings"
        size="md"
        footer={
          <>
            <Button variant="ghost" onClick={() => setN8nExport(null)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                if (n8nExport !== null) downloadN8n(n8nExport.json);
                setN8nExport(null);
              }}
            >
              Download anyway
            </Button>
          </>
        }
      >
        <div className="rounded-lg border border-amber-400/25 bg-amber-400/[0.06] px-4 py-3">
          <div className="text-sm font-medium text-amber-300">
            The conversion completed with warnings
          </div>
          <ul className="mt-1.5 list-disc space-y-1 pl-4 text-xs leading-5 text-zinc-400">
            {(n8nExport?.warnings ?? []).map((warning, i) => (
              <li key={i}>{warning}</li>
            ))}
          </ul>
        </div>
      </Modal>
    </div>
  );
}

/* ----------------------------- settings modal --------------------------- */

function SettingsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const base = useEditorStore((s) => s.base);
  const setAppMeta = useEditorStore((s) => s.setAppMeta);
  return (
    <Modal open={open} onClose={onClose} title="Workflow settings" size="sm">
      <div className="space-y-3">
        <Field label="App name">
          <TextInput
            value={base.name}
            onChange={(e) => setAppMeta({ name: e.target.value, mode: base.mode })}
            placeholder="Untitled workflow"
          />
        </Field>
        <Field label="Mode" hint="advanced-chat enables answer nodes and chat semantics.">
          <Select
            value={base.mode}
            onChange={(e) =>
              setAppMeta({
                name: base.name,
                mode: e.target.value === 'advanced-chat' ? 'advanced-chat' : 'workflow',
              })
            }
          >
            <option value="workflow">workflow</option>
            <option value="advanced-chat">advanced-chat</option>
          </Select>
        </Field>
        <Field label="Version">
          <div className="flex h-8.5 items-center rounded-md border border-white/5 bg-white/[0.02] px-2.5 font-mono text-xs text-zinc-500">
            {base.version}
          </div>
        </Field>
      </div>
    </Modal>
  );
}

/* -------------------------------- toolbar ------------------------------- */

export interface ToolbarProps {
  onImport: () => void;
  onCompile: () => void;
  onRun: (mode: 'sync' | 'async') => void;
  onOpenSuggestions: () => void;
}

export function Toolbar({ onImport, onCompile, onRun, onOpenSuggestions }: ToolbarProps) {
  const autoLayout = useEditorStore((s) => s.autoLayout);
  const undo = useEditorStore((s) => s.undo);
  const redo = useEditorStore((s) => s.redo);
  const canUndo = useEditorStore((s) => s.past.length > 0);
  const canRedo = useEditorStore((s) => s.future.length > 0);
  const getYaml = useEditorStore((s) => s.getYaml);
  const setAnalysis = useEditorStore((s) => s.setAnalysis);
  const analysis = useEditorStore((s) => s.analysis);
  const [analyzing, setAnalyzing] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [templatesOpen, setTemplatesOpen] = useState(false);

  const analyze = () => {
    setAnalyzing(true);
    setAnalysis({ status: 'loading' });
    onOpenSuggestions();
    Promise.resolve()
      .then(() => analyzeWorkflow(getYaml()))
      .then((res) =>
        setAnalysis({ status: 'done', requestId: res.request_id, suggestions: res.suggestions }),
      )
      .catch((error: unknown) => setAnalysis({ status: 'error', error }))
      .finally(() => setAnalyzing(false));
  };

  const suggestionCount = analysis?.status === 'done' ? analysis.suggestions.length : null;

  return (
    <div className="flex h-12 shrink-0 items-center gap-2 border-b border-white/10 px-3">
      <WorkflowSwitcher />
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setTemplatesOpen(true)}
        title="Create a new workflow from a template"
      >
        <IconPlus size={13} />
        New
      </Button>

      <div className="flex-1" />

      <Button variant="ghost" size="sm" onClick={undo} disabled={!canUndo} title="Undo (Ctrl/⌘+Z)">
        <IconUndo size={13} />
      </Button>
      <Button
        variant="ghost"
        size="sm"
        onClick={redo}
        disabled={!canRedo}
        title="Redo (Shift+Ctrl/⌘+Z)"
      >
        <IconRedo size={13} />
      </Button>
      <Button variant="ghost" size="sm" onClick={autoLayout} title="Auto-layout the graph (dagre)">
        <IconNetwork size={13} />
        Auto-layout
      </Button>
      <Button
        variant="ghost"
        size="sm"
        onClick={analyze}
        disabled={analyzing}
        title="Analyze the workflow for optimization suggestions"
      >
        {analyzing ? <Spinner size="sm" /> : <IconSearch size={13} />}
        Analyze
        {suggestionCount !== null && (
          <Badge variant={suggestionCount > 0 ? 'warn' : 'success'} className="!px-1.5">
            {suggestionCount}
          </Badge>
        )}
      </Button>
      <Button variant="ghost" size="sm" onClick={onCompile} title="Compile to RAGSpine Python">
        <IconCode size={13} />
        Compile
      </Button>
      <Button variant="ghost" size="sm" onClick={() => onRun('sync')} title="Run the workflow">
        <IconPlay size={13} />
        Run
      </Button>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => onRun('async')}
        title="Run as a background job"
      >
        <IconClock size={13} />
        Run async
      </Button>

      <span className="mx-1 h-5 w-px bg-white/10" />

      <Button variant="ghost" size="sm" onClick={onImport} title="Import a workflow YAML">
        <IconUpload size={13} />
        Import
      </Button>
      <ExportMenu />

      <span className="mx-1 h-5 w-px bg-white/10" />

      <button
        type="button"
        onClick={() => setSettingsOpen(true)}
        title="Workflow settings (name, mode)"
        className="rounded-md p-1.5 text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-300"
      >
        <IconSettings size={14} />
      </button>
      <SaveIndicator />

      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <TemplatesModal open={templatesOpen} onClose={() => setTemplatesOpen(false)} />
    </div>
  );
}
