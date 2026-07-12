/** Floating node-type picker for quick-add gestures: dropping a connection on
 * empty canvas, double-clicking the pane, or pressing Tab. Same search logic
 * and item visuals as the palette (accent chip + label + description). */

import { useMemo, useState } from 'react';
import type { KeyboardEvent } from 'react';

import { IconSearch, TextInput, cn } from '../../components';
import type { NodeTypeDefinition } from '../../workflow/types';
import { TypeChip } from './nodeIcons';
import { CATEGORY_LABELS, searchNodeGroups } from './nodeSearch';
import { useDismiss } from './shared';

export interface NodePickerProps {
  /** Panel top-left, in canvas-wrapper coordinates. */
  position: { x: number; y: number };
  /** Optional type filter (e.g. only defs that can take an incoming edge). */
  include?: (def: NodeTypeDefinition) => boolean;
  onPick: (type: string) => void;
  onClose: () => void;
}

export function NodePicker({ position, include, onPick, onClose }: NodePickerProps) {
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const ref = useDismiss(true, onClose);

  const groups = useMemo(() => searchNodeGroups(query, include), [query, include]);
  const flat = useMemo(() => groups.flatMap((g) => g.defs), [groups]);
  const activeIndex = Math.min(active, Math.max(flat.length - 1, 0));

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive(Math.min(activeIndex + 1, flat.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive(Math.max(activeIndex - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const def = flat[activeIndex];
      if (def !== undefined) onPick(def.type);
    }
  };

  let index = -1;
  return (
    <div
      ref={ref}
      style={{ left: position.x, top: position.y }}
      className="absolute z-30 w-64 rounded-lg border border-white/10 bg-zinc-900 p-1.5 shadow-2xl shadow-black/50"
    >
      <div className="relative">
        <IconSearch
          size={13}
          className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-zinc-600"
        />
        <TextInput
          autoFocus
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
          placeholder="Search nodes…"
          className="h-7.5 !pl-8 text-xs"
        />
      </div>
      <div className="mt-1.5 max-h-72 space-y-2 overflow-y-auto">
        {groups.length === 0 && (
          <div className="px-2 py-6 text-center text-xs text-zinc-600">No nodes match.</div>
        )}
        {groups.map((group) => (
          <div key={group.category}>
            <div className="mb-1 px-2 text-[10px] font-semibold tracking-[0.14em] text-zinc-600 uppercase">
              {CATEGORY_LABELS[group.category]}
            </div>
            <div className="space-y-0.5">
              {group.defs.map((def) => {
                index += 1;
                const itemIndex = index;
                const isActive = itemIndex === activeIndex;
                return (
                  <button
                    key={def.type}
                    type="button"
                    onClick={() => onPick(def.type)}
                    onMouseEnter={() => setActive(itemIndex)}
                    ref={(el) => {
                      if (isActive) el?.scrollIntoView({ block: 'nearest' });
                    }}
                    className={cn(
                      'group flex w-full items-center gap-2.5 rounded-md border border-transparent px-2 py-1.5',
                      'text-left transition-colors select-none',
                      isActive && 'border-white/10 bg-white/[0.04]',
                    )}
                  >
                    <TypeChip type={def.type} size="sm" />
                    <div className="min-w-0 flex-1">
                      <div
                        className={cn(
                          'truncate text-xs font-medium text-zinc-300',
                          isActive && 'text-zinc-100',
                        )}
                      >
                        {def.label}
                      </div>
                      <div className="truncate text-[10px] leading-4 text-zinc-600">
                        {def.description}
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
