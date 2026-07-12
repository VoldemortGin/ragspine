/** Property form for `start` nodes: entry variables. */

import { Checkbox, Select, TextInput } from '../../../components';
import type { StartNodeData, StartVariable } from '../../../workflow/types';
import { AddButton, ListRow } from '../shared';
import type { NodeFormProps } from './types';

const VARIABLE_TYPES = ['text-input', 'paragraph', 'number'];

export function StartForm({ data, onChange }: NodeFormProps) {
  const typed = data as StartNodeData;
  const variables = Array.isArray(typed.variables) ? typed.variables : [];

  const commit = (next: StartVariable[]) => onChange({ ...data, variables: next });
  const patch = (index: number, changes: Partial<StartVariable>) =>
    commit(variables.map((row, i) => (i === index ? { ...row, ...changes } : row)));

  return (
    <div className="space-y-3">
      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Input variables</div>
        <div className="space-y-1.5">
          {variables.map((row, index) => {
            const type = typeof row.type === 'string' && row.type !== '' ? row.type : 'text-input';
            const options = VARIABLE_TYPES.includes(type) ? VARIABLE_TYPES : [...VARIABLE_TYPES, type];
            return (
              <ListRow key={index} onRemove={() => commit(variables.filter((_, i) => i !== index))}>
                <div className="flex items-center gap-1.5">
                  <TextInput
                    value={typeof row.variable === 'string' ? row.variable : ''}
                    placeholder="variable"
                    onChange={(e) => patch(index, { variable: e.target.value })}
                    className="h-7.5 flex-1 font-mono !text-xs"
                  />
                  <div className="w-28 shrink-0">
                    <Select
                      value={type}
                      onChange={(e) => patch(index, { type: e.target.value })}
                      className="h-7.5 !text-xs"
                    >
                      {options.map((t) => (
                        <option key={t} value={t}>
                          {t}
                        </option>
                      ))}
                    </Select>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <TextInput
                    value={typeof row.label === 'string' ? row.label : ''}
                    placeholder="Label"
                    onChange={(e) => patch(index, { label: e.target.value })}
                    className="h-7.5 flex-1 !text-xs"
                  />
                  <Checkbox
                    checked={row.required === true}
                    onChange={(e) => patch(index, { required: e.target.checked })}
                    label="required"
                    className="shrink-0 !text-xs"
                  />
                </div>
              </ListRow>
            );
          })}
          <AddButton
            onClick={() =>
              commit([
                ...variables,
                { variable: '', label: '', type: 'text-input', required: false },
              ])
            }
          >
            Add variable
          </AddButton>
        </div>
      </div>
    </div>
  );
}
