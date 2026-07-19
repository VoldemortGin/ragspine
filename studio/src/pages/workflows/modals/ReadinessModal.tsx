/** On-demand readiness preflight for the current workflow. */

import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';

import { checkWorkflowReadiness } from '../../../api/client';
import type { WorkflowReadinessResponse } from '../../../api/types';
import {
  Badge,
  Button,
  IconAlertTriangle,
  Modal,
  Spinner,
} from '../../../components';
import {
  readinessCheckRows,
  readinessStatusBadge,
} from '../model/readiness';
import type { WorkflowDeploymentReadiness } from '../model/templates';
import { ApiErrorCallout } from '../shared';

export interface ReadinessModalProps {
  open: boolean;
  onClose: () => void;
  getYaml: () => string;
  fallback: WorkflowDeploymentReadiness | null;
}

type ReadinessState =
  | { status: 'loading' }
  | { status: 'error'; error: unknown }
  | { status: 'done'; response: WorkflowReadinessResponse };

export function ReadinessModal({
  open,
  onClose,
  getYaml,
  fallback,
}: ReadinessModalProps) {
  const [state, setState] = useState<ReadinessState>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    let active = true;
    setState({ status: 'loading' });
    let yaml: string;
    try {
      yaml = getYaml();
    } catch (error) {
      setState({ status: 'error', error });
      return () => controller.abort();
    }
    void checkWorkflowReadiness(yaml, controller.signal)
      .then((response) => {
        if (active) setState({ status: 'done', response });
      })
      .catch((error: unknown) => {
        if (active && !controller.signal.aborted) setState({ status: 'error', error });
      });
    return () => {
      active = false;
      controller.abort();
    };
    // Read the current YAML only when opened or explicitly retried.
  }, [attempt, open]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Modal open={open} onClose={onClose} title="Workflow readiness" size="lg">
      {state.status === 'loading' && (
        <div className="flex items-center justify-center gap-2 py-16 text-sm text-zinc-500">
          <Spinner size="sm" />
          Checking current workflow…
        </div>
      )}
      {state.status === 'error' && (
        <div className="space-y-3">
          <ApiErrorCallout error={state.error} />
          {fallback !== null && (
            <div className="flex items-center gap-2 rounded-lg border border-white/10 bg-white/[0.02] px-3 py-2 text-xs text-zinc-400">
              <span>Saved scaffold status:</span>
              <Badge
                variant={
                  fallback.kind === 'ready'
                    ? 'success'
                    : fallback.kind === 'blocked'
                      ? 'danger'
                      : 'warn'
                }
                title={fallback.detail}
              >
                {fallback.label}
              </Badge>
            </div>
          )}
          <Button variant="secondary" size="sm" onClick={() => setAttempt((value) => value + 1)}>
            Retry
          </Button>
        </div>
      )}
      {state.status === 'done' && <ReadinessResult response={state.response} />}
    </Modal>
  );
}

function ReadinessResult({ response }: { response: WorkflowReadinessResponse }) {
  const status = readinessStatusBadge(response.status);
  return (
    <div className="space-y-5">
      <div className="flex items-center gap-2">
        <Badge variant={status.badge}>{status.label}</Badge>
        <span className="text-xs text-zinc-500">Current workflow preflight</span>
      </div>

      <div className="grid gap-2 sm:grid-cols-3">
        {readinessCheckRows(response).map((check) => (
          <div
            key={check.key}
            className="rounded-lg border border-white/10 bg-white/[0.02] px-3 py-2"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-zinc-300">{check.label}</span>
              <Badge variant={check.badge}>{check.status}</Badge>
            </div>
            {check.code !== null && (
              <div className="mt-1 font-mono text-[10px] text-zinc-500">{check.code}</div>
            )}
          </div>
        ))}
      </div>

      {response.start_inputs.length > 0 && (
        <ReadinessList title="Start inputs">
          {response.start_inputs.map((input) => (
            <li key={input.name}>
              <span className="font-mono text-zinc-300">{input.name}</span>
              {input.label !== input.name && <span className="ml-2">{input.label}</span>}
              <span className="ml-2">{input.type}</span>
              {input.required && <span className="ml-2 text-indigo-300">required</span>}
            </li>
          ))}
        </ReadinessList>
      )}

      {response.requirements.length > 0 && (
        <ReadinessList title="Requirements">
          {response.requirements.map((requirement, index) => (
            <li key={`${requirement.kind}-${requirement.name}-${index}`}>
              <span className="text-zinc-300">{requirement.kind}</span>
              <span className="ml-2 font-mono">{requirement.name}</span>
              {requirement.required && <span className="ml-2 text-amber-300">required</span>}
            </li>
          ))}
        </ReadinessList>
      )}

      {response.warnings.length > 0 && (
        <div className="space-y-1">
          {response.warnings.map((warning, index) => (
            <div key={index} className="flex items-start gap-1.5 text-xs text-amber-200">
              <IconAlertTriangle size={13} className="mt-0.5 shrink-0 text-amber-400" />
              <span>{warning}</span>
            </div>
          ))}
        </div>
      )}

      <div className="font-mono text-[10px] text-zinc-600">
        schema {response.schema_version} · request {response.request_id}
      </div>
    </div>
  );
}

function ReadinessList({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section>
      <div className="mb-1.5 text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
        {title}
      </div>
      <ul className="space-y-1 text-xs text-zinc-500">{children}</ul>
    </section>
  );
}
