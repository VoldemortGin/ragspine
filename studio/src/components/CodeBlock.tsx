import { cn } from './cn';
import { IconCheck, IconCopy } from './icons';
import { useCopy } from './useCopy';

export interface CodeBlockProps {
  code: string;
  /** Shown as a small caption in the header (e.g. 'python', 'yaml'). */
  language?: string;
  lineNumbers?: boolean;
  /** CSS max-height for the scrollable code area. Default '20rem'. */
  maxHeight?: string;
  className?: string;
}

export function CodeBlock({
  code,
  language,
  lineNumbers = false,
  maxHeight = '20rem',
  className,
}: CodeBlockProps) {
  const [copied, copy] = useCopy();
  const lines = code.split('\n');

  return (
    <div
      className={cn(
        'group relative overflow-hidden rounded-lg border border-white/10 bg-zinc-900/70',
        className,
      )}
    >
      <div className="flex items-center justify-between border-b border-white/5 px-3 py-1.5">
        <span className="font-mono text-[10px] tracking-wider text-zinc-500 uppercase">
          {language ?? 'code'}
        </span>
        <button
          type="button"
          onClick={() => copy(code)}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-300"
        >
          {copied ? <IconCheck size={12} className="text-emerald-400" /> : <IconCopy size={12} />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <div className="overflow-auto" style={{ maxHeight }}>
        <pre className="px-3 py-2.5 font-mono text-xs leading-5 text-zinc-300">
          {lineNumbers ? (
            <code>
              {lines.map((line, i) => (
                <span key={i} className="block">
                  <span className="mr-3 inline-block w-6 text-right text-zinc-600 select-none">
                    {i + 1}
                  </span>
                  {line}
                  {'\n'}
                </span>
              ))}
            </code>
          ) : (
            <code>{code}</code>
          )}
        </pre>
      </div>
    </div>
  );
}
