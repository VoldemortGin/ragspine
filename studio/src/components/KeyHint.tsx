import { cn } from './cn';

export interface KeyHintProps {
  keys: string[];
  className?: string;
}

/** Row of kbd chips, e.g. <KeyHint keys={['Ctrl', 'Enter']} />. */
export function KeyHint({ keys, className }: KeyHintProps) {
  return (
    <span className={cn('inline-flex items-center gap-1', className)}>
      {keys.map((k, i) => (
        <kbd
          key={i}
          className="inline-flex h-4.5 min-w-4.5 items-center justify-center rounded border border-white/15 bg-white/5 px-1 font-mono text-[10px] leading-none text-zinc-400"
        >
          {k}
        </kbd>
      ))}
    </span>
  );
}
