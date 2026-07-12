/** Small shared editor pieces: severity meta, error callout, list editors,
 * value-selector input, misc formatting helpers. */

import { useEffect, useId, useRef, useState } from 'react';
import type { ReactNode, RefObject } from 'react';

import { ApiError } from '../../api/client';
import type { SuggestionSeverity } from '../../api/types';
import { IconPlus, IconTrash, TextInput, cn } from '../../components';
import type { BadgeVariant } from '../../components';
import { OPEN_OUTPUTS } from './model/variables';
import type { AvailableVariable } from './model/variables';

/** Drag payload type used between the palette and the canvas. */
export const NODE_TYPE_MIME = 'application/x-ragspine-node-type';

/** True when a keyboard event targets a text-editing element (hotkeys pass). */
export function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

/* ------------------------------ severity ------------------------------ */

export const SEVERITY_META: Record<
  SuggestionSeverity,
  { label: string; color: string; badge: BadgeVariant }
> = {
  high: { label: 'high', color: '#f87171', badge: 'danger' },
  medium: { label: 'medium', color: '#fbbf24', badge: 'warn' },
  low: { label: 'low', color: '#60a5fa', badge: 'info' },
  info: { label: 'info', color: '#a1a1aa', badge: 'neutral' },
};

/* ---------------------------- error callout --------------------------- */

export interface ApiErrorInfo {
  title: string;
  message: string;
  requestId?: string;
}

export function describeApiError(error: unknown): ApiErrorInfo {
  if (error instanceof ApiError) {
    if (error.status === 403 && error.type === 'dify.run_disabled') {
      return {
        title: 'Workflow execution is disabled',
        message:
          'Workflow execution is disabled on this server. Set RAGSPINE_DIFY_RUN_ENABLED=true to enable it.',
        ...(error.requestId !== undefined ? { requestId: error.requestId } : {}),
      };
    }
    if (error.type === 'network_error') {
      return {
        title: 'Server unreachable',
        message: 'Could not reach the RAGSpine server. Check that it is running, then retry.',
      };
    }
    const title =
      error.status === 400
        ? 'Compile/execution error'
        : error.status === 422
          ? 'Rejected by safety gate'
          : `Request failed (HTTP ${error.status})`;
    return {
      title,
      message: error.message,
      ...(error.requestId !== undefined ? { requestId: error.requestId } : {}),
    };
  }
  return {
    title: 'Unexpected error',
    message: error instanceof Error ? error.message : String(error),
  };
}

export function ApiErrorCallout({ error, className }: { error: unknown; className?: string }) {
  const info = describeApiError(error);
  return (
    <div
      className={cn('rounded-lg border border-red-400/25 bg-red-400/[0.06] px-4 py-3', className)}
    >
      <div className="text-sm font-medium text-red-300">{info.title}</div>
      <div className="mt-1 text-xs leading-5 text-zinc-400">{info.message}</div>
      {info.requestId !== undefined && (
        <div className="mt-1.5 font-mono text-[10px] text-zinc-600">request {info.requestId}</div>
      )}
    </div>
  );
}

/* ----------------------------- list editors --------------------------- */

/** Row shell with a trailing remove button, for list editors in forms. */
export function ListRow({
  onRemove,
  children,
  className,
}: {
  onRemove: () => void;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        'group/row flex items-start gap-1.5 rounded-md border border-white/5 bg-white/[0.02] p-2',
        className,
      )}
    >
      <div className="min-w-0 flex-1 space-y-1.5">{children}</div>
      <button
        type="button"
        onClick={onRemove}
        title="Remove"
        className="mt-0.5 shrink-0 rounded p-1 text-zinc-600 transition-colors hover:bg-red-400/10 hover:text-red-300"
      >
        <IconTrash size={13} />
      </button>
    </div>
  );
}

/** Dashed full-width "add row" button used at the bottom of list editors. */
export function AddButton({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex h-8 w-full items-center justify-center gap-1.5 rounded-md border border-dashed border-white/15 text-xs text-zinc-500 transition-colors hover:border-indigo-400/40 hover:text-zinc-300"
    >
      <IconPlus size={13} />
      {children}
    </button>
  );
}

/** Muted hint paragraph used inside property forms. */
export function FormHint({ children }: { children: ReactNode }) {
  return <p className="text-[11px] leading-4 text-zinc-500">{children}</p>;
}

/* -------------------------- value selector ---------------------------- */

/** Narrow an unknown data field to a Dify value selector (string[]). */
export function toValueSelector(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

export interface ValueSelectorInputProps {
  /** Raw data field (unknown shape tolerated). */
  value: unknown;
  /** Commits [nodeId, ...fieldParts] (or [] when fully cleared). */
  onChange: (selector: string[]) => void;
  /** Optional <datalist> id with upstream node ids. */
  listId?: string;
  /** Upstream variables; enables field suggestions for the chosen node. */
  available?: readonly AvailableVariable[];
  disabled?: boolean;
}

function normalizePath(raw: string): string {
  return raw
    .split('.')
    .map((s) => s.trim())
    .filter((s) => s !== '')
    .join('.');
}

/**
 * Two-part editor for Dify value selectors: node id + dot-separated field
 * path, stored as [nodeId, ...fieldParts].
 */
export function ValueSelectorInput({
  value,
  onChange,
  listId,
  available,
  disabled,
}: ValueSelectorInputProps) {
  const selector = toValueSelector(value);
  const canonicalNode = selector[0] ?? '';
  const canonicalPath = selector.slice(1).join('.');
  const fieldListId = useId();

  // Local drafts so intermediate text ("a.") is not normalized away mid-typing.
  const [nodeDraft, setNodeDraft] = useState(canonicalNode);
  const [pathDraft, setPathDraft] = useState(canonicalPath);
  useEffect(() => {
    setNodeDraft((prev) => (prev.trim() === canonicalNode ? prev : canonicalNode));
  }, [canonicalNode]);
  useEffect(() => {
    setPathDraft((prev) => (normalizePath(prev) === canonicalPath ? prev : canonicalPath));
  }, [canonicalPath]);

  const commit = (nodeRaw: string, pathRaw: string) => {
    const node = nodeRaw.trim();
    const parts = pathRaw
      .split('.')
      .map((s) => s.trim())
      .filter((s) => s !== '');
    onChange(node === '' && parts.length === 0 ? [] : [node, ...parts]);
  };

  // Field suggestions for the chosen node (sys.* live under the sys namespace,
  // not under the start node, so they are not offered as field paths here).
  const fieldOptions =
    available
      ?.filter(
        (v) =>
          v.nodeId === nodeDraft.trim() &&
          v.variable !== OPEN_OUTPUTS &&
          !v.variable.startsWith('sys.'),
      )
      .map((v) => v.variable) ?? [];

  return (
    <div className="flex items-center gap-1">
      {fieldOptions.length > 0 && (
        <datalist id={fieldListId}>
          {fieldOptions.map((v) => (
            <option key={v} value={v} />
          ))}
        </datalist>
      )}
      <TextInput
        value={nodeDraft}
        placeholder="node_id"
        disabled={disabled}
        {...(listId !== undefined ? { list: listId } : {})}
        onChange={(e) => {
          setNodeDraft(e.target.value);
          commit(e.target.value, pathDraft);
        }}
        className="h-7.5 flex-1 font-mono !text-xs"
      />
      <span className="shrink-0 text-xs text-zinc-600">/</span>
      <TextInput
        value={pathDraft}
        placeholder="field"
        disabled={disabled}
        {...(fieldOptions.length > 0 ? { list: fieldListId } : {})}
        onChange={(e) => {
          setPathDraft(e.target.value);
          commit(nodeDraft, e.target.value);
        }}
        className="h-7.5 flex-1 font-mono !text-xs"
      />
    </div>
  );
}

/* ------------------------------- misc --------------------------------- */

/** Compact relative timestamp for the workflow switcher ("5m ago"). */
export function formatUpdatedAt(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const delta = Date.now() - t;
  if (delta < 45_000) return 'just now';
  const minutes = Math.round(delta / 60_000);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 14) return `${days}d ago`;
  return new Date(t).toLocaleDateString();
}

/** Filename-safe slug of a workflow name. */
export function slugify(name: string): string {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return slug === '' ? 'workflow' : slug;
}

/** Close-on-outside-click / Escape for popover menus. */
export function useDismiss(open: boolean, onClose: () => void): RefObject<HTMLDivElement | null> {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onPointer = (e: MouseEvent) => {
      if (ref.current !== null && e.target instanceof Node && !ref.current.contains(e.target)) {
        onClose();
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('mousedown', onPointer);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', onPointer);
      window.removeEventListener('keydown', onKey);
    };
  }, [open, onClose]);
  return ref;
}

/** True while the element is actually rendered/visible (display:none aware). */
export function useElementVisible(ref: RefObject<HTMLElement | null>): boolean {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const el = ref.current;
    if (el === null || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver((entries) => {
      const entry = entries[entries.length - 1];
      if (entry !== undefined) setVisible(entry.isIntersecting);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return visible;
}
