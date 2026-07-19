/** Pure presentation model for workflow readiness API results. */

import type {
  WorkflowReadinessCheckStatus,
  WorkflowReadinessResponse,
} from '../../../api/types';
import type { BadgeVariant } from '../../../components';

export interface WorkflowReadinessCheckRow {
  key: 'format' | 'compile' | 'runnable';
  label: 'Format' | 'Compile' | 'Runnable';
  status: WorkflowReadinessCheckStatus;
  code: string | null;
  badge: BadgeVariant;
}

const CHECKS = [
  ['format', 'Format'],
  ['compile', 'Compile'],
  ['runnable', 'Runnable'],
] as const;

export function readinessStatusBadge(
  status: WorkflowReadinessResponse['status'],
): { label: 'Ready' | 'Blocked'; badge: BadgeVariant } {
  return status === 'ready'
    ? { label: 'Ready', badge: 'success' }
    : { label: 'Blocked', badge: 'danger' };
}

export function readinessCheckRows(
  response: WorkflowReadinessResponse,
): WorkflowReadinessCheckRow[] {
  return CHECKS.map(([key, label]) => {
    const check = response.checks[key];
    return {
      key,
      label,
      status: check.status,
      code: check.code ?? null,
      badge:
        check.status === 'passed'
          ? 'success'
          : check.status === 'blocked'
            ? 'danger'
            : 'neutral',
    };
  });
}
