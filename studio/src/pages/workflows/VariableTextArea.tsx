/**
 * Textarea with Dify-style upstream-variable autocomplete. Typing `{{#`, or
 * `/` at the start of a line / after whitespace, opens a picker listing the
 * upstream output variables grouped by node; typing filters, ArrowUp/Down
 * navigate, Enter/Tab insert the full `{{#nodeId.variable#}}` reference
 * (replacing the trigger text, `/` included), Escape dismisses. Invalid
 * references in the value are listed as an amber warning under the field.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';

import { TextArea, cn } from '../../components';
import { getNodeDefinition } from '../../workflow/registry';
import { OPEN_OUTPUTS, validateVariableRefs } from './model/variables';
import type { AvailableVariable } from './model/variables';
import { useDismiss } from './shared';

export interface VariableTextAreaProps {
  value: string;
  /** Called with the next full text value (not the DOM event). */
  onChange: (value: string) => void;
  /** Upstream variables for the edited node (see model/variables.ts). */
  available: readonly AvailableVariable[];
  rows?: number;
  placeholder?: string;
  className?: string;
  spellCheck?: boolean;
}

interface Trigger {
  /** Index of the first character to replace (the `{` of `{{#`, or the `/`). */
  start: number;
  /** Filter text typed after the trigger, up to the caret. */
  query: string;
}

const QUERY_CHARS = /^[\w.-]*$/;

/** Detect an active autocomplete trigger for the text before the caret. */
function findTrigger(value: string, caret: number): Trigger | null {
  const before = value.slice(0, caret);
  let best: Trigger | null = null;
  const braceStart = before.lastIndexOf('{{#');
  if (braceStart !== -1) {
    const query = before.slice(braceStart + 3);
    if (QUERY_CHARS.test(query)) best = { start: braceStart, query };
  }
  const slashStart = before.lastIndexOf('/');
  if (slashStart !== -1 && (best === null || slashStart > best.start)) {
    const prev = slashStart === 0 ? '' : before[slashStart - 1]!;
    const query = before.slice(slashStart + 1);
    if ((prev === '' || /\s/.test(prev)) && QUERY_CHARS.test(query)) {
      best = { start: slashStart, query };
    }
  }
  return best;
}

interface PickerItem extends AvailableVariable {
  /** Reference token inside {{#...#}} — "nodeId.variable" (or "sys.*" as-is). */
  ref: string;
}

export function VariableTextArea({
  value,
  onChange,
  available,
  rows = 3,
  placeholder,
  className,
  spellCheck,
}: VariableTextAreaProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const pendingCaret = useRef<number | null>(null);
  const [caret, setCaret] = useState(0);
  const [active, setActive] = useState(0);
  /** Trigger start index dismissed via Escape/outside click (stays closed). */
  const [dismissedAt, setDismissedAt] = useState<number | null>(null);

  const trigger = useMemo(() => findTrigger(value, caret), [value, caret]);

  const items = useMemo((): PickerItem[] => {
    const query = trigger === null ? '' : trigger.query.toLowerCase();
    return available
      .filter((v) => v.variable !== OPEN_OUTPUTS)
      .map((v) => ({
        ...v,
        ref: v.variable.startsWith('sys.') ? v.variable : `${v.nodeId}.${v.variable}`,
      }))
      .filter(
        (v) =>
          query === '' ||
          v.ref.toLowerCase().includes(query) ||
          v.nodeTitle.toLowerCase().includes(query),
      );
  }, [available, trigger]);

  const open = trigger !== null && trigger.start !== dismissedAt && items.length > 0;
  const activeIndex = items.length > 0 ? Math.min(active, items.length - 1) : 0;

  const groups = useMemo(() => {
    const list: {
      nodeId: string;
      nodeTitle: string;
      nodeType: string;
      entries: { item: PickerItem; index: number }[];
    }[] = [];
    items.forEach((item, index) => {
      const last = list[list.length - 1];
      if (last !== undefined && last.nodeId === item.nodeId) {
        last.entries.push({ item, index });
      } else {
        list.push({
          nodeId: item.nodeId,
          nodeTitle: item.nodeTitle,
          nodeType: item.nodeType,
          entries: [{ item, index }],
        });
      }
    });
    return list;
  }, [items]);

  const invalid = useMemo(() => validateVariableRefs(value, available), [value, available]);

  useEffect(() => {
    setActive(0);
  }, [trigger?.start, trigger?.query]);

  useEffect(() => {
    if (trigger === null && dismissedAt !== null) setDismissedAt(null);
  }, [trigger, dismissedAt]);

  // Restore the caret after a programmatic insert (controlled value update).
  useEffect(() => {
    if (pendingCaret.current === null) return;
    const pos = pendingCaret.current;
    pendingCaret.current = null;
    const el = textareaRef.current;
    if (el !== null) {
      el.focus();
      el.setSelectionRange(pos, pos);
    }
    setCaret(pos);
  }, [value]);

  useEffect(() => {
    if (open) listRef.current?.querySelector('[data-active]')?.scrollIntoView({ block: 'nearest' });
  }, [open, activeIndex]);

  const wrapperRef = useDismiss(open, () => {
    setDismissedAt(trigger === null ? null : trigger.start);
  });

  const syncCaret = () => {
    const el = textareaRef.current;
    if (el !== null) setCaret(el.selectionStart);
  };

  const insert = (item: PickerItem) => {
    if (trigger === null) return;
    const token = `{{#${item.ref}#}}`;
    pendingCaret.current = trigger.start + token.length;
    onChange(value.slice(0, trigger.start) + token + value.slice(caret));
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!open || trigger === null) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, items.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault();
      const item = items[activeIndex];
      if (item !== undefined) insert(item);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      setDismissedAt(trigger.start);
    }
  };

  return (
    <div ref={wrapperRef} className="relative">
      <TextArea
        ref={textareaRef}
        value={value}
        rows={rows}
        placeholder={placeholder}
        spellCheck={spellCheck}
        className={className}
        onChange={(e) => {
          setCaret(e.target.selectionStart);
          onChange(e.target.value);
        }}
        onSelect={syncCaret}
        onKeyDown={onKeyDown}
      />
      {open && (
        <div
          ref={listRef}
          className="absolute top-full left-0 z-30 mt-1 max-h-56 w-full overflow-y-auto rounded-lg border border-white/10 bg-zinc-900 p-1 shadow-2xl shadow-black/50"
        >
          {groups.map((group) => (
            <div key={group.nodeId}>
              <div className="flex items-center gap-1.5 px-2 pt-1.5 pb-0.5">
                <span
                  className="h-1.5 w-1.5 shrink-0 rounded-full"
                  style={{ backgroundColor: getNodeDefinition(group.nodeType).accent }}
                />
                <span className="truncate text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
                  {group.nodeTitle}
                </span>
                <span className="truncate font-mono text-[9px] text-zinc-600">{group.nodeId}</span>
              </div>
              {group.entries.map(({ item, index }) => (
                <button
                  type="button"
                  key={index}
                  {...(index === activeIndex ? { 'data-active': true } : {})}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => insert(item)}
                  onMouseEnter={() => setActive(index)}
                  className={cn(
                    'flex w-full items-center gap-2 rounded-md px-2 py-1 text-left font-mono text-xs',
                    index === activeIndex
                      ? 'bg-indigo-400/15 text-zinc-100'
                      : 'text-zinc-300 hover:bg-white/[0.05]',
                  )}
                >
                  <span className="truncate">{item.variable}</span>
                  <span className="ml-auto shrink-0 font-sans text-[9px] text-zinc-600">
                    {item.nodeType}
                  </span>
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
      {invalid.length > 0 && (
        <div className="mt-1 space-y-0.5">
          {invalid.map((issue) => (
            <div key={issue.ref} className="text-[11px] leading-4 text-amber-300/90">
              <span className="font-mono">{issue.ref}</span>{' '}
              {issue.reason === 'unknown-node'
                ? 'node is not upstream'
                : 'variable not found on this node'}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
