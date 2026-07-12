import { cn } from './cn';

export interface JsonViewProps {
  value: unknown;
  className?: string;
  /** CSS max-height for the scrollable area. Default '16rem'. */
  maxHeight?: string;
}

function stringify(value: unknown): string {
  if (value === undefined) return 'undefined';
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

/** Pretty-printed JSON from an unknown value. */
export function JsonView({ value, className, maxHeight = '16rem' }: JsonViewProps) {
  return (
    <pre
      className={cn(
        'overflow-auto rounded-md border border-white/10 bg-zinc-900/70 px-3 py-2 font-mono text-xs leading-5 whitespace-pre-wrap text-zinc-300',
        className,
      )}
      style={{ maxHeight }}
    >
      {stringify(value)}
    </pre>
  );
}
