import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';

import { ApiError, getJob, submitNarrativeIngest, submitStructuredIngest } from '../api/client';
import type { JobState, NarrativeIngestRequest, StructuredIngestRequest } from '../api/types';
import {
  Badge,
  Button,
  Checkbox,
  EmptyState,
  Field,
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconCopy,
  IconInbox,
  IconRefresh,
  IconX,
  JsonView,
  Spinner,
  TextArea,
  TextInput,
  cn,
  useCopy,
} from '../components';
import type { BadgeVariant } from '../components';

const JOBS_STORAGE_KEY = 'ragspine-studio.jobs';
const POLL_MS = 2500;
const MAX_POLL_FAILURES = 5;

type JobKind = 'structured' | 'narrative';

interface TrackedJob {
  jobId: string;
  kind: JobKind;
  submittedAt: number;
}

interface LiveStatus {
  /** Last known status; undefined until the first successful poll. */
  status?: JobState;
  result?: unknown;
  error?: string | null;
  failures: number;
}

function isTrackedJob(value: unknown): value is TrackedJob {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v['jobId'] === 'string' &&
    (v['kind'] === 'structured' || v['kind'] === 'narrative') &&
    typeof v['submittedAt'] === 'number'
  );
}

function loadJobs(): TrackedJob[] {
  try {
    const raw = localStorage.getItem(JOBS_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isTrackedJob);
  } catch {
    return [];
  }
}

function saveJobs(jobs: TrackedJob[]): void {
  try {
    localStorage.setItem(JOBS_STORAGE_KEY, JSON.stringify(jobs));
  } catch {
    /* storage unavailable */
  }
}

function isTerminal(status: JobState): boolean {
  return status === 'finished' || status === 'failed';
}

const STATUS_VARIANT: Record<JobState, BadgeVariant> = {
  queued: 'neutral',
  started: 'info',
  finished: 'success',
  failed: 'danger',
};

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return `${err.message} (${err.type})`;
  if (err instanceof Error) return err.message;
  return 'Unknown error';
}

/* ------------------------------ Submit forms ---------------------------- */

function StructuredIngestForm({ onSubmitted }: { onSubmitted: (jobId: string) => void }) {
  const [file, setFile] = useState('');
  const [dryRun, setDryRun] = useState(false);
  const [validAsOf, setValidAsOf] = useState('');
  const [batchId, setBatchId] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const path = file.trim();
    if (!path || submitting) return;
    const req: StructuredIngestRequest = { file: path };
    if (dryRun) req.dry_run = true;
    if (validAsOf) req.valid_as_of = validAsOf;
    if (batchId.trim()) req.batch_id = batchId.trim();

    setSubmitting(true);
    setError(null);
    submitStructuredIngest(req)
      .then((ref) => {
        onSubmitted(ref.job_id);
        setFile('');
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setSubmitting(false));
  };

  return (
    <form onSubmit={onSubmit} className="space-y-3 rounded-lg border border-white/10 bg-zinc-900/40 p-4">
      <div>
        <div className="text-[10px] font-semibold tracking-[0.14em] text-zinc-500 uppercase">
          Structured ingest
        </div>
        <div className="mt-0.5 text-[11px] text-zinc-600">
          Tables and slides into the structured fact channel.
        </div>
      </div>
      <Field
        label="File"
        required
        hint="Path on the server, under the configured allowed_upload_root. Extensions: .xlsx .xlsm .pptx .pdf"
      >
        <TextInput
          value={file}
          onChange={(e) => setFile(e.target.value)}
          placeholder="reports/fy2024_results.xlsx"
          className="font-mono text-xs"
        />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Valid as of" hint="Optional fact validity date.">
          <TextInput type="date" value={validAsOf} onChange={(e) => setValidAsOf(e.target.value)} />
        </Field>
        <Field label="Batch id" hint="Optional grouping label.">
          <TextInput
            value={batchId}
            onChange={(e) => setBatchId(e.target.value)}
            placeholder="fy2024-q4"
            className="font-mono text-xs"
          />
        </Field>
      </div>
      <div className="flex items-center justify-between pt-1">
        <Checkbox
          label="Dry run"
          checked={dryRun}
          onChange={(e) => setDryRun(e.target.checked)}
        />
        <Button type="submit" variant="primary" size="sm" loading={submitting} disabled={!file.trim()}>
          Submit job
        </Button>
      </div>
      {error && (
        <div className="rounded-md border border-red-400/20 bg-red-400/[0.05] px-3 py-2 text-xs text-red-300">
          {error}
        </div>
      )}
    </form>
  );
}

function NarrativeIngestForm({ onSubmitted }: { onSubmitted: (jobId: string) => void }) {
  const [inputs, setInputs] = useState('');
  const [dryRun, setDryRun] = useState(false);
  const [metaJson, setMetaJson] = useState('');
  const [metaError, setMetaError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const paths = inputs
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
    if (paths.length === 0 || submitting) return;

    let meta: Record<string, unknown> | undefined;
    if (metaJson.trim()) {
      try {
        const parsed: unknown = JSON.parse(metaJson);
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
          setMetaError('Must be a JSON object mapping doc id to metadata.');
          return;
        }
        meta = parsed as Record<string, unknown>;
        setMetaError(null);
      } catch {
        setMetaError('Invalid JSON.');
        return;
      }
    } else {
      setMetaError(null);
    }

    const req: NarrativeIngestRequest = { inputs: paths.length === 1 ? paths[0]! : paths };
    if (dryRun) req.dry_run = true;
    if (meta) req.meta_by_doc = meta;

    setSubmitting(true);
    setError(null);
    submitNarrativeIngest(req)
      .then((ref) => {
        onSubmitted(ref.job_id);
        setInputs('');
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setSubmitting(false));
  };

  const hasInput = inputs.split('\n').some((line) => line.trim().length > 0);

  return (
    <form onSubmit={onSubmit} className="space-y-3 rounded-lg border border-white/10 bg-zinc-900/40 p-4">
      <div>
        <div className="text-[10px] font-semibold tracking-[0.14em] text-zinc-500 uppercase">
          Narrative ingest
        </div>
        <div className="mt-0.5 text-[11px] text-zinc-600">
          Prose documents into the narrative RAG channel.
        </div>
      </div>
      <Field label="Inputs" required hint="One server-side path per line.">
        <TextArea
          value={inputs}
          onChange={(e) => setInputs(e.target.value)}
          rows={3}
          placeholder={'reports/annual_report_2024.pdf\nnotes/strategy_memo.txt'}
          className="font-mono text-xs"
        />
      </Field>
      <Field
        label="Metadata by doc"
        hint='Optional JSON object, e.g. {"doc-1": {"sensitivity": "INTERNAL"}}.'
        error={metaError}
      >
        <TextArea
          value={metaJson}
          onChange={(e) => {
            setMetaJson(e.target.value);
            if (metaError) setMetaError(null);
          }}
          rows={2}
          placeholder="{ }"
          className={cn('font-mono text-xs', metaError && 'border-red-400/50')}
        />
      </Field>
      <div className="flex items-center justify-between pt-1">
        <Checkbox label="Dry run" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
        <Button type="submit" variant="primary" size="sm" loading={submitting} disabled={!hasInput}>
          Submit job
        </Button>
      </div>
      {error && (
        <div className="rounded-md border border-red-400/20 bg-red-400/[0.05] px-3 py-2 text-xs text-red-300">
          {error}
        </div>
      )}
    </form>
  );
}

/* -------------------------------- Job card ------------------------------ */

function JobIdChip({ jobId }: { jobId: string }) {
  const [copied, copy] = useCopy();
  return (
    <button
      type="button"
      onClick={() => copy(jobId)}
      title="Copy job id"
      className="inline-flex items-center gap-1 rounded px-1 py-0.5 font-mono text-[11px] text-zinc-400 transition-colors hover:bg-white/5 hover:text-zinc-200"
    >
      {copied ? <IconCheck size={11} className="text-emerald-400" /> : <IconCopy size={11} />}
      {jobId}
    </button>
  );
}

function JobCard({
  job,
  live,
  onRemove,
  onRetryPolling,
}: {
  job: TrackedJob;
  live: LiveStatus | undefined;
  onRemove: () => void;
  onRetryPolling: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const unreachable = (live?.failures ?? 0) >= MAX_POLL_FAILURES && !live?.status;
  const status = live?.status;
  const hasDetail = live !== undefined && (live.result !== undefined || Boolean(live.error));

  return (
    <div className="rounded-lg border border-white/10 bg-zinc-900/40">
      <div className="flex items-center gap-2.5 px-3.5 py-2.5">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          disabled={!hasDetail}
          className={cn(
            'shrink-0 rounded p-0.5 text-zinc-500 transition-colors',
            hasDetail ? 'hover:bg-white/5 hover:text-zinc-300' : 'opacity-30',
          )}
          aria-label={expanded ? 'Collapse' : 'Expand'}
        >
          {expanded ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
        </button>
        <Badge variant={job.kind === 'structured' ? 'accent' : 'info'}>{job.kind}</Badge>
        <JobIdChip jobId={job.jobId} />
        <span className="hidden text-[11px] text-zinc-600 sm:inline">
          {new Date(job.submittedAt).toLocaleString()}
        </span>
        <span className="ml-auto flex items-center gap-2">
          {status ? (
            <Badge variant={STATUS_VARIANT[status]}>
              {status === 'started' && <Spinner size="sm" className="h-2.5 w-2.5 text-sky-300" />}
              {status}
            </Badge>
          ) : unreachable ? (
            <>
              <Badge variant="warn">unreachable</Badge>
              <button
                type="button"
                onClick={onRetryPolling}
                title="Retry polling"
                className="rounded p-1 text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-300"
              >
                <IconRefresh size={12} />
              </button>
            </>
          ) : (
            <Badge variant="neutral">
              <Spinner size="sm" className="h-2.5 w-2.5" />
              checking
            </Badge>
          )}
          <button
            type="button"
            onClick={onRemove}
            title="Remove from list"
            className="rounded p-1 text-zinc-600 transition-colors hover:bg-white/5 hover:text-zinc-300"
          >
            <IconX size={13} />
          </button>
        </span>
      </div>
      {expanded && hasDetail && (
        <div className="space-y-2 border-t border-white/5 px-3.5 py-3">
          {live.error && (
            <div className="rounded-md border border-red-400/20 bg-red-400/[0.05] px-3 py-2 font-mono text-xs whitespace-pre-wrap text-red-300">
              {live.error}
            </div>
          )}
          {live.result !== undefined && <JsonView value={live.result} />}
        </div>
      )}
    </div>
  );
}

/* --------------------------------- Page --------------------------------- */

export function JobsPage() {
  const [jobs, setJobs] = useState<TrackedJob[]>(loadJobs);
  const [live, setLive] = useState<Record<string, LiveStatus>>({});
  const liveRef = useRef(live);
  liveRef.current = live;

  useEffect(() => {
    saveJobs(jobs);
  }, [jobs]);

  const addJob = useCallback((jobId: string, kind: JobKind) => {
    setJobs((prev) => [{ jobId, kind, submittedAt: Date.now() }, ...prev]);
  }, []);

  useEffect(() => {
    let active = true;

    const poll = async () => {
      const current = liveRef.current;
      const pending = jobs.filter((job) => {
        const status = current[job.jobId];
        if (!status) return true;
        if (status.status && isTerminal(status.status)) return false;
        return status.failures < MAX_POLL_FAILURES;
      });
      if (pending.length === 0) return;

      const updates = await Promise.all(
        pending.map(async (job): Promise<[string, LiveStatus]> => {
          try {
            const s = await getJob(job.jobId);
            return [job.jobId, { status: s.status, result: s.result, error: s.error, failures: 0 }];
          } catch {
            const prev = liveRef.current[job.jobId];
            return [job.jobId, { ...prev, failures: (prev?.failures ?? 0) + 1 }];
          }
        }),
      );
      if (!active) return;
      setLive((prev) => {
        const next = { ...prev };
        for (const [id, status] of updates) next[id] = status;
        return next;
      });
    };

    void poll();
    const id = setInterval(() => void poll(), POLL_MS);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [jobs]);

  const removeJob = useCallback((jobId: string) => {
    setJobs((prev) => prev.filter((j) => j.jobId !== jobId));
  }, []);

  const retryPolling = useCallback((jobId: string) => {
    setLive((prev) => {
      const entry = prev[jobId];
      if (!entry) return prev;
      return { ...prev, [jobId]: { ...entry, failures: 0 } };
    });
  }, []);

  const clearFinished = useCallback(() => {
    setJobs((prev) =>
      prev.filter((j) => {
        const status = live[j.jobId]?.status;
        return !(status && isTerminal(status));
      }),
    );
  }, [live]);

  const hasFinished = jobs.some((j) => {
    const status = live[j.jobId]?.status;
    return status && isTerminal(status);
  });

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl space-y-6 px-6 py-6">
        <div className="grid gap-4 lg:grid-cols-2">
          <StructuredIngestForm onSubmitted={(id) => addJob(id, 'structured')} />
          <NarrativeIngestForm onSubmitted={(id) => addJob(id, 'narrative')} />
        </div>

        <div className="space-y-2.5">
          <div className="flex items-center justify-between">
            <div className="text-[10px] font-semibold tracking-[0.14em] text-zinc-500 uppercase">
              Tracked jobs {jobs.length > 0 && `(${jobs.length})`}
            </div>
            {hasFinished && (
              <Button variant="ghost" size="sm" onClick={clearFinished}>
                Clear finished
              </Button>
            )}
          </div>
          {jobs.length === 0 ? (
            <div className="rounded-lg border border-dashed border-white/10">
              <EmptyState
                icon={<IconInbox size={18} />}
                title="No tracked jobs"
                hint="Submit an ingestion job above; its status will be tracked here and survives page reloads."
              />
            </div>
          ) : (
            <div className="space-y-2">
              {jobs.map((job) => (
                <JobCard
                  key={job.jobId}
                  job={job}
                  live={live[job.jobId]}
                  onRemove={() => removeJob(job.jobId)}
                  onRetryPolling={() => retryPolling(job.jobId)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
