/** Right-hand property panel for the selected node (per-type form) or edge. */

import { useMemo, useState } from 'react';

import type { NodeTrace } from '../../api/types';
import {
  Badge,
  Button,
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconCopy,
  IconTrash,
  IconX,
  JsonView,
  TextInput,
  useCopy,
} from '../../components';
import type { StudioFlowEdge, StudioFlowNode } from '../../workflow/reactflow';
import { getNodeDefinition } from '../../workflow/registry';
import { getNodeForm } from './forms';
import { TRACE_BADGE_VARIANT, formatElapsedMs } from './model/execution';
import { availableVariables } from './model/variables';
import { TypeChip } from './nodeIcons';
import { useEditorStore } from './store';

const NODE_IDS_DATALIST = 'workflow-editor-node-ids';

function CopyableNodeId({ id }: { id: string }) {
  const [copied, copy] = useCopy();
  return (
    <button
      type="button"
      onClick={() => copy(id)}
      title="Copy node id"
      className="inline-flex max-w-full items-center gap-1 rounded px-1 py-0.5 font-mono text-[10px] text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-300"
    >
      {copied ? <IconCheck size={10} className="text-emerald-400" /> : <IconCopy size={10} />}
      <span className="truncate">{id}</span>
    </button>
  );
}

/** Collapsed-by-default summary of the node's trace from the last run. */
function LastRunSection({ trace }: { trace: NodeTrace }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-3 rounded-md border border-white/10 bg-white/[0.02]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-2.5 py-2 text-left"
      >
        {open ? (
          <IconChevronDown size={12} className="shrink-0 text-zinc-500" />
        ) : (
          <IconChevronRight size={12} className="shrink-0 text-zinc-500" />
        )}
        <span className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
          Last run
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          <Badge variant={TRACE_BADGE_VARIANT[trace.status]}>{trace.status}</Badge>
          {trace.status !== 'skipped' && (
            <span className="font-mono text-[10px] text-zinc-500">
              {formatElapsedMs(trace.elapsed_ms)}
            </span>
          )}
        </span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-white/5 px-2.5 py-2">
          {trace.error !== null && (
            <div className="rounded border border-red-400/25 bg-red-400/[0.06] px-2 py-1.5 text-[11px] leading-4 whitespace-pre-wrap text-red-300">
              {trace.error}
            </div>
          )}
          <div>
            <div className="mb-1 text-[10px] tracking-wider text-zinc-600 uppercase">Inputs</div>
            <JsonView value={trace.inputs} maxHeight="9rem" />
          </div>
          <div>
            <div className="mb-1 text-[10px] tracking-wider text-zinc-600 uppercase">Outputs</div>
            <JsonView value={trace.outputs} maxHeight="9rem" />
          </div>
        </div>
      )}
    </div>
  );
}

function NodePanel({ node }: { node: StudioFlowNode }) {
  const updateNodeData = useEditorStore((s) => s.updateNodeData);
  const deleteNodes = useEditorStore((s) => s.deleteNodes);
  const setPanelOpen = useEditorStore((s) => s.setPanelOpen);
  const nodes = useEditorStore((s) => s.nodes);
  const edges = useEditorStore((s) => s.edges);
  const trace = useEditorStore((s): NodeTrace | undefined => s.execution.traces[node.id]);

  const def = getNodeDefinition(node.data.dify.type);
  const Form = useMemo(() => getNodeForm(node.data.dify.type), [node.data.dify.type]);
  const title = typeof node.data.dify.title === 'string' ? node.data.dify.title : '';
  const otherIds = nodes.map((n) => n.id).filter((id) => id !== node.id);

  // NodePanel already re-renders on every nodes change (data edits included),
  // so recomputing here adds no extra subscription pressure.
  const available = useMemo(
    () =>
      availableVariables(
        nodes.map((n) => ({ id: n.id, parentId: n.parentId, data: n.data.dify })),
        edges,
        node.id,
      ),
    [nodes, edges, node.id],
  );
  const upstreamIds = useMemo(() => [...new Set(available.map((v) => v.nodeId))], [available]);

  return (
    <>
      <div className="shrink-0 border-b border-white/10 px-3 py-3">
        <div className="flex items-center gap-2">
          <TypeChip type={node.data.dify.type} />
          <div className="min-w-0 flex-1">
            <TextInput
              value={title}
              placeholder={def.label}
              onChange={(e) => updateNodeData(node.id, { ...node.data.dify, title: e.target.value })}
              className="h-7.5 text-xs font-medium"
              aria-label="Node title"
            />
          </div>
          <button
            type="button"
            onClick={() => setPanelOpen(false)}
            title="Close panel"
            className="shrink-0 rounded p-1 text-zinc-600 transition-colors hover:bg-white/5 hover:text-zinc-300"
          >
            <IconX size={14} />
          </button>
        </div>
        <div className="mt-2 flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="shrink-0 text-[10px] tracking-wider text-zinc-600 uppercase">
              {def.label}
            </span>
            <CopyableNodeId id={node.id} />
          </div>
          <Button
            variant="danger"
            size="sm"
            className="!h-6 !px-2 !text-[11px]"
            onClick={() => deleteNodes([node.id])}
          >
            <IconTrash size={11} />
            Delete
          </Button>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {trace !== undefined && <LastRunSection key={node.id} trace={trace} />}
        <datalist id={NODE_IDS_DATALIST}>
          {upstreamIds.map((id) => (
            <option key={id} value={id} />
          ))}
        </datalist>
        <Form
          nodeId={node.id}
          data={node.data.dify}
          nodeIds={otherIds}
          listId={NODE_IDS_DATALIST}
          available={available}
          onChange={(next) => updateNodeData(node.id, next)}
        />
      </div>
    </>
  );
}

function MultiNodePanel({ ids }: { ids: readonly string[] }) {
  const duplicateNodes = useEditorStore((s) => s.duplicateNodes);
  const deleteNodes = useEditorStore((s) => s.deleteNodes);
  const setPanelOpen = useEditorStore((s) => s.setPanelOpen);

  return (
    <>
      <div className="flex shrink-0 items-center justify-between border-b border-white/10 px-3 py-3">
        <span className="text-[10px] font-semibold tracking-[0.14em] text-zinc-500 uppercase">
          Selection
        </span>
        <button
          type="button"
          onClick={() => setPanelOpen(false)}
          title="Close panel"
          className="rounded p-1 text-zinc-600 transition-colors hover:bg-white/5 hover:text-zinc-300"
        >
          <IconX size={14} />
        </button>
      </div>
      <div className="space-y-3 px-3 py-3">
        <div className="rounded-md border border-white/10 bg-white/[0.02] px-3 py-2.5 text-xs font-medium text-zinc-200">
          {ids.length} nodes selected
        </div>
        <Button size="sm" className="w-full" onClick={() => duplicateNodes(ids)}>
          <IconCopy size={12} />
          Duplicate
        </Button>
        <Button variant="danger" size="sm" className="w-full" onClick={() => deleteNodes(ids)}>
          <IconTrash size={12} />
          Delete
        </Button>
      </div>
    </>
  );
}

function EdgePanel({ edge }: { edge: StudioFlowEdge }) {
  const deleteEdge = useEditorStore((s) => s.deleteEdge);
  const setPanelOpen = useEditorStore((s) => s.setPanelOpen);
  const nodes = useEditorStore((s) => s.nodes);
  const titleOf = (id: string): string => {
    const n = nodes.find((x) => x.id === id);
    const t = n?.data.dify.title;
    return typeof t === 'string' && t !== '' ? t : id;
  };

  return (
    <>
      <div className="flex shrink-0 items-center justify-between border-b border-white/10 px-3 py-3">
        <span className="text-[10px] font-semibold tracking-[0.14em] text-zinc-500 uppercase">
          Edge
        </span>
        <button
          type="button"
          onClick={() => setPanelOpen(false)}
          title="Close panel"
          className="rounded p-1 text-zinc-600 transition-colors hover:bg-white/5 hover:text-zinc-300"
        >
          <IconX size={14} />
        </button>
      </div>
      <div className="space-y-3 px-3 py-3">
        <div className="rounded-md border border-white/10 bg-white/[0.02] px-3 py-2.5 text-xs">
          <div className="flex items-center gap-1.5">
            <span className="truncate font-medium text-zinc-200">{titleOf(edge.source)}</span>
            <span className="shrink-0 text-zinc-600">-&gt;</span>
            <span className="truncate font-medium text-zinc-200">{titleOf(edge.target)}</span>
          </div>
          <div className="mt-1.5 font-mono text-[10px] text-zinc-500">
            {edge.source} [{edge.sourceHandle ?? 'source'}] -&gt; {edge.target}
          </div>
        </div>
        <Button variant="danger" size="sm" className="w-full" onClick={() => deleteEdge(edge.id)}>
          <IconTrash size={12} />
          Delete edge
        </Button>
      </div>
    </>
  );
}

export function PropertyPanel() {
  const selection = useEditorStore((s) => s.selection);
  const multiSelection = useEditorStore((s) => s.multiSelection);
  const panelOpen = useEditorStore((s) => s.panelOpen);
  const node = useEditorStore((s) =>
    s.selection?.kind === 'node' ? s.nodes.find((n) => n.id === s.selection?.id) : undefined,
  );
  const edge = useEditorStore((s) =>
    s.selection?.kind === 'edge' ? s.edges.find((e) => e.id === s.selection?.id) : undefined,
  );

  if (!panelOpen) return null;
  if (multiSelection.length > 1) {
    return (
      <aside className="flex w-88 shrink-0 flex-col border-l border-white/10">
        <MultiNodePanel ids={multiSelection} />
      </aside>
    );
  }
  if (selection === null) return null;
  const content =
    selection.kind === 'node' && node !== undefined ? (
      <NodePanel node={node} />
    ) : selection.kind === 'edge' && edge !== undefined ? (
      <EdgePanel edge={edge} />
    ) : null;
  if (content === null) return null;

  return <aside className="flex w-88 shrink-0 flex-col border-l border-white/10">{content}</aside>;
}
