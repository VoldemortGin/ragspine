import { describe, expect, it } from 'vitest';

import {
  initializeRunInputs,
  parseRunInputsJson,
  serializeRunInputs,
  startInputKind,
  validateRunInputs,
} from '../../src/pages/workflows/model/runInputs';
import type { StartVariable } from '../../src/workflow/types';

const variables: StartVariable[] = [
  { variable: 'title', type: 'text-input', required: true },
  { variable: 'body', type: 'paragraph' },
  { variable: 'count', type: 'number', required: true },
  { variable: 'route', type: 'select', options: ['standard', 'urgent'], required: true },
  { variable: 'notify', type: 'checkbox', required: true },
];

describe('run input model', () => {
  it('recognizes imported select and boolean aliases', () => {
    expect(startInputKind({ variable: 'a', type: 'select' })).toBe('select');
    expect(startInputKind({ variable: 'a', type: 'boolean' })).toBe('boolean');
    expect(startInputKind({ variable: 'a', type: 'checkbox' })).toBe('boolean');
    expect(startInputKind({ variable: 'a', type: 'paragraph' })).toBe('paragraph');
  });

  it('preserves stored advanced keys and initializes checkbox controls', () => {
    expect(initializeRunInputs(variables, { advanced: 1 })).toEqual({
      advanced: 1,
      notify: false,
    });
  });

  it('normalizes declared values while preserving advanced extra keys', () => {
    const result = validateRunInputs(variables, {
      title: 'Invoice',
      body: '',
      count: '3.5',
      route: 'urgent',
      notify: false,
      advanced_flag: { retries: 2 },
    });

    expect(result).toEqual({
      inputs: {
        title: 'Invoice',
        count: 3.5,
        route: 'urgent',
        notify: false,
        advanced_flag: { retries: 2 },
      },
      errors: {},
    });
  });

  it('reports required, numeric, select, and boolean errors without throwing', () => {
    const result = validateRunInputs(variables, {
      title: ' ',
      count: 'NaN',
      route: 'other',
      notify: 'yes',
    });

    expect(result.errors).toEqual({
      title: 'This field is required.',
      count: 'Must be a valid number.',
      route: 'Choose one of the available options.',
      notify: 'Must be true or false.',
    });
  });

  it('round-trips the complete inputs object through advanced JSON', () => {
    const inputs = { title: 'A', notify: true, nested: { ids: [1, 2] } };
    const parsed = parseRunInputsJson(serializeRunInputs(inputs));

    expect(parsed).toEqual({ inputs, error: null });
    expect(parseRunInputsJson('[]').error).toBe('Inputs JSON must be an object.');
    expect(parseRunInputsJson('{')).toEqual({
      inputs: null,
      error: 'Enter a valid JSON object.',
    });
  });
});
