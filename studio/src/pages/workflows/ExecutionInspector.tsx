/**
 * Persistent, execution-focused companion to the workflow canvas.
 *
 * The service currently returns node traces after a run finishes. This panel
 * therefore presents completed traces as an explicit replay; it never implies
 * that recorded steps are arriving live while a run is in progress.
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent, ReactNode } from 'react';

import type { NodeTrace, NodeTraceStatus } from '../../api/types';
import {
  Badge,
  Button,
  EmptyState,
  IconAlertTriangle,
  IconCheck,
  IconChevronLeft,
  IconChevronRight,
  IconClock,
  IconInbox,
  IconPlay,
  IconTrash,
  IconX,
  JsonView,
  Spinner,
  cn,
} from '../../components';
import type { BadgeVariant } from '../../components';
import { formatElapsedMs, orderedNodeTraces, summarizeNodeTraces } from './model/execution';
import type { RunHistoryEntry, RunSlice } from './model/execution';
import { describeApiError } from './shared';

const CURRENT_RUN_VALUE = '__current_run__';

type InspectorStatus = 'idle' | 'running' | 'succeeded' | 'failed';
type DetailTab = 'inputs' | 'outputs' | 'error';

interface InspectorView {
  status: InspectorStatus;
  result: unknown;
  warnings: string[];
  traces: NodeTrace[];
  inputs: Record<string, unknown> | null;
  errorTitle: string | null;
  errorMessage: string | null;
  requestId: string | null;
  timestamp: string | null;
  jobLabel: string | null;
  tracesSummarized: boolean;
}

export interface ExecutionInspectorProps {
  /** Mount the inspector beside (or over, on narrow screens) the canvas. */
  open: boolean;
  /** Current editor run lifecycle. */
  run: RunSlice;
  /** Most-recent-first local run history for the active workflow. */
  history: readonly RunHistoryEntry[];
  /** `null` views the current attempt; an ISO timestamp views that history entry. */
  selectedHistoryAt: string | null;
  /** Zero-based position in the sorted recorded trace sequence; `null` means the final step. */
  replayStep: number | null;
  /** Canvas node selection, used to choose the matching trace detail. */
  selectedNodeId: string | null;
  /** Stable identity of the in-memory current run, used to reset detail tabs. */
  currentRunKey: string | null;
  /** Whether this trace belongs to the workflow currently shown on canvas. */
  canvasProjectionAvailable: boolean;
  /** Why canvas projection is disabled for this run. */
  projectionWarning: string | null;
  /** On compact screens the inspector covers the canvas and behaves as a dialog. */
  modal: boolean;
  /** Controlled replay playback state. */
  isPlaying: boolean;
  onClose: () => void;
  onSelectHistory: (startedAt: string | null) => void;
  onSelectStep: (step: number) => void;
  onTogglePlayback: () => void;
  onClearHistory: () => void;
  className?: string;
}

const STATUS_META: Record<
  InspectorStatus,
  { label: string; badge: BadgeVariant; icon: ReactNode }
> = {
  idle: { label: 'No run', badge: 'neutral', icon: <IconInbox size={13} /> },
  running: { label: 'Running', badge: 'info', icon: <Spinner size="sm" /> },
  succeeded: { label: 'Succeeded', badge: 'success', icon: <IconCheck size={13} /> },
  failed: { label: 'Failed', badge: 'danger', icon: <IconX size={13} /> },
};

const TRACE_LABEL: Record<NodeTraceStatus, string> = {
  succeeded: 'Succeeded',
  failed: 'Failed',
  skipped: 'Skipped',
};

const TRACE_BADGE: Record<NodeTraceStatus, BadgeVariant> = {
  succeeded: 'success',
  failed: 'danger',
  skipped: 'neutral',
};

function viewCurrentRun(run: RunSlice): InspectorView {
  if (run.name === 'idle') {
    return {
      status: 'idle',
      result: null,
      warnings: [],
      traces: [],
      inputs: null,
      errorTitle: null,
      errorMessage: null,
      requestId: null,
      timestamp: null,
      jobLabel: null,
      tracesSummarized: false,
    };
  }
  if (run.name === 'running') {
    return {
      status: 'running',
      result: null,
      warnings: [],
      traces: [],
      inputs: null,
      errorTitle: null,
      errorMessage: null,
      requestId: null,
      timestamp: null,
      jobLabel: 'Executing workflow',
      tracesSummarized: false,
    };
  }
  if (run.name === 'job') {
    return {
      status: 'running',
      result: null,
      warnings: [],
      traces: [],
      inputs: null,
      errorTitle: null,
      errorMessage: null,
      requestId: null,
      timestamp: null,
      jobLabel: `Async job ${run.jobState} · ${run.jobId}`,
      tracesSummarized: false,
    };
  }
  if (run.name === 'result') {
    return {
      status: 'succeeded',
      result: run.result,
      warnings: run.warnings,
      traces: orderedNodeTraces(run.traces),
      inputs: null,
      errorTitle: null,
      errorMessage: null,
      requestId: run.requestId,
      timestamp: null,
      jobLabel: null,
      tracesSummarized: false,
    };
  }
  if (run.failure.kind === 'job') {
    return {
      status: 'failed',
      result: null,
      warnings: [],
      traces: orderedNodeTraces(run.traces),
      inputs: null,
      errorTitle: 'Async job failed',
      errorMessage: run.failure.message,
      requestId: null,
      timestamp: null,
      jobLabel: null,
      tracesSummarized: false,
    };
  }
  const described = describeApiError(run.failure.error);
  return {
    status: 'failed',
    result: null,
    warnings: [],
    traces: orderedNodeTraces(run.traces),
    inputs: null,
    errorTitle: described.title,
    errorMessage: described.message,
    requestId: described.requestId ?? null,
    timestamp: null,
    jobLabel: null,
    tracesSummarized: false,
  };
}

function viewHistoryEntry(entry: RunHistoryEntry): InspectorView {
  return {
    status: entry.status,
    result: entry.result,
    warnings: entry.warnings,
    traces: orderedNodeTraces(entry.node_traces),
    inputs: entry.inputs,
    errorTitle: entry.status === 'failed' ? 'Run failed' : null,
    errorMessage:
      entry.error ?? (entry.status === 'failed' ? 'No error message was recorded.' : null),
    requestId: null,
    timestamp: entry.at,
    jobLabel: null,
    tracesSummarized: entry.tracesSummarized === true,
  };
}

function formatTimestamp(value: string): string {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? value : new Date(parsed).toLocaleString();
}

function clampReplayStep(step: number | null, traceCount: number): number {
  if (traceCount === 0) return -1;
  if (step === null || !Number.isFinite(step)) return traceCount - 1;
  return Math.min(Math.max(Math.round(step), 0), traceCount - 1);
}

function latestTraceForNode(
  traces: readonly NodeTrace[],
  nodeId: string | null,
  replayStep: number,
): NodeTrace | null {
  if (replayStep < 0) return null;
  if (nodeId !== null) {
    for (let index = replayStep; index >= 0; index -= 1) {
      const trace = traces[index];
      if (trace?.node_id === nodeId) return trace;
    }
  }
  return traces[replayStep] ?? null;
}

function StatusGlyph({ status }: { status: NodeTraceStatus }) {
  if (status === 'succeeded') {
    return <IconCheck size={13} className="shrink-0 text-emerald-400" />;
  }
  if (status === 'failed') return <IconX size={13} className="shrink-0 text-red-400" />;
  return (
    <span aria-hidden="true" className="w-3.5 shrink-0 text-center text-xs text-zinc-400">
      —
    </span>
  );
}

function SummaryCard({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0 rounded-lg border border-white/10 bg-white/[0.025] px-3 py-2.5">
      <div className="text-[10px] font-semibold tracking-wider text-zinc-400 uppercase">{label}</div>
      <div className="mt-1 truncate text-sm font-semibold text-zinc-100">{value}</div>
    </div>
  );
}

function RunSummary({ view }: { view: InspectorView }) {
  const status = STATUS_META[view.status];
  const summary = summarizeNodeTraces(view.traces);

  return (
    <section aria-labelledby="execution-summary-heading" className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h3
          id="execution-summary-heading"
          className="text-[11px] font-semibold tracking-wider text-zinc-400 uppercase"
        >
          Run summary
        </h3>
        <div aria-live="polite" role="status">
          <Badge variant={status.badge}>
            {status.icon}
            {status.label}
          </Badge>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2">
        <SummaryCard label="Recorded steps" value={summary.total} />
        <SummaryCard
          label="Node time"
          value={summary.total === 0 ? '—' : formatElapsedMs(summary.elapsedMs)}
        />
        <SummaryCard
          label="Outcome"
          value={
            summary.total === 0 ? (
              '—'
            ) : (
              <span className="text-xs">
                {summary.succeeded} ok · {summary.failed} failed · {summary.skipped} skipped
              </span>
            )
          }
        />
      </div>
    </section>
  );
}

interface RunChooserProps {
  history: readonly RunHistoryEntry[];
  selectedHistoryAt: string | null;
  onSelectHistory: (startedAt: string | null) => void;
}

function RunChooser({ history, selectedHistoryAt, onSelectHistory }: RunChooserProps) {
  const id = useId();
  const selectedExists =
    selectedHistoryAt !== null && history.some((entry) => entry.at === selectedHistoryAt);
  const value = selectedExists && selectedHistoryAt !== null ? selectedHistoryAt : CURRENT_RUN_VALUE;
  const handleChange = useCallback(
    (event: ChangeEvent<HTMLSelectElement>) => {
      onSelectHistory(event.target.value === CURRENT_RUN_VALUE ? null : event.target.value);
    },
    [onSelectHistory],
  );

  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="text-[11px] font-medium text-zinc-300">
        Inspect run
      </label>
      <select
        id={id}
        value={value}
        onChange={handleChange}
        className={cn(
          'min-h-11 w-full rounded-md border border-white/10 bg-zinc-900 px-3 text-sm text-zinc-100',
          'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400',
        )}
      >
        <option value={CURRENT_RUN_VALUE}>Current attempt</option>
        {history.map((entry) => (
          <option key={entry.at} value={entry.at}>
            {formatTimestamp(entry.at)} · {entry.status}
          </option>
        ))}
      </select>
    </div>
  );
}

interface ReplayControlsProps {
  traces: readonly NodeTrace[];
  replayStep: number;
  isPlaying: boolean;
  onSelectStep: (step: number) => void;
  onTogglePlayback: () => void;
}

function ReplayControls({
  traces,
  replayStep,
  isPlaying,
  onSelectStep,
  onTogglePlayback,
}: ReplayControlsProps) {
  const rangeId = useId();
  const hasMultipleSteps = traces.length > 1;
  const previous = useCallback(() => {
    onSelectStep(Math.max(replayStep - 1, 0));
  }, [onSelectStep, replayStep]);
  const next = useCallback(() => {
    onSelectStep(Math.min(replayStep + 1, traces.length - 1));
  }, [onSelectStep, replayStep, traces.length]);
  const changeStep = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      onSelectStep(Number(event.target.value));
    },
    [onSelectStep],
  );

  return (
    <section
      aria-labelledby="execution-replay-heading"
      className="space-y-3 rounded-xl border border-indigo-400/20 bg-indigo-400/[0.045] p-3"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 id="execution-replay-heading" className="text-xs font-semibold text-zinc-100">
            Completed-run replay
          </h3>
          <p className="mt-1 text-[11px] leading-4 text-zinc-400">
            Recorded steps are replayed after completion; this is not a live event stream. Dashed
            canvas connectors are inferred from recorded endpoint nodes.
          </p>
        </div>
        <Badge variant="accent" className="shrink-0">
          Step {replayStep + 1}/{traces.length}
        </Badge>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={previous}
          disabled={replayStep <= 0}
          aria-label="Previous recorded step"
          className={cn(
            'flex h-11 w-11 shrink-0 items-center justify-center rounded-md border border-white/10 bg-white/5 text-zinc-200',
            'transition-colors hover:bg-white/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400',
            'disabled:cursor-not-allowed disabled:opacity-40',
          )}
        >
          <IconChevronLeft size={16} />
        </button>
        <button
          type="button"
          onClick={onTogglePlayback}
          disabled={!hasMultipleSteps}
          aria-label={isPlaying ? 'Pause recorded-step replay' : 'Play recorded-step replay'}
          aria-pressed={isPlaying}
          className={cn(
            'flex h-11 w-11 shrink-0 items-center justify-center rounded-md border border-indigo-400/30 bg-indigo-400/10 text-indigo-200',
            'transition-colors hover:bg-indigo-400/20 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400',
            'disabled:cursor-not-allowed disabled:opacity-40',
          )}
        >
          {isPlaying ? (
            <span aria-hidden="true" className="flex gap-1">
              <span className="h-4 w-1 rounded-sm bg-current" />
              <span className="h-4 w-1 rounded-sm bg-current" />
            </span>
          ) : (
            <IconPlay size={16} />
          )}
        </button>
        <button
          type="button"
          onClick={next}
          disabled={replayStep >= traces.length - 1}
          aria-label="Next recorded step"
          className={cn(
            'flex h-11 w-11 shrink-0 items-center justify-center rounded-md border border-white/10 bg-white/5 text-zinc-200',
            'transition-colors hover:bg-white/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400',
            'disabled:cursor-not-allowed disabled:opacity-40',
          )}
        >
          <IconChevronRight size={16} />
        </button>
        <div className="min-w-0 flex-1 px-1">
          <label htmlFor={rangeId} className="sr-only">
            Recorded replay step
          </label>
          <input
            id={rangeId}
            type="range"
            min={0}
            max={Math.max(traces.length - 1, 0)}
            step={1}
            value={replayStep}
            onChange={changeStep}
            className="h-11 w-full cursor-pointer accent-indigo-400"
          />
        </div>
      </div>
    </section>
  );
}

interface TraceRowProps {
  trace: NodeTrace;
  position: number;
  active: boolean;
  reached: boolean;
  onSelectStep: (step: number) => void;
}

function TraceRow({
  trace,
  position,
  active,
  reached,
  onSelectStep,
}: TraceRowProps) {
  const select = useCallback(() => {
    onSelectStep(position);
  }, [onSelectStep, position]);
  const title = trace.title.trim() === '' ? trace.node_id : trace.title;

  return (
    <li>
      <button
        type="button"
        onClick={select}
        aria-current={active ? 'step' : undefined}
        className={cn(
          'group flex min-h-11 w-full items-center gap-2.5 px-3 py-2 text-left',
          'transition-colors focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-indigo-400',
          active ? 'bg-indigo-400/10' : 'hover:bg-white/[0.045]',
          !reached && 'opacity-60',
        )}
      >
        <span
          className={cn(
            'flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-[10px] font-semibold',
            active
              ? 'border-indigo-300/50 bg-indigo-400/20 text-indigo-100'
              : 'border-white/10 bg-white/[0.025] text-zinc-400',
          )}
        >
          {position + 1}
        </span>
        <StatusGlyph status={trace.status} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs font-medium text-zinc-100">{title}</span>
          <span className="mt-0.5 block truncate text-[10px] text-zinc-400">
            {trace.node_type} · {TRACE_LABEL[trace.status]}
            {!reached ? ' · later in replay' : ''}
          </span>
        </span>
        <span className="shrink-0 font-mono text-[10px] text-zinc-400">
          {trace.status === 'skipped' ? '—' : formatElapsedMs(trace.elapsed_ms)}
        </span>
      </button>
    </li>
  );
}

interface TraceTimelineProps {
  traces: readonly NodeTrace[];
  replayStep: number;
  canvasProjectionAvailable: boolean;
  onSelectStep: (step: number) => void;
}

function TraceTimeline({
  traces,
  replayStep,
  canvasProjectionAvailable,
  onSelectStep,
}: TraceTimelineProps) {
  return (
    <section aria-labelledby="execution-timeline-heading" className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <h3
          id="execution-timeline-heading"
          className="text-[11px] font-semibold tracking-wider text-zinc-400 uppercase"
        >
          Node timeline
        </h3>
        <span className="text-[10px] text-zinc-400">
          {canvasProjectionAvailable
            ? 'Select a step to focus its node'
            : 'Select a step to inspect its trace'}
        </span>
      </div>
      <ol
        aria-label="Recorded node execution timeline"
        className="max-h-72 divide-y divide-white/5 overflow-y-auto rounded-lg border border-white/10 bg-zinc-900/60"
      >
        {traces.map((trace, position) => (
          <TraceRow
            key={`${trace.index}:${trace.node_id}:${trace.node_type}`}
            trace={trace}
            position={position}
            active={position === replayStep}
            reached={position <= replayStep}
            onSelectStep={onSelectStep}
          />
        ))}
      </ol>
    </section>
  );
}

interface DetailTabButtonProps {
  tab: DetailTab;
  label: string;
  selected: boolean;
  tabId: string;
  panelId: string;
  onSelect: (tab: DetailTab) => void;
}

function DetailTabButton({
  tab,
  label,
  selected,
  tabId,
  panelId,
  onSelect,
}: DetailTabButtonProps) {
  const select = useCallback(() => onSelect(tab), [onSelect, tab]);
  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLButtonElement>) => {
    if (
      event.key !== 'ArrowLeft' &&
      event.key !== 'ArrowRight' &&
      event.key !== 'Home' &&
      event.key !== 'End'
    ) {
      return;
    }
    const parent = event.currentTarget.parentElement;
    if (parent === null) return;
    const tabs = Array.from(parent.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
    const current = tabs.indexOf(event.currentTarget);
    if (current < 0 || tabs.length === 0) return;
    let next = current;
    if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    else if (event.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length;
    else next = (current + 1) % tabs.length;
    event.preventDefault();
    tabs[next]?.focus();
    tabs[next]?.click();
  }, []);
  return (
    <button
      id={tabId}
      type="button"
      role="tab"
      aria-selected={selected}
      aria-controls={panelId}
      tabIndex={selected ? 0 : -1}
      onClick={select}
      onKeyDown={handleKeyDown}
      className={cn(
        'min-h-11 flex-1 border-b-2 px-3 text-xs font-medium transition-colors',
        'focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-indigo-400',
        selected
          ? 'border-indigo-400 text-indigo-200'
          : 'border-transparent text-zinc-400 hover:text-zinc-200',
      )}
    >
      {label}
    </button>
  );
}

function EmptyPayload({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-white/10 px-3 py-6 text-center text-xs text-zinc-400">
      {children}
    </div>
  );
}

function TraceDetails({ trace }: { trace: NodeTrace }) {
  const [tab, setTab] = useState<DetailTab>(trace.error === null ? 'outputs' : 'error');
  const baseId = useId();
  const tabId = `${baseId}-${tab}-tab`;
  const panelId = `${baseId}-${tab}-panel`;
  const title = trace.title.trim() === '' ? trace.node_id : trace.title;

  let content: ReactNode;
  if (tab === 'inputs') {
    content =
      trace.inputs === null ? (
        <EmptyPayload>No input payload was recorded for this node.</EmptyPayload>
      ) : (
        <JsonView value={trace.inputs} maxHeight="18rem" />
      );
  } else if (tab === 'outputs') {
    content =
      trace.outputs === null ? (
        <EmptyPayload>No output payload was recorded for this node.</EmptyPayload>
      ) : (
        <JsonView value={trace.outputs} maxHeight="18rem" />
      );
  } else {
    content =
      trace.error === null || trace.error === '' ? (
        <EmptyPayload>No error was recorded for this node.</EmptyPayload>
      ) : (
        <div className="rounded-md border border-red-400/25 bg-red-400/[0.06] px-3 py-2.5 text-xs leading-5 whitespace-pre-wrap text-red-100">
          {trace.error}
        </div>
      );
  }

  return (
    <section aria-labelledby="execution-node-detail-heading" className="space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3
            id="execution-node-detail-heading"
            className="truncate text-sm font-semibold text-zinc-100"
          >
            {title}
          </h3>
          <p className="mt-0.5 truncate font-mono text-[10px] text-zinc-400">{trace.node_id}</p>
        </div>
        <Badge variant={TRACE_BADGE[trace.status]} className="shrink-0">
          <StatusGlyph status={trace.status} />
          {TRACE_LABEL[trace.status]}
        </Badge>
      </div>

      <div className="overflow-hidden rounded-lg border border-white/10 bg-zinc-900/50">
        <div role="tablist" aria-label="Recorded node payload" className="flex border-b border-white/10">
          <DetailTabButton
            tab="inputs"
            label="Inputs"
            selected={tab === 'inputs'}
            tabId={`${baseId}-inputs-tab`}
            panelId={`${baseId}-inputs-panel`}
            onSelect={setTab}
          />
          <DetailTabButton
            tab="outputs"
            label="Outputs"
            selected={tab === 'outputs'}
            tabId={`${baseId}-outputs-tab`}
            panelId={`${baseId}-outputs-panel`}
            onSelect={setTab}
          />
          <DetailTabButton
            tab="error"
            label="Error"
            selected={tab === 'error'}
            tabId={`${baseId}-error-tab`}
            panelId={`${baseId}-error-panel`}
            onSelect={setTab}
          />
        </div>
        <div
          id={panelId}
          role="tabpanel"
          aria-labelledby={tabId}
          tabIndex={0}
          className="p-3 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-indigo-400"
        >
          {content}
        </div>
      </div>
    </section>
  );
}

function RunFailureCallout({ title, message }: { title: string; message: string }) {
  return (
    <div role="alert" className="rounded-lg border border-red-400/25 bg-red-400/[0.06] px-3 py-3">
      <div className="flex items-center gap-2 text-sm font-semibold text-red-200">
        <IconAlertTriangle size={15} className="shrink-0" />
        {title}
      </div>
      <p className="mt-1.5 text-xs leading-5 whitespace-pre-wrap text-zinc-300">{message}</p>
    </div>
  );
}

function RunWarnings({ warnings }: { warnings: readonly string[] }) {
  if (warnings.length === 0) return null;
  const uniqueWarnings = [...new Set(warnings)];
  return (
    <section aria-labelledby="execution-warnings-heading" className="space-y-2">
      <h3
        id="execution-warnings-heading"
        className="text-[11px] font-semibold tracking-wider text-zinc-400 uppercase"
      >
        Warnings
      </h3>
      <ul className="space-y-1.5 rounded-lg border border-amber-400/20 bg-amber-400/[0.045] p-3">
        {uniqueWarnings.map((warning) => (
          <li key={warning} className="flex items-start gap-2 text-xs leading-5 text-amber-100">
            <IconAlertTriangle size={13} className="mt-1 shrink-0 text-amber-300" />
            <span>{warning}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function PayloadSection({ title, value }: { title: string; value: unknown }) {
  return (
    <section className="space-y-2">
      <h3 className="text-[11px] font-semibold tracking-wider text-zinc-400 uppercase">{title}</h3>
      {typeof value === 'string' ? (
        <div className="max-h-72 overflow-auto rounded-md border border-white/10 bg-zinc-900/70 px-3 py-2.5 text-sm leading-6 whitespace-pre-wrap text-zinc-200">
          {value}
        </div>
      ) : (
        <JsonView value={value} maxHeight="18rem" />
      )}
    </section>
  );
}

interface ClearHistoryControlProps {
  count: number;
  onClearHistory: () => void;
  onSelectHistory: (startedAt: string | null) => void;
}

function ClearHistoryControl({
  count,
  onClearHistory,
  onSelectHistory,
}: ClearHistoryControlProps) {
  const [confirming, setConfirming] = useState(false);
  const requestClear = useCallback(() => setConfirming(true), []);
  const cancelClear = useCallback(() => setConfirming(false), []);
  const confirmClear = useCallback(() => {
    onSelectHistory(null);
    onClearHistory();
    setConfirming(false);
  }, [onClearHistory, onSelectHistory]);

  if (count === 0) return null;
  if (!confirming) {
    return (
      <Button
        variant="ghost"
        size="sm"
        onClick={requestClear}
        className="min-h-11 text-zinc-300"
      >
        <IconTrash size={13} />
        Clear history
      </Button>
    );
  }
  return (
    <div className="rounded-lg border border-red-400/20 bg-red-400/[0.045] p-3">
      <p className="text-xs leading-5 text-zinc-200">Clear all {count} stored runs?</p>
      <p className="mt-0.5 text-[11px] leading-4 text-zinc-400">This cannot be undone.</p>
      <div className="mt-2 flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={cancelClear} className="min-h-11">
          Cancel
        </Button>
        <Button variant="danger" size="sm" onClick={confirmClear} className="min-h-11">
          Clear
        </Button>
      </div>
    </div>
  );
}

/**
 * Persistent execution inspector. Keep it mounted next to the canvas so node
 * selection and completed-run replay retain spatial context.
 */
export function ExecutionInspector({
  open,
  run,
  history,
  selectedHistoryAt,
  replayStep,
  selectedNodeId,
  currentRunKey,
  canvasProjectionAvailable,
  projectionWarning,
  modal,
  isPlaying,
  onClose,
  onSelectHistory,
  onSelectStep,
  onTogglePlayback,
  onClearHistory,
  className,
}: ExecutionInspectorProps) {
  const panelRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const selectedHistory = useMemo(
    () =>
      selectedHistoryAt === null
        ? null
        : (history.find((entry) => entry.at === selectedHistoryAt) ?? null),
    [history, selectedHistoryAt],
  );
  const view = useMemo(
    () => (selectedHistory === null ? viewCurrentRun(run) : viewHistoryEntry(selectedHistory)),
    [run, selectedHistory],
  );
  const activeStep = clampReplayStep(replayStep, view.traces.length);
  const selectedTrace = latestTraceForNode(
    view.traces,
    canvasProjectionAvailable ? selectedNodeId : null,
    activeStep,
  );

  useEffect(() => {
    if (!open) return;
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = window.requestAnimationFrame(() => panelRef.current?.focus());
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (!modal || event.key !== 'Tab' || panelRef.current === null) return;
      const focusable = Array.from(
        panelRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), select:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute('hidden'));
      if (focusable.length === 0) {
        event.preventDefault();
        panelRef.current.focus();
        return;
      }
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      const active = document.activeElement;
      if (!(active instanceof HTMLElement) || !focusable.includes(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener('keydown', onKeyDown);
      // In compact mode the rest of the app is inert until the parent effect
      // finishes cleaning up. Restore focus on the next task so the trigger is
      // focusable again instead of silently dropping the request.
      window.setTimeout(() => {
        if (previousFocus?.isConnected === true && previousFocus.closest('[inert]') === null) {
          previousFocus.focus();
        }
      }, 0);
    };
  }, [modal, open]);

  if (!open) return null;

  return (
    <aside
      ref={panelRef}
      role={modal ? 'dialog' : undefined}
      aria-modal={modal ? true : undefined}
      aria-label="Execution inspector"
      tabIndex={-1}
      className={cn(
        'flex h-full min-h-0 w-full shrink-0 flex-col border-t border-white/10 bg-zinc-950 shadow-2xl',
        'lg:w-96 lg:border-t-0 lg:border-l xl:w-[28rem]',
        className,
      )}
    >
      <header className="flex min-h-14 shrink-0 items-center gap-3 border-b border-white/10 px-4">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-indigo-400/20 bg-indigo-400/10 text-indigo-200">
          <IconClock size={16} />
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-zinc-50">Execution inspector</h2>
          <p className="truncate text-[11px] text-zinc-400">
            {selectedHistory === null
              ? 'Current attempt and recorded node data'
              : formatTimestamp(selectedHistory.at)}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close execution inspector"
          className={cn(
            'flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-zinc-300',
            'transition-colors hover:bg-white/5 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400',
          )}
        >
          <IconX size={17} />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <div className="space-y-5 p-4">
          <RunChooser
            history={history}
            selectedHistoryAt={selectedHistoryAt}
            onSelectHistory={onSelectHistory}
          />

          <RunSummary view={view} />

          {view.timestamp !== null && (
            <p className="text-[11px] text-zinc-400">Started {formatTimestamp(view.timestamp)}</p>
          )}

          {view.status === 'idle' && (
            <EmptyState
              icon={<IconInbox size={18} />}
              title="No execution to inspect"
              hint="Run the workflow from the toolbar, or choose a stored run above."
              className="rounded-xl border border-dashed border-white/10 py-10"
            />
          )}

          {view.status === 'running' && (
            <div
              role="status"
              aria-live="polite"
              className="flex flex-col items-center rounded-xl border border-sky-400/15 bg-sky-400/[0.035] px-5 py-10 text-center"
            >
              <Spinner />
              <div className="mt-3 text-sm font-medium text-zinc-100">
                {view.jobLabel ?? 'Running workflow'}
              </div>
              <p className="mt-1 max-w-xs text-xs leading-5 text-zinc-400">
                Node traces will appear here when the server returns the completed run.
              </p>
            </div>
          )}

          {view.errorTitle !== null && view.errorMessage !== null && (
            <RunFailureCallout title={view.errorTitle} message={view.errorMessage} />
          )}

          {view.tracesSummarized && (
            <div className="rounded-lg border border-amber-400/20 bg-amber-400/[0.04] px-3 py-2.5 text-xs leading-5 text-zinc-300">
              This stored run exceeded the browser storage budget. Its timeline is available, but node
              input and output payloads were removed.
            </div>
          )}

          {projectionWarning !== null && (
            <div
              role="status"
              className="rounded-lg border border-amber-400/20 bg-amber-400/[0.04] px-3 py-2.5 text-xs leading-5 text-zinc-300"
            >
              {projectionWarning}
            </div>
          )}

          {view.traces.length > 0 && (
            <>
              <ReplayControls
                traces={view.traces}
                replayStep={activeStep}
                isPlaying={isPlaying}
                onSelectStep={onSelectStep}
                onTogglePlayback={onTogglePlayback}
              />
              <TraceTimeline
                traces={view.traces}
                replayStep={activeStep}
                canvasProjectionAvailable={canvasProjectionAvailable}
                onSelectStep={onSelectStep}
              />
              {selectedTrace !== null && (
                <TraceDetails
                  key={`${selectedHistory?.at ?? currentRunKey ?? CURRENT_RUN_VALUE}:${selectedTrace.index}:${selectedTrace.node_id}`}
                  trace={selectedTrace}
                />
              )}
            </>
          )}

          {view.inputs !== null && <PayloadSection title="Run inputs" value={view.inputs} />}
          {view.status === 'succeeded' && <PayloadSection title="Final result" value={view.result} />}
          <RunWarnings warnings={view.warnings} />

          {view.requestId !== null && (
            <div className="font-mono text-[10px] text-zinc-400">request {view.requestId}</div>
          )}

          <ClearHistoryControl
            count={history.length}
            onClearHistory={onClearHistory}
            onSelectHistory={onSelectHistory}
          />
        </div>
      </div>
    </aside>
  );
}
