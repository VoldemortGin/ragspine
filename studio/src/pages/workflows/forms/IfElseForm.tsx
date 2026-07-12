/** Property form for `if-else` nodes: cases with condition lists. */

import { Select, TextInput } from '../../../components';
import { COMPARISON_OPERATORS } from '../../../workflow/types';
import type { IfElseCase, IfElseCondition, IfElseNodeData } from '../../../workflow/types';
import { AddButton, FormHint, ListRow, ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

const NO_VALUE_OPERATORS = ['empty', 'not empty'];

export function IfElseForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as IfElseNodeData;
  const cases = Array.isArray(typed.cases) ? typed.cases : [];

  const commitCases = (next: IfElseCase[]) => onChange({ ...data, cases: next });
  const patchCase = (index: number, changes: Partial<IfElseCase>) =>
    commitCases(cases.map((row, i) => (i === index ? { ...row, ...changes } : row)));

  const addCase = () => {
    const ids = new Set(cases.map((c) => (typeof c.case_id === 'string' ? c.case_id : '')));
    let n = 1;
    while (ids.has(`case_${n}`)) n += 1;
    commitCases([...cases, { case_id: `case_${n}`, logical_operator: 'and', conditions: [] }]);
  };

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        {cases.map((row, caseIndex) => {
          const conditions = Array.isArray(row.conditions) ? row.conditions : [];
          const patchCondition = (condIndex: number, changes: Partial<IfElseCondition>) =>
            patchCase(caseIndex, {
              conditions: conditions.map((c, i) => (i === condIndex ? { ...c, ...changes } : c)),
            });
          return (
            <ListRow
              key={caseIndex}
              onRemove={() => commitCases(cases.filter((_, i) => i !== caseIndex))}
              className="border-white/10"
            >
              <TextInput
                value={typeof row.case_id === 'string' ? row.case_id : ''}
                placeholder="case_id"
                onChange={(e) => patchCase(caseIndex, { case_id: e.target.value })}
                className="h-7.5 font-mono !text-xs"
              />
              <FormHint>The case id becomes the branch handle on the canvas.</FormHint>
              <div className="w-24">
                <Select
                  value={row.logical_operator === 'or' ? 'or' : 'and'}
                  onChange={(e) =>
                    patchCase(caseIndex, {
                      logical_operator: e.target.value as IfElseCase['logical_operator'],
                    })
                  }
                  className="h-7.5 !text-xs"
                >
                  <option value="and">and</option>
                  <option value="or">or</option>
                </Select>
              </div>
              {conditions.map((cond, condIndex) => {
                const operator =
                  typeof cond.comparison_operator === 'string' && cond.comparison_operator !== ''
                    ? cond.comparison_operator
                    : '==';
                const operatorOptions = (COMPARISON_OPERATORS as readonly string[]).includes(
                  operator,
                )
                  ? (COMPARISON_OPERATORS as readonly string[])
                  : [...COMPARISON_OPERATORS, operator];
                return (
                  <ListRow
                    key={condIndex}
                    onRemove={() =>
                      patchCase(caseIndex, {
                        conditions: conditions.filter((_, i) => i !== condIndex),
                      })
                    }
                  >
                    <ValueSelectorInput
                      value={cond.variable_selector}
                      listId={listId}
                      available={available}
                      onChange={(selector) =>
                        patchCondition(condIndex, { variable_selector: selector })
                      }
                    />
                    <div className="flex items-center gap-1.5">
                      <div className="w-32 shrink-0">
                        <Select
                          value={operator}
                          onChange={(e) =>
                            patchCondition(condIndex, { comparison_operator: e.target.value })
                          }
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
                          onChange={(e) => patchCondition(condIndex, { value: e.target.value })}
                          className="h-7.5 flex-1 !text-xs"
                        />
                      )}
                    </div>
                  </ListRow>
                );
              })}
              <AddButton
                onClick={() =>
                  patchCase(caseIndex, {
                    conditions: [
                      ...conditions,
                      { variable_selector: [], comparison_operator: '==', value: '' },
                    ],
                  })
                }
              >
                Add condition
              </AddButton>
            </ListRow>
          );
        })}
        <AddButton onClick={addCase}>Add case</AddButton>
      </div>
      <FormHint>
        The ELSE branch is implicit: unmatched flow leaves through the &quot;false&quot; handle.
      </FormHint>
    </div>
  );
}
