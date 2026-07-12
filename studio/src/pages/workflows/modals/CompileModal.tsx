/** Modal that compiles the current workflow YAML to Python and shows the result. */

import { useEffect, useRef, useState } from 'react';

import { compileWorkflow } from '../../../api/client';
import type { CompileResponse } from '../../../api/types';
import { Badge, CodeBlock, IconAlertTriangle, Modal, Spinner } from '../../../components';
import { ApiErrorCallout, SEVERITY_META } from '../shared';

export interface CompileModalProps {
  open: boolean;
  onClose: () => void;
  /** May throw on serialization failure. */
  getYaml: () => string;
  fold: boolean;
}

type CompileState =
  | { status: 'loading' }
  | { status: 'error'; error: unknown }
  | { status: 'done'; response: CompileResponse };

export function CompileModal({ open, onClose, getYaml, fold }: CompileModalProps) {
  const [state, setState] = useState<CompileState>({ status: 'loading' });
  const tokenRef = useRef(0);

  // Compile on every transition to open; the token ignores stale responses.
  useEffect(() => {
    tokenRef.current += 1;
    if (!open) return;
    const token = tokenRef.current;
    setState({ status: 'loading' });
    let yaml: string;
    try {
      yaml = getYaml();
    } catch (err) {
      setState({ status: 'error', error: err });
      return;
    }
    compileWorkflow(yaml, fold)
      .then((response) => {
        if (tokenRef.current === token) setState({ status: 'done', response });
      })
      .catch((err: unknown) => {
        if (tokenRef.current === token) setState({ status: 'error', error: err });
      });
    // Intentionally keyed on `open` only: getYaml/fold are read at open time.
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Modal open={open} onClose={onClose} title="Compile to Python" size="wide">
      {state.status === 'loading' && (
        <div className="flex items-center justify-center gap-2 py-16 text-sm text-zinc-500">
          <Spinner size="sm" />
          Compiling…
        </div>
      )}
      {state.status === 'error' && <ApiErrorCallout error={state.error} />}
      {state.status === 'done' && <CompileResult response={state.response} />}
    </Modal>
  );
}

function CompileResult({ response }: { response: CompileResponse }) {
  return (
    <div className="space-y-4">
      <CodeBlock code={response.code} language="python" lineNumbers maxHeight="22rem" />

      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[10px] tracking-wider text-zinc-600 uppercase">entrypoint</span>
        <span className="rounded border border-white/10 bg-white/[0.03] px-1.5 py-0.5 font-mono text-[11px] text-zinc-300">
          {response.entrypoint}
        </span>
        {response.imports.length > 0 && (
          <>
            <span className="ml-2 text-[10px] tracking-wider text-zinc-600 uppercase">imports</span>
            {response.imports.map((name) => (
              <span
                key={name}
                className="rounded border border-white/5 bg-white/[0.03] px-1.5 py-0.5 font-mono text-[10px] text-zinc-500"
              >
                {name}
              </span>
            ))}
          </>
        )}
      </div>

      {response.warnings.length > 0 && (
        <div className="space-y-1">
          {response.warnings.map((warning, i) => (
            <div key={i} className="flex items-start gap-1.5 text-xs leading-5 text-amber-200">
              <IconAlertTriangle size={13} className="mt-1 shrink-0 text-amber-400" />
              <span>{warning}</span>
            </div>
          ))}
        </div>
      )}

      {response.suggestions.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
            Suggestions ({response.suggestions.length})
          </div>
          {response.suggestions.map((s, i) => (
            <div key={`${s.rule_id}-${i}`} className="flex flex-wrap items-center gap-1.5">
              <Badge variant={SEVERITY_META[s.severity].badge}>
                {SEVERITY_META[s.severity].label}
              </Badge>
              <span className="rounded border border-white/10 px-1.5 py-0.5 font-mono text-[10px] text-zinc-500">
                {s.rule_id}
              </span>
              <span className="text-xs text-zinc-300">{s.title}</span>
            </div>
          ))}
        </div>
      )}

      <div className="font-mono text-[10px] text-zinc-600">request {response.request_id}</div>
    </div>
  );
}
