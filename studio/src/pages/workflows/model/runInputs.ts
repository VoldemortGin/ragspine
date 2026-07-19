/** Pure normalization and validation for Start-node run inputs. */

import type { StartVariable } from '../../../workflow/types';

export type StartInputKind = 'text' | 'paragraph' | 'number' | 'select' | 'boolean';

export interface RunInputValidation {
  inputs: Record<string, unknown>;
  errors: Record<string, string>;
}

export interface ParsedRunInputs {
  inputs: Record<string, unknown> | null;
  error: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function startInputKind(variable: StartVariable): StartInputKind {
  const type = typeof variable.type === 'string' ? variable.type.toLocaleLowerCase() : '';
  if (type === 'paragraph') return 'paragraph';
  if (type === 'number') return 'number';
  if (type === 'select') return 'select';
  if (type === 'boolean' || type === 'checkbox') return 'boolean';
  return 'text';
}

export function startInputOptions(variable: StartVariable): string[] {
  return Array.isArray(variable.options)
    ? variable.options.filter(
        (option): option is string => typeof option === 'string' && option !== '',
      )
    : [];
}

export function serializeRunInputs(inputs: Record<string, unknown>): string {
  try {
    return JSON.stringify(inputs, null, 2);
  } catch {
    return '{}';
  }
}

export function initializeRunInputs(
  variables: readonly StartVariable[],
  stored: Readonly<Record<string, unknown>>,
): Record<string, unknown> {
  const inputs: Record<string, unknown> = { ...stored };
  for (const variable of variables) {
    if (startInputKind(variable) === 'boolean' && inputs[variable.variable] === undefined) {
      inputs[variable.variable] = false;
    }
  }
  return inputs;
}

export function parseRunInputsJson(text: string): ParsedRunInputs {
  try {
    const parsed: unknown = JSON.parse(text);
    return isRecord(parsed)
      ? { inputs: parsed, error: null }
      : { inputs: null, error: 'Inputs JSON must be an object.' };
  } catch {
    return { inputs: null, error: 'Enter a valid JSON object.' };
  }
}

export function validateRunInputs(
  variables: readonly StartVariable[],
  draft: Readonly<Record<string, unknown>>,
): RunInputValidation {
  const inputs: Record<string, unknown> = { ...draft };
  const errors: Record<string, string> = {};

  for (const variable of variables) {
    const key = variable.variable;
    const value = draft[key];
    const kind = startInputKind(variable);
    if (value === undefined || value === null || (typeof value === 'string' && value.trim() === '')) {
      delete inputs[key];
      if (variable.required === true) errors[key] = 'This field is required.';
      continue;
    }

    if (kind === 'number') {
      const number = typeof value === 'number' ? value : Number(String(value).trim());
      if (!Number.isFinite(number)) errors[key] = 'Must be a valid number.';
      else inputs[key] = number;
      continue;
    }
    if (kind === 'boolean') {
      if (typeof value === 'boolean') inputs[key] = value;
      else if (value === 'true' || value === 'false') inputs[key] = value === 'true';
      else errors[key] = 'Must be true or false.';
      continue;
    }

    const text = typeof value === 'string' ? value : String(value);
    if (kind === 'select') {
      const options = startInputOptions(variable);
      if (options.length > 0 && !options.includes(text)) {
        errors[key] = 'Choose one of the available options.';
        continue;
      }
    }
    inputs[key] = text;
  }

  return { inputs, errors };
}
