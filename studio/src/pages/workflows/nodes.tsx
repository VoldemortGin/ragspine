/** Custom React Flow node components: the standard Dify node card and the
 * iteration container, both accent-tinted from the registry. */

import { Handle, NodeResizer, Position, useUpdateNodeInternals } from '@xyflow/react';
import type { NodeProps, NodeTypes } from '@xyflow/react';
import { memo, useEffect } from 'react';
import type { CSSProperties } from 'react';

import type { NodeTrace } from '../../api/types';
import { Badge, IconCheck, IconX, cn } from '../../components';
import { DIFY_ITERATION, DIFY_NODE } from '../../workflow/reactflow';
import type { StudioFlowNode } from '../../workflow/reactflow';
import { getNodeDefinition } from '../../workflow/registry';
import type { IterationNodeData } from '../../workflow/types';
import { formatElapsedMs } from './model/execution';
import { TypeChip } from './nodeIcons';
import { SEVERITY_META } from './shared';
import { useEditorStore } from './store';

const HANDLE_CLASS = '!h-2.5 !w-2.5 !rounded-full !border-2 !border-zinc-950';

/** Severity color when this node is in the active suggestion highlight set. */
function useHighlightColor(id: string): string | null {
  return useEditorStore((s) =>
    s.highlight !== null && s.highlight.nodeIds.includes(id)
      ? SEVERITY_META[s.highlight.severity].color
      : null,
  );
}

/** Execution overlay state for one node: pulse while running, trace after. */
function useNodeExecution(id: string): { pulse: boolean; trace: NodeTrace | undefined } {
  const pulse = useEditorStore((s) => s.execution.status === 'running');
  const trace = useEditorStore((s): NodeTrace | undefined => s.execution.traces[id]);
  return { pulse, trace };
}

function ringShadow(
  selected: boolean,
  accent: string,
  highlight: string | null,
  trace: NodeTrace | undefined,
): string | undefined {
  // Execution state outranks the analyze highlight.
  if (trace?.status === 'failed') return '0 0 0 2px #f87171, 0 0 18px 0 #f8717155';
  if (highlight !== null) return `0 0 0 2px ${highlight}, 0 0 18px 0 ${highlight}55`;
  if (selected) return `0 0 0 2px ${accent}`;
  return undefined;
}

/** Corner status badges after a run: green check + elapsed, or red cross. */
function TraceBadges({ trace }: { trace: NodeTrace }) {
  if (trace.status === 'succeeded') {
    return (
      <div className="pointer-events-none absolute -top-2 -right-1.5 z-10 flex items-center gap-1">
        <span className="rounded-full border border-emerald-400/30 bg-zinc-950 px-1.5 py-px font-mono text-[9px] leading-3.5 text-emerald-300">
          {formatElapsedMs(trace.elapsed_ms)}
        </span>
        <span className="flex h-4 w-4 items-center justify-center rounded-full bg-emerald-500 text-zinc-950">
          <IconCheck size={10} strokeWidth={2.5} />
        </span>
      </div>
    );
  }
  if (trace.status === 'failed') {
    return (
      <span className="pointer-events-none absolute -top-2 -right-1.5 z-10 flex h-4 w-4 items-center justify-center rounded-full bg-red-500 text-zinc-950">
        <IconX size={10} strokeWidth={2.5} />
      </span>
    );
  }
  return null;
}

/** Soft border pulse shown on every node while a run is in flight. */
function RunPulse({ rounded }: { rounded: string }) {
  return (
    <div
      className={cn(
        'pointer-events-none absolute -inset-px z-10 animate-pulse border-2 border-indigo-400/50',
        rounded,
      )}
    />
  );
}

function nodeTitle(title: unknown, fallback: string): string {
  return typeof title === 'string' && title.trim() !== '' ? title : fallback;
}

const DifyNodeView = memo(function DifyNodeView({ id, data, selected }: NodeProps<StudioFlowNode>) {
  const def = getNodeDefinition(data.dify.type);
  const handles = def.getSourceHandles(data.dify);
  const highlight = useHighlightColor(id);
  const { pulse, trace } = useNodeExecution(id);
  const single = handles.length === 1 && handles[0]!.id === 'source';

  const updateNodeInternals = useUpdateNodeInternals();
  const handleKey = handles.map((h) => h.id).join('|');
  useEffect(() => {
    updateNodeInternals(id);
  }, [id, handleKey, updateNodeInternals]);

  const sourceHandleStyle: CSSProperties = { backgroundColor: def.accent };

  return (
    <div
      className={cn(
        'relative w-60 rounded-lg border border-white/10 bg-zinc-900 transition-shadow',
        trace?.status === 'skipped' && 'opacity-40',
      )}
      style={{ boxShadow: ringShadow(selected, def.accent, highlight, trace) }}
    >
      {pulse && <RunPulse rounded="rounded-lg" />}
      {trace !== undefined && <TraceBadges trace={trace} />}
      {def.hasTargetHandle && (
        <Handle
          type="target"
          position={Position.Left}
          id="target"
          className={`${HANDLE_CLASS} !bg-zinc-500`}
        />
      )}

      <div className="flex items-center gap-2 px-3 pt-2.5">
        <TypeChip type={data.dify.type} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-semibold text-zinc-100">
            {nodeTitle(data.dify.title, def.label)}
          </div>
          <div className="truncate text-[10px] leading-3.5 text-zinc-500">{def.label}</div>
        </div>
      </div>
      <div className="truncate px-3 pt-1.5 pb-2.5 text-[11px] text-zinc-500">
        {def.summarize(data.dify)}
      </div>

      {single && (
        <Handle
          type="source"
          position={Position.Right}
          id="source"
          className={HANDLE_CLASS}
          style={sourceHandleStyle}
        />
      )}
      {!single && handles.length > 0 && (
        <div className="border-t border-white/5 py-1">
          {handles.map((h) => (
            <div key={h.id} className="relative flex h-6 items-center justify-end pr-3.5">
              <span
                className="max-w-[13rem] truncate text-[10px] font-semibold tracking-wide text-zinc-400 uppercase"
                title={h.id}
              >
                {h.label !== '' ? h.label : h.id}
              </span>
              <Handle
                type="source"
                position={Position.Right}
                id={h.id}
                className={HANDLE_CLASS}
                style={{
                  ...sourceHandleStyle,
                  top: '50%',
                  right: '-5px',
                  transform: 'translateY(-50%)',
                }}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
});

const IterationNodeView = memo(function IterationNodeView({
  id,
  data,
  selected,
}: NodeProps<StudioFlowNode>) {
  const def = getNodeDefinition(data.dify.type);
  const d = data.dify as IterationNodeData;
  const highlight = useHighlightColor(id);
  const { pulse, trace } = useNodeExecution(id);
  const updateContainerLayout = useEditorStore((s) => s.updateContainerLayout);

  return (
    <>
      <NodeResizer
        isVisible={selected}
        minWidth={300}
        minHeight={160}
        color={def.accent}
        onResizeEnd={(_event, params) => updateContainerLayout(id, params)}
      />
      <div
        className={cn(
          'relative flex h-full w-full flex-col rounded-xl border',
          trace?.status === 'skipped' && 'opacity-40',
        )}
        style={{
          borderColor: `${def.accent}55`,
          backgroundColor: `${def.accent}08`,
          boxShadow: ringShadow(selected, def.accent, highlight, trace),
        }}
      >
        {pulse && <RunPulse rounded="rounded-xl" />}
        {trace !== undefined && <TraceBadges trace={trace} />}
        <div
          className="flex h-9 shrink-0 items-center gap-2 rounded-t-xl px-3"
          style={{ backgroundColor: `${def.accent}14` }}
        >
          <TypeChip type={data.dify.type} size="sm" />
          <span className="truncate text-xs font-semibold text-zinc-100">
            {nodeTitle(data.dify.title, def.label)}
          </span>
          <span className="shrink-0 text-[10px] text-zinc-500">Iteration</span>
          {d.is_parallel === true && (
            <Badge variant="info" className="ml-auto shrink-0">
              parallel x{typeof d.parallel_nums === 'number' ? d.parallel_nums : 1}
            </Badge>
          )}
        </div>
        <div
          className="min-h-0 flex-1 rounded-b-xl border-t border-dashed"
          style={{ borderColor: `${def.accent}2e` }}
        />
      </div>
      <Handle
        type="target"
        position={Position.Left}
        id="target"
        className={`${HANDLE_CLASS} !bg-zinc-500`}
      />
      <Handle
        type="source"
        position={Position.Right}
        id="source"
        className={HANDLE_CLASS}
        style={{ backgroundColor: def.accent }}
      />
    </>
  );
});

export const EDITOR_NODE_TYPES: NodeTypes = {
  [DIFY_NODE]: DifyNodeView,
  [DIFY_ITERATION]: IterationNodeView,
};
