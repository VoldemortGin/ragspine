/** Property form for `tool` nodes: tool name + parameter dict. */

import { Field, Select, TextInput } from '../../../components';
import type { ToolNodeData } from '../../../workflow/types';
import { AddButton, ListRow, ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

const PARAMETER_KINDS = ['variable', 'mixed', 'constant'];

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function constantText(value: unknown): string {
  if (value === undefined || value === null) return '';
  return typeof value === 'string' ? value : String(value);
}

export function ToolForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as ToolNodeData;
  const parameters: Record<string, unknown> = asRecord(typed.tool_parameters);
  const entries = Object.entries(parameters);

  const commitParameters = (next: Record<string, unknown>) =>
    onChange({ ...data, tool_parameters: next });

  const renameParameter = (index: number, nextKey: string) => {
    const next: Record<string, unknown> = {};
    entries.forEach(([key, value], i) => {
      next[i === index ? nextKey : key] = value;
    });
    commitParameters(next);
  };
  const patchParameter = (key: string, changes: Record<string, unknown>) =>
    commitParameters({ ...parameters, [key]: { ...asRecord(parameters[key]), ...changes } });
  const removeParameter = (key: string) => {
    const next = { ...parameters };
    delete next[key];
    commitParameters(next);
  };
  const addParameter = () => {
    let n = 1;
    while (Object.prototype.hasOwnProperty.call(parameters, `param_${n}`)) n += 1;
    commitParameters({ ...parameters, [`param_${n}`]: { type: 'constant', value: '' } });
  };

  return (
    <div className="space-y-3">
      <Field label="Tool">
        <TextInput
          value={typeof typed.tool_name === 'string' ? typed.tool_name : ''}
          placeholder="tool_name"
          onChange={(e) => onChange({ ...data, tool_name: e.target.value })}
          className="h-7.5 font-mono !text-xs"
        />
      </Field>

      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Parameters</div>
        <div className="space-y-1.5">
          {entries.map(([key, raw], index) => {
            const entry = asRecord(raw);
            const kindRaw = entry['type'];
            const kind =
              typeof kindRaw === 'string' && kindRaw !== '' ? kindRaw : 'constant';
            const kindOptions = PARAMETER_KINDS.includes(kind)
              ? PARAMETER_KINDS
              : [...PARAMETER_KINDS, kind];
            return (
              <ListRow key={index} onRemove={() => removeParameter(key)}>
                <div className="flex items-center gap-1.5">
                  <TextInput
                    value={key}
                    placeholder="parameter"
                    onChange={(e) => renameParameter(index, e.target.value)}
                    className="h-7.5 flex-1 font-mono !text-xs"
                  />
                  <div className="w-28 shrink-0">
                    <Select
                      value={kind}
                      onChange={(e) => patchParameter(key, { type: e.target.value })}
                      className="h-7.5 !text-xs"
                    >
                      {kindOptions.map((k) => (
                        <option key={k} value={k}>
                          {k}
                        </option>
                      ))}
                    </Select>
                  </div>
                </div>
                {kind === 'variable' ? (
                  <ValueSelectorInput
                    value={entry['value']}
                    listId={listId}
                    available={available}
                    onChange={(selector) => patchParameter(key, { value: selector })}
                  />
                ) : (
                  <TextInput
                    value={constantText(entry['value'])}
                    placeholder="value"
                    onChange={(e) => patchParameter(key, { value: e.target.value })}
                    className="h-7.5 !text-xs"
                  />
                )}
              </ListRow>
            );
          })}
          <AddButton onClick={addParameter}>Add parameter</AddButton>
        </div>
      </div>
    </div>
  );
}
