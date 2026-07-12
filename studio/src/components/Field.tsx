import type { ReactNode } from 'react';

import { cn } from './cn';

export interface FieldProps {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  className?: string;
  children: ReactNode;
}

/** Label + control + hint + error wrapper for form inputs. */
export function Field({ label, hint, error, required, className, children }: FieldProps) {
  return (
    <label className={cn('block', className)}>
      <span className="mb-1.5 flex items-baseline gap-1 text-xs font-medium text-zinc-400">
        {label}
        {required && <span className="text-indigo-400">*</span>}
      </span>
      {children}
      {hint && !error && <span className="mt-1 block text-[11px] leading-4 text-zinc-500">{hint}</span>}
      {error && <span className="mt-1 block text-[11px] leading-4 text-red-400">{error}</span>}
    </label>
  );
}
