import type { ReactNode } from 'react';

import { cn } from './cn';

export interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  hint?: ReactNode;
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ icon, title, hint, action, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center gap-2 px-6 py-12 text-center',
        className,
      )}
    >
      {icon && (
        <div className="mb-1 flex h-10 w-10 items-center justify-center rounded-lg border border-white/10 bg-white/5 text-zinc-500">
          {icon}
        </div>
      )}
      <div className="text-sm font-medium text-zinc-300">{title}</div>
      {hint && <div className="max-w-sm text-xs leading-5 text-zinc-500">{hint}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
