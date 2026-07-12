/** Property form for `loop` (container) nodes: max count + break conditions. */

import { Field, Select, TextInput } from '../../../components';
import { COMPARISON_OPERATORS } from '../../../workflow/types';
import type { LoopBreakCondition, LoopNodeData } from '../../../workflow/types';
import { AddButton, FormHint, ListRow, ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

const NO_VALUE_OPERATORS = ['empty', 'not empty'];

export function LoopForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as LoopNodeData;
  const loopCount =
    typeof typed.loop_count === 'number' && Number.isFinite(typed.loop_count) ? typed.loop_count : 10;
  const logical = typed.logical_operator === 'or' ? 'or' : 'and';
  const conditions = Array.isArray(typed.break_conditions) ? typed.break_conditions : [];

  const commit = (next: LoopBreakCondition[]) => onChange({ ...data, break_conditions: next });
  const patch = (index: number, changes: Partial<LoopBreakCondition>) =>
    commit(conditions.map((row, i) => (i === index ? { ...row, ...changes } : row)));

  const commitLoopCount = (raw: string) => {
    const n = Number(raw);
    if (!Number.isFinite(n)) return;
    onChange({ ...data, loop_count: n });
  };

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <Field label="Max iterations">
          <TextInput
            type="number"
            value={String(loopCount)}
            onChange={(e) => commitLoopCount(e.target.value)}
            className="h-7.5 !text-xs"
          />
        </Field>
        <Field label="Match">
          <Select
            value={logical}
            onChange={(e) =>
              onChange({ ...data, logical_operator: e.target.value as LoopNodeData['logical_operator'] })
            }
            className="h-7.5 !text-xs"
          >
            <option value="and">all (and)</option>
            <option value="or">any (or)</option>
          </Select>
        </Field>
      </div>

      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Break conditions</div>
        <div className="space-y-1.5">
          {conditions.map((cond, index) => {
            const operator =
              typeof cond.comparison_operator === 'string' && cond.comparison_operator !== ''
                ? cond.comparison_operator
                : '==';
            const operatorOptions = (COMPARISON_OPERATORS as readonly string[]).includes(operator)
              ? (COMPARISON_OPERATORS as readonly string[])
              : [...COMPARISON_OPERATORS, operator];
            return (
              <ListRow key={index} onRemove={() => commit(conditions.filter((_, i) => i !== index))}>
                <ValueSelectorInput
                  value={cond.variable_selector}
                  listId={listId}
                  available={available}
                  onChange={(selector) => patch(index, { variable_selector: selector })}
                />
                <div className="flex items-center gap-1.5">
                  <div className="w-32 shrink-0">
                    <Select
                      value={operator}
                      onChange={(e) => patch(index, { comparison_operator: e.target.value })}
                      className="h-7.5 !text-xs"
                    >
                      {operatorOptions.map((op) => (
                        <option key={op} value={op}>
                          {op}
                        </option>
                      ))}
                    </Select>
                  </div>
                  {!NO_VALUE_OPERATORS.includes(operator) && (
                    <TextInput
                      value={typeof cond.value === 'string' ? cond.value : ''}
                      placeholder="value"
                      onChange={(e) => patch(index, { value: e.target.value })}
                      className="h-7.5 flex-1 !text-xs"
                    />
                  )}
                </div>
              </ListRow>
            );
          })}
          <AddButton
            onClick={() =>
              commit([...conditions, { variable_selector: [], comparison_operator: '==', value: '' }])
            }
          >
            Add break condition
          </AddButton>
        </div>
      </div>

      <FormHint>
        The loop body lives inside the container on the canvas; drop nodes into it. Iteration stops at
        the max count or when the break conditions match.
      </FormHint>
    </div>
  );
}
