import { useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';

import { ApiError, ask } from '../api/client';
import type { AskResponse, AskSource, ToolStatusSummary } from '../api/types';
import {
  Badge,
  Button,
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconCopy,
  IconFileText,
  IconSend,
  IconSparkle,
  KeyHint,
  cn,
  useCopy,
} from '../components';
import type { BadgeVariant } from '../components';

const EXAMPLE_QUESTIONS = [
  'What was total revenue in FY2024?',
  'Summarize the key risks mentioned in the latest annual report.',
  'How did operating margin change year over year?',
];

const IS_MAC =
  typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform ?? '');

interface Exchange {
  id: number;
  question: string;
  state: 'pending' | 'done' | 'error';
  response?: AskResponse;
  error?: ApiError;
}

function answerKindVariant(kind: AskResponse['answer_kind']): BadgeVariant {
  switch (kind) {
    case 'normal':
      return 'success';
    case 'clarification':
      return 'warn';
    case 'refusal':
      return 'danger';
  }
}

function CopyableId({ value }: { value: string }) {
  const [copied, copy] = useCopy();
  return (
    <button
      type="button"
      onClick={() => copy(value)}
      title="Copy request id"
      className="inline-flex items-center gap-1 rounded px-1 py-0.5 font-mono text-[10px] text-zinc-600 transition-colors hover:bg-white/5 hover:text-zinc-400"
    >
      {copied ? <IconCheck size={10} className="text-emerald-400" /> : <IconCopy size={10} />}
      {value}
    </button>
  );
}

function SourceCard({ source }: { source: AskSource }) {
  const extras = Object.entries(source).filter(
    ([key, value]) => key !== 'doc' && key !== 'locator' && value !== null && value !== undefined,
  );
  return (
    <div className="rounded-md border border-white/10 bg-zinc-900/60 px-3 py-2">
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-zinc-200">
          <IconFileText size={12} className="text-indigo-400" />
          {source.doc ?? 'Unknown document'}
        </span>
        {source.locator && (
          <span className="font-mono text-[11px] text-zinc-500">{source.locator}</span>
        )}
      </div>
      {extras.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {extras.map(([key, value]) => (
            <span
              key={key}
              className="rounded border border-white/5 bg-white/[0.03] px-1.5 py-0.5 font-mono text-[10px] text-zinc-500"
            >
              {key}={typeof value === 'object' ? JSON.stringify(value) : String(value)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function ToolStatusChips({ summary }: { summary: ToolStatusSummary }) {
  const chips: Array<{ label: string; count: number; variant: BadgeVariant }> = [
    { label: 'found', count: summary.found ?? 0, variant: 'success' },
    { label: 'not found', count: summary.not_found ?? 0, variant: 'warn' },
    { label: 'unrecognized', count: summary.unrecognized ?? 0, variant: 'danger' },
  ];
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-[10px] tracking-wider text-zinc-600 uppercase">Tool status</span>
      {chips.map((chip) => (
        <Badge key={chip.label} variant={chip.count > 0 ? chip.variant : 'neutral'}>
          {chip.label} {chip.count}
        </Badge>
      ))}
    </div>
  );
}

function PendingDots() {
  return (
    <div className="flex items-center gap-1.5 px-1 py-2" aria-label="Waiting for answer">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="h-1.5 w-1.5 animate-pulse rounded-full bg-indigo-400/70"
          style={{ animationDelay: `${i * 200}ms` }}
        />
      ))}
    </div>
  );
}

function ResponseCard({
  response,
  onFollowUp,
}: {
  response: AskResponse;
  onFollowUp: (question: string) => void;
}) {
  const sources = response.sources ?? [];
  return (
    <div className="space-y-3 rounded-lg border border-white/10 bg-zinc-900/40 px-4 py-3">
      <div className="text-sm leading-6 whitespace-pre-wrap text-zinc-200">{response.answer}</div>

      {response.answer_kind === 'clarification' && response.clarification && (
        <div className="space-y-2 rounded-md border border-amber-400/20 bg-amber-400/[0.04] px-3 py-2.5">
          {response.clarification.question && (
            <div className="text-xs text-amber-200">{response.clarification.question}</div>
          )}
          {(response.clarification.narrowing_options?.length ?? 0) > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {response.clarification.narrowing_options?.map((option) => (
                <button
                  key={option}
                  type="button"
                  onClick={() => onFollowUp(option)}
                  className="rounded-full border border-amber-400/30 bg-amber-400/10 px-2.5 py-1 text-[11px] text-amber-200 transition-colors hover:bg-amber-400/20"
                >
                  {option}
                </button>
              ))}
            </div>
          )}
          {response.clarification.assumption_note && (
            <div className="text-[11px] text-zinc-500 italic">
              {response.clarification.assumption_note}
            </div>
          )}
        </div>
      )}

      {sources.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-[10px] font-semibold tracking-[0.14em] text-zinc-500 uppercase">
            Sources ({sources.length})
          </div>
          <div className="space-y-1.5">
            {sources.map((source, i) => (
              <SourceCard key={i} source={source} />
            ))}
          </div>
        </div>
      )}

      {response.tool_status_summary && <ToolStatusChips summary={response.tool_status_summary} />}

      <div className="flex flex-wrap items-center gap-1.5 border-t border-white/5 pt-2.5">
        {response.route && <Badge variant="info">route: {response.route}</Badge>}
        <Badge variant={answerKindVariant(response.answer_kind)}>{response.answer_kind}</Badge>
        {response.cache?.hit && (
          <Badge variant="accent">cache: {response.cache.type ?? 'hit'}</Badge>
        )}
        <span className="ml-auto">
          <CopyableId value={response.request_id} />
        </span>
      </div>
    </div>
  );
}

export function PlaygroundPage() {
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [question, setQuestion] = useState('');
  const [referenceDate, setReferenceDate] = useState('');
  const [showDate, setShowDate] = useState(false);
  const [busy, setBusy] = useState(false);

  const nextId = useRef(1);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [question]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [exchanges]);

  const submit = useCallback(
    (raw: string) => {
      const q = raw.trim();
      if (!q || busy) return;
      const id = nextId.current++;
      setBusy(true);
      setQuestion('');
      setExchanges((prev) => [...prev, { id, question: q, state: 'pending' }]);

      const req = referenceDate ? { question: q, reference_date: referenceDate } : { question: q };
      ask(req)
        .then((response) => {
          setExchanges((prev) =>
            prev.map((ex) => (ex.id === id ? { ...ex, state: 'done' as const, response } : ex)),
          );
        })
        .catch((err: unknown) => {
          const error =
            err instanceof ApiError
              ? err
              : new ApiError(err instanceof Error ? err.message : 'Unknown error', 0, 'unknown');
          setExchanges((prev) =>
            prev.map((ex) => (ex.id === id ? { ...ex, state: 'error' as const, error } : ex)),
          );
        })
        .finally(() => setBusy(false));
    },
    [busy, referenceDate],
  );

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit(question);
    }
  };

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col">
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
        {exchanges.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-white/10 bg-white/5 text-indigo-400">
              <IconSparkle size={18} />
            </div>
            <div className="text-sm font-medium text-zinc-300">Ask the RAG engine</div>
            <div className="max-w-sm text-xs leading-5 text-zinc-500">
              Answers are grounded in ingested documents and always carry source provenance.
            </div>
            <div className="mt-1 flex max-w-md flex-wrap justify-center gap-1.5">
              {EXAMPLE_QUESTIONS.map((example) => (
                <button
                  key={example}
                  type="button"
                  onClick={() => submit(example)}
                  className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 text-xs text-zinc-400 transition-colors hover:border-indigo-400/40 hover:text-zinc-200"
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-5">
            {exchanges.map((ex) => (
              <div key={ex.id} className="space-y-2">
                <div className="flex justify-end">
                  <div className="max-w-[85%] rounded-lg bg-indigo-500/15 px-3.5 py-2 text-sm whitespace-pre-wrap text-zinc-200">
                    {ex.question}
                  </div>
                </div>
                {ex.state === 'pending' && <PendingDots />}
                {ex.state === 'done' && ex.response && (
                  <ResponseCard response={ex.response} onFollowUp={submit} />
                )}
                {ex.state === 'error' && ex.error && (
                  <div className="rounded-lg border border-red-400/20 bg-red-400/[0.05] px-4 py-3">
                    <div className="text-sm text-red-300">{ex.error.message}</div>
                    <div className="mt-1.5 flex items-center gap-2 font-mono text-[10px] text-zinc-500">
                      <span>{ex.error.type}</span>
                      {ex.error.status > 0 && <span>HTTP {ex.error.status}</span>}
                      {ex.error.requestId && <span>{ex.error.requestId}</span>}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="shrink-0 border-t border-white/10 px-6 py-4">
        <div className="rounded-lg border border-white/10 bg-zinc-900/60 transition-colors focus-within:border-indigo-400/50">
          <textarea
            ref={textareaRef}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            placeholder="Ask a question about your documents…"
            className="max-h-[200px] w-full resize-none bg-transparent px-3.5 py-2.5 text-sm text-zinc-200 placeholder:text-zinc-600 focus:outline-none"
          />
          <div className="flex items-center justify-between gap-3 px-2.5 pb-2">
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setShowDate((v) => !v)}
                className="inline-flex items-center gap-1 rounded px-1.5 py-1 text-[11px] text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-300"
              >
                {showDate ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
                Reference date{referenceDate && !showDate ? `: ${referenceDate}` : ''}
              </button>
              {showDate && (
                <input
                  type="date"
                  value={referenceDate}
                  onChange={(e) => setReferenceDate(e.target.value)}
                  className={cn(
                    'h-7 rounded-md border border-white/10 bg-zinc-900 px-2 text-xs text-zinc-300',
                    'focus:border-indigo-400/60 focus:outline-none [color-scheme:dark]',
                  )}
                />
              )}
            </div>
            <div className="flex items-center gap-2.5">
              <KeyHint keys={IS_MAC ? ['⌘', 'Enter'] : ['Ctrl', 'Enter']} />
              <Button
                variant="primary"
                size="sm"
                loading={busy}
                disabled={!question.trim()}
                onClick={() => submit(question)}
              >
                {!busy && <IconSend size={13} />}
                Ask
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
