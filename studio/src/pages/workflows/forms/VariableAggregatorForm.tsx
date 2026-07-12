/** Property form for `variable-aggregator` nodes: merge first-non-null variable. */

import { Field, Select } from '../../../components';
import type { ValueSelector, VariableAggregatorNodeData } from '../../../workflow/types';
import { AddButton, FormHint, ListRow, ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

const OUTPUT_TYPES = ['any', 'string', 'number', 'object', 'array[string]', 'array[object]'];

export function VariableAggregatorForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as VariableAggregatorNodeData;
  const variables = Array.isArray(typed.variables) ? typed.variables : [];
  const outputType = typeof typed.output_type === 'string' ? typed.output_type : 'any';
  const outputTypeOptions = OUTPUT_TYPES.includes(outputType)
    ? OUTPUT_TYPES
    : [...OUTPUT_TYPES, outputType];

  const commit = (next: ValueSelector[]) => onChange({ ...data, variables: next });

  return (
    <div className="space-y-3">
      <Field label="Output type">
        <Select
          value={outputType}
          onChange={(e) => onChange({ ...data, output_type: e.target.value })}
          className="h-7.5 !text-xs"
        >
          {outputTypeOptions.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </Select>
      </Field>
      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Variables</div>
        <div className="space-y-1.5">
          {variables.map((selector, index) => (
            <ListRow key={index} onRemove={() => commit(variables.filter((_, i) => i !== index))}>
              <ValueSelectorInput
                value={selector}
                listId={listId}
                available={available}
                onChange={(next) => commit(variables.map((v, i) => (i === index ? next : v)))}
              />
            </ListRow>
          ))}
          <AddButton onClick={() => commit([...variables, []])}>Add variable</AddButton>
        </div>
      </div>
      <FormHint>Outputs the first non-empty variable in order — used to merge branches.</FormHint>
    </div>
  );
}
