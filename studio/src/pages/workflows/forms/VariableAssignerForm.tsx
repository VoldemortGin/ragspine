/** Property form for `assigner` (variable assigner v2) nodes: write operations. */

import { Select, TextInput } from '../../../components';
import type { AssignerItem, AssignerNodeData } from '../../../workflow/types';
import { AddButton, FormHint, ListRow, ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

const OPERATIONS = [
  'over-write',
  'set',
  'append',
  'extend',
  'clear',
  '+=',
  '-=',
  '*=',
  '/=',
  'remove-first',
  'remove-last',
];
const NO_VALUE_OPERATIONS = ['clear', 'remove-first', 'remove-last'];

export function VariableAssignerForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as AssignerNodeData;
  const items = Array.isArray(typed.items) ? typed.items : [];

  const commit = (next: AssignerItem[]) => onChange({ ...data, version: '2', items: next });
  const patch = (index: number, changes: Partial<AssignerItem>) =>
    commit(items.map((row, i) => (i === index ? { ...row, ...changes } : row)));

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        {items.map((row, index) => {
          const operation = typeof row.operation === 'string' ? row.operation : 'over-write';
          const operationOptions = OPERATIONS.includes(operation)
            ? OPERATIONS
            : [...OPERATIONS, operation];
          return (
            <ListRow key={index} onRemove={() => commit(items.filter((_, i) => i !== index))}>
              <ValueSelectorInput
                value={row.variable_selector}
                listId={listId}
                available={available}
                onChange={(selector) => patch(index, { variable_selector: selector })}
              />
              <div className="flex items-center gap-1.5">
                <div className="w-32 shrink-0">
                  <Select
                    value={operation}
                    onChange={(e) =>
                      patch(index, { operation: e.target.value as AssignerItem['operation'] })
                    }
                    className="h-7.5 !text-xs"
                  >
                    {operationOptions.map((op) => (
                      <option key={op} value={op}>
                        {op}
                      </option>
                    ))}
                  </Select>
                </div>
                {!NO_VALUE_OPERATIONS.includes(operation) && (
                  <TextInput
                    value={typeof row.value === 'string' ? row.value : ''}
                    placeholder="value or {{#node.var#}}"
                    onChange={(e) => patch(index, { value: e.target.value })}
                    className="h-7.5 flex-1 font-mono !text-xs"
                  />
                )}
              </div>
            </ListRow>
          );
        })}
        <AddButton
          onClick={() => commit([...items, { variable_selector: [], operation: 'over-write', value: '' }])}
        >
          Add assignment
        </AddButton>
      </div>
      <FormHint>
        Writes into conversation/loop variables. Requires a session; single-shot runs report it as
        unsupported.
      </FormHint>
    </div>
  );
}
