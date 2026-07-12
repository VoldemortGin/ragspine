/** Property form for `end` nodes: output variable mappings. */

import { TextInput } from '../../../components';
import type { EndNodeData, EndOutput } from '../../../workflow/types';
import { AddButton, ListRow, ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

export function EndForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as EndNodeData;
  const outputs = Array.isArray(typed.outputs) ? typed.outputs : [];

  const commit = (next: EndOutput[]) => onChange({ ...data, outputs: next });
  const patch = (index: number, changes: Partial<EndOutput>) =>
    commit(outputs.map((row, i) => (i === index ? { ...row, ...changes } : row)));

  return (
    <div className="space-y-3">
      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Outputs</div>
        <div className="space-y-1.5">
          {outputs.map((row, index) => (
            <ListRow key={index} onRemove={() => commit(outputs.filter((_, i) => i !== index))}>
              <TextInput
                value={typeof row.variable === 'string' ? row.variable : ''}
                placeholder="variable"
                onChange={(e) => patch(index, { variable: e.target.value })}
                className="h-7.5 font-mono !text-xs"
              />
              <ValueSelectorInput
                value={row.value_selector}
                listId={listId}
                available={available}
                onChange={(selector) => patch(index, { value_selector: selector })}
              />
            </ListRow>
          ))}
          <AddButton onClick={() => commit([...outputs, { variable: '', value_selector: [] }])}>
            Add output
          </AddButton>
        </div>
      </div>
    </div>
  );
}
