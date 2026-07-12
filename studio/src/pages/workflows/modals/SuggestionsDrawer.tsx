/** Bottom drawer listing analysis suggestions (hover to highlight, click to focus). */

import type { Suggestion, SuggestionSeverity } from '../../../api/types';
import { Badge, IconX, Spinner } from '../../../components';
import type { AnalysisSlice } from '../model/analysis';
import { ApiErrorCallout, SEVERITY_META } from '../shared';

export interface SuggestionsDrawerProps {
  slice: AnalysisSlice | null;
  onClose: () => void;
  onHighlight: (s: Suggestion | null) => void;
  onFocus: (s: Suggestion) => void;
}

const SEVERITY_ORDER: readonly SuggestionSeverity[] = ['high', 'medium', 'low', 'info'];

export function SuggestionsDrawer({ slice, onClose, onHighlight, onFocus }: SuggestionsDrawerProps) {
  if (slice === null) return null;

  return (
    <div className="flex h-60 flex-col border-t border-white/10 bg-zinc-950/95 backdrop-blur">
      <div className="flex h-10 shrink-0 items-center gap-2 border-b border-white/5 px-4">
        <span className="text-xs font-semibold tracking-wider text-zinc-300 uppercase">
          Suggestions
        </span>
        {slice.status === 'done' && (
          <HeaderMeta suggestions={slice.suggestions} requestId={slice.requestId} />
        )}
        <span className="flex-1" />
        <button
          type="button"
          onClick={onClose}
          aria-label="Close suggestions"
          className="rounded p-1 text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-300"
        >
          <IconX size={14} />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {slice.status === 'loading' && (
          <div className="flex h-full items-center justify-center gap-2 text-xs text-zinc-500">
            <Spinner size="sm" />
            Analyzing workflow…
          </div>
        )}
        {slice.status === 'error' && <ApiErrorCallout error={slice.error} className="m-4" />}
        {slice.status === 'done' &&
          (slice.suggestions.length === 0 ? (
            <div className="flex h-full items-center justify-center">
              <Badge variant="success">No suggestions — looks good.</Badge>
            </div>
          ) : (
            <div className="divide-y divide-white/5">
              {slice.suggestions.map((s, i) => (
                <SuggestionRow
                  key={`${s.rule_id}-${i}`}
                  suggestion={s}
                  onHighlight={onHighlight}
                  onFocus={onFocus}
                />
              ))}
            </div>
          ))}
      </div>
    </div>
  );
}

function HeaderMeta({ suggestions, requestId }: { suggestions: Suggestion[]; requestId: string }) {
  const counts = new Map<SuggestionSeverity, number>();
  for (const s of suggestions) counts.set(s.severity, (counts.get(s.severity) ?? 0) + 1);
  return (
    <>
      {SEVERITY_ORDER.map((severity) => {
        const count = counts.get(severity);
        if (count === undefined) return null;
        return (
          <Badge key={severity} variant={SEVERITY_META[severity].badge}>
            {count} {SEVERITY_META[severity].label}
          </Badge>
        );
      })}
      {suggestions.length > 0 && <Badge variant="neutral">{suggestions.length} total</Badge>}
      <span className="font-mono text-[10px] text-zinc-600">{requestId}</span>
    </>
  );
}

function SuggestionRow({
  suggestion,
  onHighlight,
  onFocus,
}: {
  suggestion: Suggestion;
  onHighlight: (s: Suggestion | null) => void;
  onFocus: (s: Suggestion) => void;
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      className="cursor-pointer space-y-1 px-4 py-2.5 transition-colors hover:bg-white/[0.03]"
      onMouseEnter={() => onHighlight(suggestion)}
      onMouseLeave={() => onHighlight(null)}
      onClick={() => onFocus(suggestion)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onFocus(suggestion);
        }
      }}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant={SEVERITY_META[suggestion.severity].badge}>
          {SEVERITY_META[suggestion.severity].label}
        </Badge>
        <span className="rounded border border-white/10 px-1.5 py-0.5 font-mono text-[10px] text-zinc-500">
          {suggestion.rule_id}
        </span>
        <span className="text-[10px] tracking-wider text-zinc-600 uppercase">
          {suggestion.category}
        </span>
      </div>
      <div className="text-xs font-medium text-zinc-200">{suggestion.title}</div>
      <div className="text-[11px] leading-4 text-zinc-500">{suggestion.detail}</div>
      {suggestion.node_ids.length > 0 && (
        <div className="flex flex-wrap gap-1 pt-0.5">
          {suggestion.node_ids.map((nodeId) => (
            <span
              key={nodeId}
              className="rounded border border-white/5 bg-white/[0.03] px-1.5 py-0.5 font-mono text-[10px] text-zinc-500"
            >
              {nodeId}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
