import type {
  InputHTMLAttributes,
  ReactNode,
  Ref,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react';

import { cn } from './cn';
import { IconCheck, IconChevronDown } from './icons';

const CONTROL =
  'w-full rounded-md border border-white/10 bg-zinc-900 px-2.5 text-sm text-zinc-200 ' +
  'placeholder:text-zinc-600 transition-colors ' +
  'hover:border-white/20 focus:border-indigo-400/60 focus:outline-none ' +
  'focus:ring-2 focus:ring-indigo-400/20 disabled:cursor-not-allowed disabled:opacity-50 ' +
  '[color-scheme:dark]';

export type TextInputProps = InputHTMLAttributes<HTMLInputElement>;

export function TextInput({ className, ...rest }: TextInputProps) {
  return <input className={cn(CONTROL, 'h-8.5', className)} {...rest} />;
}

export type TextAreaProps = TextareaHTMLAttributes<HTMLTextAreaElement> & {
  /** React 19 ref-as-prop, forwarded to the underlying textarea. */
  ref?: Ref<HTMLTextAreaElement>;
};

export function TextArea({ className, rows = 3, ...rest }: TextAreaProps) {
  return <textarea rows={rows} className={cn(CONTROL, 'py-2 leading-5', className)} {...rest} />;
}

export type SelectProps = SelectHTMLAttributes<HTMLSelectElement>;

export function Select({ className, children, ...rest }: SelectProps) {
  return (
    <span className="relative block">
      <select className={cn(CONTROL, 'h-8.5 appearance-none pr-8', className)} {...rest}>
        {children}
      </select>
      <IconChevronDown
        size={14}
        className="pointer-events-none absolute top-1/2 right-2.5 -translate-y-1/2 text-zinc-500"
      />
    </span>
  );
}

export interface CheckboxProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'children'> {
  label?: ReactNode;
}

export function Checkbox({ label, className, ...rest }: CheckboxProps) {
  return (
    <label
      className={cn(
        'inline-flex cursor-pointer items-center gap-2 text-sm text-zinc-300 select-none',
        rest.disabled && 'cursor-not-allowed opacity-50',
        className,
      )}
    >
      <span className="relative inline-flex h-4 w-4 shrink-0">
        <input type="checkbox" className="peer h-4 w-4 appearance-none rounded border border-white/20 bg-zinc-900 transition-colors checked:border-indigo-500 checked:bg-indigo-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400" {...rest} />
        <IconCheck
          size={11}
          strokeWidth={2.5}
          className="pointer-events-none absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 text-white opacity-0 transition-opacity peer-checked:opacity-100"
        />
      </span>
      {label && <span>{label}</span>}
    </label>
  );
}
