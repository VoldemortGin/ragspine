import { Background, BackgroundVariant, Controls, MiniMap, Panel, ReactFlow } from '@xyflow/react';
import type { Edge, Node } from '@xyflow/react';
import { useEffect, useState } from 'react';

import { ApiError, fetchTopology } from '../../api/client';
import type { TopologyScope } from '../../api/types';
import {
  Button,
  EmptyState,
  IconAlertTriangle,
  IconNetwork,
  IconRefresh,
  Spinner,
  cn,
} from '../../components';
import { layoutTopology } from './layout';
import type { TopologyFlowNode } from './layout';
import { domainTint } from './palette';
import { TopologyNodeView } from './TopologyNodeView';

const SCOPES: TopologyScope[] = ['agent', 'retriever', 'service'];

const nodeTypes = { topology: TopologyNodeView };

type Phase =
  | { kind: 'loading' }
  | {
      kind: 'ready';
      nodes: TopologyFlowNode[];
      edges: Edge[];
      title?: string;
      requestId?: string;
    }
  | { kind: 'unavailable' }
  | { kind: 'error'; message: string };

function miniMapNodeColor(node: Node): string {
  const domain = node.data['domain'];
  if (typeof domain === 'string' && domain) return `${domainTint(domain).accent}66`;
  return '#3f3f46';
}

function LegendSwatch({ kind }: { kind: string }) {
  const base = 'h-3.5 w-6 shrink-0 border border-white/25 bg-white/[0.04]';
  switch (kind) {
    case 'stage':
      return <span className={cn(base, 'rounded-[3px]')} />;
    case 'store':
      return <span className={cn(base, 'rounded-md')} style={{ borderTopWidth: 3, borderTopStyle: 'double' }} />;
    case 'external':
      return <span className={cn(base, 'rounded-[3px] border-dashed')} />;
    case 'gate':
      return (
        <span
          className="h-3.5 w-6 shrink-0 bg-white/25"
          style={{ clipPath: 'polygon(15% 0%, 85% 0%, 100% 50%, 85% 100%, 15% 100%, 0% 50%)' }}
        />
      );
    case 'channel':
      return <span className={cn(base, 'rounded-full')} />;
    default:
      return <span className={base} />;
  }
}

function LegendLine({ dash }: { dash?: string }) {
  return (
    <svg width="24" height="4" className="shrink-0" aria-hidden="true">
      <line
        x1="0"
        y1="2"
        x2="24"
        y2="2"
        stroke="rgba(255,255,255,0.45)"
        strokeWidth="1.5"
        strokeDasharray={dash}
      />
    </svg>
  );
}

function Legend({ title, requestId }: { title?: string; requestId?: string }) {
  return (
    <div className="w-44 rounded-lg border border-white/10 bg-zinc-900/90 p-3 shadow-xl shadow-black/30 backdrop-blur-sm">
      {(title || requestId) && (
        <div className="mb-2.5 border-b border-white/5 pb-2">
          {title && <div className="text-xs font-medium text-zinc-200">{title}</div>}
          {requestId && <div className="mt-0.5 font-mono text-[9px] text-zinc-600">{requestId}</div>}
        </div>
      )}
      <div className="space-y-1.5">
        {(['stage', 'store', 'external', 'gate', 'channel'] as const).map((kind) => (
          <div key={kind} className="flex items-center gap-2 text-[10px] text-zinc-500 capitalize">
            <LegendSwatch kind={kind} />
            {kind}
          </div>
        ))}
      </div>
      <div className="mt-2.5 space-y-1.5 border-t border-white/5 pt-2">
        <div className="flex items-center gap-2 text-[10px] text-zinc-500">
          <LegendLine /> flow
        </div>
        <div className="flex items-center gap-2 text-[10px] text-zinc-500">
          <LegendLine dash="7 5" /> conditional
        </div>
        <div className="flex items-center gap-2 text-[10px] text-zinc-500">
          <LegendLine dash="2 4" /> data
        </div>
      </div>
    </div>
  );
}

export function PipelinePage() {
  const [scope, setScope] = useState<TopologyScope>('agent');
  const [attempt, setAttempt] = useState(0);
  const [phase, setPhase] = useState<Phase>({ kind: 'loading' });

  useEffect(() => {
    let cancelled = false;
    setPhase({ kind: 'loading' });
    fetchTopology(scope)
      .then(({ requestId, graph }) => {
        if (cancelled) return;
        const { nodes, edges } = layoutTopology(graph);
        setPhase({ kind: 'ready', nodes, edges, title: graph.title, requestId });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setPhase({ kind: 'unavailable' });
        } else {
          setPhase({
            kind: 'error',
            message: err instanceof Error ? err.message : 'Failed to load topology.',
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [scope, attempt]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-white/10 px-4 py-2">
        <div className="inline-flex rounded-md border border-white/10 bg-zinc-900 p-0.5">
          {SCOPES.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setScope(s)}
              className={cn(
                'rounded-[5px] px-3 py-1 text-xs capitalize transition-colors',
                scope === s
                  ? 'bg-white/10 font-medium text-zinc-100'
                  : 'text-zinc-500 hover:text-zinc-300',
              )}
            >
              {s}
            </button>
          ))}
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setAttempt((n) => n + 1)}
          disabled={phase.kind === 'loading'}
          title="Reload topology"
        >
          <IconRefresh size={13} />
          Refresh
        </Button>
      </div>

      <div className="relative min-h-0 flex-1">
        {phase.kind === 'loading' && (
          <div className="flex h-full flex-col items-center justify-center gap-3">
            <Spinner />
            <span className="text-xs text-zinc-500">Loading topology…</span>
          </div>
        )}

        {phase.kind === 'unavailable' && (
          <div className="flex h-full items-center justify-center">
            <EmptyState
              icon={<IconNetwork size={18} />}
              title="Topology endpoint not available"
              hint="This view requires a newer server exposing GET /v1/topology."
            />
          </div>
        )}

        {phase.kind === 'error' && (
          <div className="flex h-full items-center justify-center">
            <EmptyState
              icon={<IconAlertTriangle size={18} />}
              title="Failed to load topology"
              hint={phase.message}
              action={
                <Button variant="secondary" size="sm" onClick={() => setAttempt((n) => n + 1)}>
                  <IconRefresh size={13} />
                  Retry
                </Button>
              }
            />
          </div>
        )}

        {phase.kind === 'ready' &&
          (phase.nodes.length === 0 ? (
            <div className="flex h-full items-center justify-center">
              <EmptyState
                icon={<IconNetwork size={18} />}
                title="Empty topology"
                hint={`The ${scope} scope returned no nodes.`}
              />
            </div>
          ) : (
            <ReactFlow
              key={`${scope}:${attempt}`}
              colorMode="dark"
              nodes={phase.nodes}
              edges={phase.edges}
              nodeTypes={nodeTypes}
              fitView
              fitViewOptions={{ padding: 0.15 }}
              minZoom={0.2}
              maxZoom={2}
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={false}
              panOnDrag
              zoomOnScroll
            >
              <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="rgba(255,255,255,0.08)" />
              <Controls showInteractive={false} position="top-right" />
              <MiniMap nodeColor={miniMapNodeColor} pannable zoomable />
              <Panel position="bottom-left">
                <Legend title={phase.title} requestId={phase.requestId} />
              </Panel>
            </ReactFlow>
          ))}
      </div>
    </div>
  );
}
