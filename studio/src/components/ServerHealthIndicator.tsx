import { useEffect, useState } from 'react';

import { checkHealth } from '../api/client';
import type { HealthState } from '../api/types';
import { cn } from './cn';

const POLL_MS = 10_000;

type Level = 'checking' | 'online' | 'degraded' | 'offline';

function levelOf(state: HealthState | null): Level {
  if (state === null) return 'checking';
  if (state.healthy && state.ready) return 'online';
  if (state.healthy) return 'degraded';
  return 'offline';
}

const LEVEL_META: Record<Level, { label: string; dot: string; detail: string }> = {
  checking: { label: 'Checking', dot: 'bg-zinc-500', detail: 'Contacting the server…' },
  online: { label: 'Online', dot: 'bg-emerald-400', detail: 'Server is healthy and ready.' },
  degraded: {
    label: 'Degraded',
    dot: 'bg-amber-400',
    detail: 'Server is up but not ready to serve requests.',
  },
  offline: { label: 'Offline', dot: 'bg-red-400', detail: 'Server is unreachable.' },
};

export function ServerHealthIndicator() {
  const [state, setState] = useState<HealthState | null>(null);

  useEffect(() => {
    let active = true;
    const tick = async () => {
      const health = await checkHealth();
      if (active) setState(health);
    };
    void tick();
    const id = setInterval(() => void tick(), POLL_MS);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, []);

  const level = levelOf(state);
  const meta = LEVEL_META[level];

  return (
    <div className="group relative flex items-center gap-2">
      <span className="relative flex h-2 w-2">
        {level === 'checking' && (
          <span className={cn('absolute inline-flex h-full w-full animate-ping rounded-full opacity-60', meta.dot)} />
        )}
        <span className={cn('relative inline-flex h-2 w-2 rounded-full', meta.dot)} />
      </span>
      <span className="text-xs text-zinc-400">{meta.label}</span>

      <div className="pointer-events-none absolute top-full right-0 z-40 mt-2 w-56 rounded-lg border border-white/10 bg-zinc-900 p-3 opacity-0 shadow-xl shadow-black/40 transition-opacity group-hover:opacity-100">
        <div className="mb-1.5 text-xs font-medium text-zinc-200">{meta.detail}</div>
        <div className="space-y-1 font-mono text-[11px] text-zinc-500">
          <div className="flex justify-between">
            <span>/healthz</span>
            <span className={state?.healthy ? 'text-emerald-400' : 'text-red-400'}>
              {state === null ? '—' : state.healthy ? 'ok' : 'failing'}
            </span>
          </div>
          <div className="flex justify-between">
            <span>/readyz</span>
            <span className={state?.ready ? 'text-emerald-400' : 'text-amber-400'}>
              {state === null ? '—' : state.ready ? 'ready' : 'not ready'}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
