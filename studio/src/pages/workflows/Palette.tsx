/** Left node palette: the 12 registry types grouped by category, searchable,
 * draggable onto the canvas (click also adds at the viewport center). */

import { useMemo, useState } from 'react';
import type { DragEvent } from 'react';

import { IconChevronLeft, IconChevronRight, IconSearch, TextInput, cn } from '../../components';
import type { NodeTypeDefinition } from '../../workflow/types';
import { TypeChip } from './nodeIcons';
import { CATEGORY_LABELS, searchNodeGroups } from './nodeSearch';
import { NODE_TYPE_MIME } from './shared';
import { useEditorStore } from './store';

function PaletteItem({ def, onAdd }: { def: NodeTypeDefinition; onAdd: (type: string) => void }) {
  const onDragStart = (event: DragEvent<HTMLDivElement>) => {
    event.dataTransfer.setData(NODE_TYPE_MIME, def.type);
    event.dataTransfer.effectAllowed = 'move';
  };
  return (
    <div
      draggable
      role="button"
      tabIndex={0}
      onDragStart={onDragStart}
      onClick={() => onAdd(def.type)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onAdd(def.type);
        }
      }}
      title={`${def.label} — drag onto the canvas or click to add`}
      className={cn(
        'group flex cursor-grab items-center gap-2.5 rounded-md border border-transparent px-2 py-1.5',
        'transition-colors select-none hover:border-white/10 hover:bg-white/[0.04] active:cursor-grabbing',
        'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-indigo-400',
      )}
    >
      <TypeChip type={def.type} size="sm" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs font-medium text-zinc-300 group-hover:text-zinc-100">
          {def.label}
        </div>
        <div className="truncate text-[10px] leading-4 text-zinc-600">{def.description}</div>
      </div>
    </div>
  );
}

export function Palette({ onAdd }: { onAdd: (type: string) => void }) {
  const open = useEditorStore((s) => s.paletteOpen);
  const setOpen = useEditorStore((s) => s.setPaletteOpen);
  const [query, setQuery] = useState('');

  const groups = useMemo(() => searchNodeGroups(query), [query]);

  if (!open) {
    return (
      <div className="flex w-9 shrink-0 flex-col items-center border-r border-white/10 py-2">
        <button
          type="button"
          onClick={() => setOpen(true)}
          title="Show node palette"
          className="rounded p-1.5 text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-300"
        >
          <IconChevronRight size={14} />
        </button>
        <span className="mt-3 text-[10px] font-semibold tracking-[0.18em] text-zinc-600 uppercase [writing-mode:vertical-rl]">
          Nodes
        </span>
      </div>
    );
  }

  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-white/10">
      <div className="flex h-10 shrink-0 items-center justify-between border-b border-white/10 pr-1 pl-3">
        <span className="text-[10px] font-semibold tracking-[0.14em] text-zinc-500 uppercase">
          Nodes
        </span>
        <button
          type="button"
          onClick={() => setOpen(false)}
          title="Collapse palette"
          className="rounded p-1.5 text-zinc-600 transition-colors hover:bg-white/5 hover:text-zinc-300"
        >
          <IconChevronLeft size={14} />
        </button>
      </div>
      <div className="shrink-0 px-2 pt-2">
        <div className="relative">
          <IconSearch
            size={13}
            className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-zinc-600"
          />
          <TextInput
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search nodes…"
            className="h-7.5 !pl-8 text-xs"
          />
        </div>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-2 py-2.5">
        {groups.length === 0 && (
          <div className="px-2 py-6 text-center text-xs text-zinc-600">No nodes match.</div>
        )}
        {groups.map((group) => (
          <div key={group.category}>
            <div className="mb-1 px-2 text-[10px] font-semibold tracking-[0.14em] text-zinc-600 uppercase">
              {CATEGORY_LABELS[group.category]}
            </div>
            <div className="space-y-0.5">
              {group.defs.map((def) => (
                <PaletteItem key={def.type} def={def} onAdd={onAdd} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}
