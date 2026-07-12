import { Handle, Position } from '@xyflow/react';
import type { NodeProps } from '@xyflow/react';
import type { CSSProperties } from 'react';

import { cn } from '../../components';
import type { TopologyFlowNode } from './layout';
import { domainTint } from './palette';

const HEX_CLIP = 'polygon(10% 0%, 90% 0%, 100% 50%, 90% 100%, 10% 100%, 0% 50%)';

const NEUTRAL_BORDER = 'rgba(255, 255, 255, 0.14)';
const NEUTRAL_BG = '#18181b';

function NodeContent({
  label,
  symbol,
  domain,
  accent,
}: {
  label: string;
  symbol?: string;
  domain?: string;
  accent?: string;
}) {
  return (
    <div className="flex h-full min-w-0 flex-col items-center justify-center px-3 text-center">
      <div className="w-full truncate text-xs font-medium text-zinc-200">{label}</div>
      {symbol && (
        <div className="w-full truncate font-mono text-[10px] text-zinc-500">{symbol}</div>
      )}
      {domain && (
        <div
          className="w-full truncate text-[9px] tracking-[0.12em] uppercase"
          style={{ color: accent ?? '#71717a' }}
        >
          {domain}
        </div>
      )}
    </div>
  );
}

export function TopologyNodeView({ data }: NodeProps<TopologyFlowNode>) {
  const tint = data.domain ? domainTint(data.domain) : undefined;
  const border = tint?.border ?? NEUTRAL_BORDER;
  const bg = tint ? undefined : NEUTRAL_BG;

  const handles = (
    <>
      <Handle type="target" position={Position.Left} isConnectable={false} style={{ opacity: 0 }} />
      <Handle
        type="source"
        position={Position.Right}
        isConnectable={false}
        style={{ opacity: 0 }}
      />
    </>
  );

  const content = (
    <NodeContent label={data.label} symbol={data.symbol} domain={data.domain} accent={tint?.accent} />
  );

  if (data.kind === 'gate') {
    // Hexagon: outer clipped div acts as the border, inset inner div as fill.
    return (
      <div className="relative h-full w-full">
        <div className="absolute inset-0" style={{ clipPath: HEX_CLIP, background: border }} />
        <div
          className="absolute inset-[1.5px]"
          style={{ clipPath: HEX_CLIP, background: '#18181b' }}
        />
        {tint && (
          <div
            className="absolute inset-[1.5px]"
            style={{ clipPath: HEX_CLIP, background: tint.bg }}
          />
        )}
        <div className="relative h-full w-full px-4">{content}</div>
        {handles}
      </div>
    );
  }

  const shapeClass: string = (() => {
    switch (data.kind) {
      case 'store':
        return 'rounded-xl border';
      case 'external':
        return 'rounded-md border border-dashed';
      case 'channel':
        return 'rounded-full border';
      case 'stage':
      default:
        return 'rounded-md border';
    }
  })();

  const style: CSSProperties = {
    borderColor: border,
    background: bg,
  };
  if (tint) {
    style.background = `linear-gradient(${tint.bg}, ${tint.bg}), ${NEUTRAL_BG}`;
  }
  if (data.kind === 'store') {
    // Cylinder hint: double top border.
    style.borderTopWidth = 3;
    style.borderTopStyle = 'double';
  }

  return (
    <div className={cn('h-full w-full', shapeClass)} style={style}>
      {content}
      {handles}
    </div>
  );
}
