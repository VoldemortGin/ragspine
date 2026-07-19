/** Modal that runs the current workflow (sync or async via job polling).
 *
 * The run lifecycle itself lives in the editor store (startRun) — this modal
 * only collects inputs and subscribes to the store's `run` slice, so closing
 * it never cancels an in-flight run and the canvas keeps its pulse/badges. */

import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

import type { NodeTrace } from '../../../api/types';
import {
  Badge,
  Button,
  Checkbox,
  Field,
  IconAlertTriangle,
  IconCheck,
  IconClock,
  IconCopy,
  IconPlay,
  IconX,
  JsonView,
  Modal,
  Select,
  Spinner,
  TextArea,
  TextInput,
  cn,
  useCopy,
} from '../../../components';
import type { StartVariable } from '../../../workflow/types';
import { formatElapsedMs, loadRunHistory } from '../model/execution';
import type { RunHistoryEntry } from '../model/execution';
import { loadRunInputs, saveRunInputs } from '../model/library';
import {
  initializeRunInputs,
  parseRunInputsJson,
  serializeRunInputs,
  startInputKind,
  startInputOptions,
  validateRunInputs,
} from '../model/runInputs';
import { ApiErrorCallout, FormHint, formatUpdatedAt } from '../shared';
import { useEditorStore } from '../store';

export interface RunModalProps {
  open: boolean;
  onClose: () => void;
  mode: 'sync' | 'async';
  workflowId: string;
  startVariables: StartVariable[];
  fold: boolean;
  onFoldChange: (v: boolean) => void;
}

function controlDraft(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value) ?? '';
  } catch {
    return '';
  }
}

function JobId({ value }: { value: string }) {
  const [copied, copy] = useCopy();
  return (
    <button
      type="button"
      onClick={() => copy(value)}
      title="Copy job id"
      className="inline-flex items-center gap-1.5 rounded border border-white/10 bg-white/[0.03] px-2 py-1 font-mono text-[11px] text-zinc-400 transition-colors hover:bg-white/5 hover:text-zinc-200"
    >
      {copied ? <IconCheck size={11} className="text-emerald-400" /> : <IconCopy size={11} />}
      {value}
    </button>
  );
}

function FailureCallout({ title, message }: { title: string; message: string }) {
  return (
    <div className="rounded-lg border border-red-400/25 bg-red-400/[0.06] px-4 py-3">
      <div className="text-sm font-medium text-red-300">{title}</div>
      <div className="mt-1 text-xs leading-5 whitespace-pre-wrap text-zinc-400">{message}</div>
    </div>
  );
}

function WarningList({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null;
  return (
    <div className="space-y-1">
      {warnings.map((warning, i) => (
        <div key={i} className="flex items-start gap-1.5 text-xs leading-5 text-amber-200">
          <IconAlertTriangle size={13} className="mt-1 shrink-0 text-amber-400" />
          <span>{warning}</span>
        </div>
      ))}
    </div>
  );
}

function StatusIcon({ status }: { status: NodeTrace['status'] }) {
  if (status === 'succeeded') return <IconCheck size={12} className="shrink-0 text-emerald-400" />;
  if (status === 'failed') return <IconX size={12} className="shrink-0 text-red-400" />;
  return <span className="w-3 shrink-0 text-center text-xs leading-none text-zinc-600">⊘</span>;
}

/** Sum of per-node timings; folded pairs share one timing, so approximate. */
function totalElapsedMs(traces: NodeTrace[] | null): number | null {
  if (traces === null || traces.length === 0) return null;
  return traces.reduce((sum, t) => sum + (Number.isFinite(t.elapsed_ms) ? t.elapsed_ms : 0), 0);
}

/** Per-node run timeline; clicking a row selects that node on the canvas. */
function TraceTimeline({
  traces,
  onPick,
}: {
  traces: NodeTrace[];
  onPick: (nodeId: string) => void;
}) {
  const nodes = useEditorStore((s) => s.nodes);
  const existing = useMemo(() => new Set(nodes.map((n) => n.id)), [nodes]);
  const sorted = useMemo(() => [...traces].sort((a, b) => a.index - b.index), [traces]);
  if (sorted.length === 0) return null;
  return (
    <div>
      <div className="mb-1.5 text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
        Node timeline
      </div>
      <div className="max-h-56 divide-y divide-white/5 overflow-y-auto rounded-lg border border-white/10 bg-zinc-900/70">
        {sorted.map((trace, i) => {
          const canPick = existing.has(trace.node_id);
          return (
            <button
              key={`${trace.index}-${trace.node_id}-${i}`}
              type="button"
              disabled={!canPick}
              onClick={() => onPick(trace.node_id)}
              title={canPick ? 'Select node on canvas' : undefined}
              className={cn(
                'flex w-full items-center gap-2 px-3 py-1.5 text-left',
                canPick ? 'transition-colors hover:bg-white/5' : 'cursor-default',
              )}
            >
              <StatusIcon status={trace.status} />
              <span
                className={cn(
                  'min-w-0 flex-1 truncate text-xs',
                  trace.status === 'skipped' ? 'text-zinc-600' : 'text-zinc-200',
                )}
              >
                {trace.title !== '' ? trace.title : trace.node_id}
              </span>
              {trace.status !== 'skipped' && (
                <span className="shrink-0 font-mono text-[10px] text-zinc-500">
                  {formatElapsedMs(trace.elapsed_ms)}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function ResultBody({
  result,
  warnings,
  requestId,
  traces,
  onPick,
}: {
  result: unknown;
  warnings: string[];
  requestId: string | null;
  traces: NodeTrace[] | null;
  onPick: (nodeId: string) => void;
}) {
  return (
    <div className="space-y-3">
      <div className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
        Result
      </div>
      {typeof result === 'string' ? (
        <div className="max-h-80 overflow-y-auto rounded-lg border border-white/10 bg-zinc-900/70 px-3.5 py-2.5 text-sm leading-6 whitespace-pre-wrap text-zinc-200">
          {result}
        </div>
      ) : (
        <JsonView value={result} maxHeight="20rem" />
      )}
      <WarningList warnings={warnings} />
      {traces !== null && <TraceTimeline traces={traces} onPick={onPick} />}
      {requestId !== null && (
        <div className="font-mono text-[10px] text-zinc-600">request {requestId}</div>
      )}
    </div>
  );
}

/** Read-only detail of one stored run (result view + timeline). */
function HistoryDetail({
  entry,
  onPick,
}: {
  entry: RunHistoryEntry;
  onPick: (nodeId: string) => void;
}) {
  const total = totalElapsedMs(entry.node_traces);
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Badge variant={entry.status === 'succeeded' ? 'success' : 'danger'}>{entry.status}</Badge>
        <span className="text-xs text-zinc-500">{new Date(entry.at).toLocaleString()}</span>
        {total !== null && (
          <span className="ml-auto font-mono text-[10px] text-zinc-500">
            {formatElapsedMs(total)}
          </span>
        )}
      </div>
      {entry.status === 'failed' ? (
        <>
          <FailureCallout title="Run failed" message={entry.error ?? 'No error message.'} />
          {entry.node_traces !== null && (
            <TraceTimeline traces={entry.node_traces} onPick={onPick} />
          )}
        </>
      ) : (
        <ResultBody
          result={entry.result}
          warnings={entry.warnings}
          requestId={null}
          traces={entry.node_traces}
          onPick={onPick}
        />
      )}
    </div>
  );
}

function RunHistoryList({
  history,
  onView,
}: {
  history: RunHistoryEntry[];
  onView: (entry: RunHistoryEntry) => void;
}) {
  if (history.length === 0) return null;
  return (
    <div>
      <div className="mb-1.5 text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
        Recent runs
      </div>
      <div className="max-h-48 divide-y divide-white/5 overflow-y-auto rounded-lg border border-white/10 bg-white/[0.02]">
        {history.map((entry, i) => {
          const total = totalElapsedMs(entry.node_traces);
          return (
            <button
              key={`${entry.at}-${i}`}
              type="button"
              onClick={() => onView(entry)}
              title="View run details"
              className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-white/5"
            >
              <StatusIcon status={entry.status} />
              <span className="min-w-0 flex-1 truncate text-xs text-zinc-300">
                {formatUpdatedAt(entry.at)}
              </span>
              <Badge variant={entry.status === 'succeeded' ? 'success' : 'danger'}>
                {entry.status}
              </Badge>
              {total !== null && (
                <span className="shrink-0 font-mono text-[10px] text-zinc-500">
                  {formatElapsedMs(total)}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function RunModal({
  open,
  onClose,
  mode,
  workflowId,
  startVariables,
  fold,
  onFoldChange,
}: RunModalProps) {
  const run = useEditorStore((s) => s.run);
  const startRun = useEditorStore((s) => s.startRun);
  const resetRun = useEditorStore((s) => s.resetRun);
  const selectSingleNode = useEditorStore((s) => s.selectSingleNode);

  const [inputs, setInputs] = useState<Record<string, unknown>>({});
  const [rawOpen, setRawOpen] = useState(false);
  const [rawDraft, setRawDraft] = useState('{}');
  const [rawError, setRawError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [history, setHistory] = useState<RunHistoryEntry[]>([]);
  const [viewing, setViewing] = useState<RunHistoryEntry | null>(null);

  // On open: return a finished run to the form (an in-flight run keeps its
  // running/job view) and load the stored inputs + run history.
  useEffect(() => {
    if (!open) return;
    const state = useEditorStore.getState();
    if (state.run.name === 'result' || state.run.name === 'error') state.resetRun();
    setViewing(null);
    setFieldErrors({});
    setHistory(loadRunHistory(workflowId));
    const initialInputs = initializeRunInputs(startVariables, loadRunInputs(workflowId));
    setInputs(initialInputs);
    setRawDraft(serializeRunInputs(initialInputs));
    setRawError(null);
    setRawOpen(false);
    // Intentionally keyed on `open` only: props are read at open time.
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const pickNode = (nodeId: string) => {
    selectSingleNode(nodeId);
    onClose();
  };

  const backToForm = () => {
    setHistory(loadRunHistory(workflowId));
    resetRun();
  };

  const submit = () => {
    if (rawError !== null) return;
    const validated = validateRunInputs(startVariables, inputs);
    if (Object.keys(validated.errors).length > 0) {
      setFieldErrors(validated.errors);
      return;
    }
    setFieldErrors({});
    setInputs(validated.inputs);
    setRawDraft(serializeRunInputs(validated.inputs));
    saveRunInputs(workflowId, validated.inputs);
    startRun(mode, validated.inputs);
  };

  let footer: ReactNode;
  if (viewing !== null) {
    footer = (
      <>
        <Button variant="secondary" onClick={() => setViewing(null)}>
          Back
        </Button>
        <Button variant="primary" onClick={onClose}>
          Close
        </Button>
      </>
    );
  } else if (run.name === 'idle') {
    footer = (
      <>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button variant="primary" onClick={submit}>
          {mode === 'sync' ? <IconPlay size={13} /> : <IconClock size={13} />}
          {mode === 'sync' ? 'Run' : 'Run async'}
        </Button>
      </>
    );
  } else if (run.name === 'result') {
    footer = (
      <>
        <Button variant="secondary" onClick={backToForm}>
          Run again
        </Button>
        <Button variant="primary" onClick={onClose}>
          Close
        </Button>
      </>
    );
  } else if (run.name === 'error') {
    footer = (
      <>
        <Button variant="secondary" onClick={backToForm}>
          Back
        </Button>
        <Button variant="primary" onClick={onClose}>
          Close
        </Button>
      </>
    );
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={mode === 'sync' ? 'Run workflow' : 'Run workflow (async)'}
      size="lg"
      footer={footer}
    >
      {viewing !== null ? (
        <HistoryDetail entry={viewing} onPick={pickNode} />
      ) : (
        <>
          {run.name === 'idle' && (
            <div className="space-y-4">
              {startVariables.length === 0 ? (
                <FormHint>This workflow has no start variables. Run it with empty inputs.</FormHint>
              ) : (
                <div className="space-y-3">
                  {startVariables.map((variable) => {
                    const key = variable.variable;
                    const kind = startInputKind(variable);
                    const value = inputs[key];
                    const setValue = (next: unknown) => {
                      const updated = { ...inputs, [key]: next };
                      setInputs(updated);
                      setRawDraft(serializeRunInputs(updated));
                      setRawError(null);
                      setFieldErrors((prev) => {
                        if (!(key in prev)) return prev;
                        const rest = { ...prev };
                        delete rest[key];
                        return rest;
                      });
                    };
                    const label =
                      variable.label !== undefined && variable.label !== '' ? variable.label : key;
                    if (kind === 'boolean') {
                      return (
                        <div key={key}>
                          <div className="mb-1.5 flex items-baseline gap-1 text-xs font-medium text-zinc-400">
                            {label}
                            {variable.required === true && (
                              <span className="text-indigo-400">*</span>
                            )}
                          </div>
                          <Checkbox
                            aria-label={label}
                            checked={value === true}
                            label={value === true ? 'True' : 'False'}
                            onChange={(e) => setValue(e.target.checked)}
                          />
                          {fieldErrors[key] !== undefined && (
                            <div className="mt-1 text-[11px] leading-4 text-red-400">
                              {fieldErrors[key]}
                            </div>
                          )}
                        </div>
                      );
                    }
                    return (
                      <Field
                        key={key}
                        label={label}
                        required={variable.required === true}
                        error={fieldErrors[key]}
                      >
                        {kind === 'paragraph' ? (
                          <TextArea
                            rows={4}
                            value={controlDraft(value)}
                            onChange={(e) => setValue(e.target.value)}
                          />
                        ) : kind === 'number' ? (
                          <TextInput
                            type="number"
                            value={controlDraft(value)}
                            onChange={(e) => setValue(e.target.value)}
                          />
                        ) : kind === 'select' ? (
                          <Select
                            value={controlDraft(value)}
                            onChange={(e) => setValue(e.target.value)}
                          >
                            <option value="">Select an option</option>
                            {startInputOptions(variable).map((option) => (
                              <option key={option} value={option}>
                                {option}
                              </option>
                            ))}
                          </Select>
                        ) : (
                          <TextInput
                            value={controlDraft(value)}
                            onChange={(e) => setValue(e.target.value)}
                          />
                        )}
                      </Field>
                    );
                  })}
                  <div className="border-t border-white/10 pt-3">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      aria-expanded={rawOpen}
                      onClick={() => setRawOpen((value) => !value)}
                    >
                      Advanced JSON
                    </Button>
                    {rawOpen && (
                      <Field
                        label="Complete inputs object"
                        error={rawError ?? undefined}
                      >
                        <TextArea
                          aria-label="Advanced inputs JSON"
                          className="font-mono text-xs"
                          rows={8}
                          value={rawDraft}
                          onChange={(event) => {
                            const next = event.target.value;
                            setRawDraft(next);
                            const parsed = parseRunInputsJson(next);
                            setRawError(parsed.error);
                            if (parsed.inputs !== null) {
                              setInputs(parsed.inputs);
                              setFieldErrors({});
                            }
                          }}
                        />
                      </Field>
                    )}
                  </div>
                </div>
              )}
              <Checkbox
                label="Fold answer/question node pairs"
                checked={fold}
                onChange={(e) => onFoldChange(e.target.checked)}
              />
              <RunHistoryList history={history} onView={setViewing} />
            </div>
          )}

          {run.name === 'running' && (
            <div className="flex items-center justify-center gap-2 py-16 text-sm text-zinc-500">
              <Spinner size="sm" />
              Running workflow…
            </div>
          )}

          {run.name === 'job' && (
            <div className="flex flex-col items-center justify-center gap-3 py-12">
              <Badge variant={run.jobState === 'queued' ? 'neutral' : 'info'}>
                <Spinner size="sm" />
                {run.jobState}
              </Badge>
              <JobId value={run.jobId} />
              <div className="text-[11px] text-zinc-500">Polling job status every 2 seconds…</div>
            </div>
          )}

          {run.name === 'result' && (
            <ResultBody
              result={run.result}
              warnings={run.warnings}
              requestId={run.requestId}
              traces={run.traces}
              onPick={pickNode}
            />
          )}

          {run.name === 'error' && (
            <div className="space-y-3">
              {run.failure.kind === 'api' ? (
                <ApiErrorCallout error={run.failure.error} />
              ) : (
                <FailureCallout title="Job failed" message={run.failure.message} />
              )}
              {run.traces !== null && <TraceTimeline traces={run.traces} onPick={pickNode} />}
            </div>
          )}
        </>
      )}
    </Modal>
  );
}
