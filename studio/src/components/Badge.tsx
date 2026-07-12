import type { ReactNode } from 'react';

import { cn } from './cn';

export type BadgeVariant = 'neutral' | 'accent' | 'success' | 'warn' | 'danger' | 'info';

const VARIANTS: Record<BadgeVariant, string> = {
  neutral: 'border-white/10 bg-white/5 text-zinc-400',
  accent: 'border-indigo-400/30 bg-indigo-400/10 text-indigo-300',
  success: 'border-emerald-400/30 bg-emerald-400/10 text-emerald-300',
  warn: 'border-amber-400/30 bg-amber-400/10 text-amber-300',
  danger: 'border-red-400/30 bg-red-400/10 text-red-300',
  info: 'border-sky-400/30 bg-sky-400/10 text-sky-300',
};

export interface BadgeProps {
  variant?: BadgeVariant;
  className?: string;
  title?: string;
  children: ReactNode;
}

export function Badge({ variant = 'neutral', className, title, children }: BadgeProps) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4',
        VARIANTS[variant],
        className,
      )}
    >
      {children}
    </span>
  );
}
