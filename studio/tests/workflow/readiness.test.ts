import { describe, expect, it } from 'vitest';

import type { WorkflowReadinessResponse } from '../../src/api/types';
import {
  readinessCheckRows,
  readinessStatusBadge,
} from '../../src/pages/workflows/model/readiness';

const response: WorkflowReadinessResponse = {
  request_id: 'req-1',
  schema_version: 1,
  status: 'blocked',
  checks: {
    format: { status: 'passed' },
    compile: { status: 'passed' },
    runnable: { status: 'blocked', code: 'dify.unsafe' },
  },
  start_inputs: [],
  warnings: ['unsupported tool'],
  requirements: [],
};

describe('workflow readiness presentation', () => {
  it('maps stable check order, codes, and severity badges', () => {
    expect(readinessCheckRows(response)).toEqual([
      { key: 'format', label: 'Format', status: 'passed', code: null, badge: 'success' },
      { key: 'compile', label: 'Compile', status: 'passed', code: null, badge: 'success' },
      {
        key: 'runnable',
        label: 'Runnable',
        status: 'blocked',
        code: 'dify.unsafe',
        badge: 'danger',
      },
    ]);
  });

  it('maps top-level ready and blocked states', () => {
    expect(readinessStatusBadge('ready')).toEqual({ label: 'Ready', badge: 'success' });
    expect(readinessStatusBadge('blocked')).toEqual({ label: 'Blocked', badge: 'danger' });
  });
});
