/** Property form for `parameter-extractor` nodes: query, instruction, parameters. */

import { Checkbox, Field, Select, TextArea, TextInput } from '../../../components';
import type { ExtractorParameter, ParameterExtractorNodeData } from '../../../workflow/types';
import { AddButton, ListRow, ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

const PARAMETER_TYPES = ['string', 'number', 'bool', 'array[string]'];

export function ParameterExtractorForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as ParameterExtractorNodeData;
  const model =
    typeof typed.model === 'object' && typed.model !== null && !Array.isArray(typed.model)
      ? typed.model
      : {};
  const parameters = Array.isArray(typed.parameters) ? typed.parameters : [];

  const commit = (next: ExtractorParameter[]) => onChange({ ...data, parameters: next });
  const patch = (index: number, changes: Partial<ExtractorParameter>) =>
    commit(parameters.map((row, i) => (i === index ? { ...row, ...changes } : row)));

  return (
    <div className="space-y-3">
      <Field label="Query">
        <ValueSelectorInput
          value={typed.query}
          listId={listId}
          available={available}
          onChange={(selector) => onChange({ ...data, query: selector })}
        />
      </Field>
      <Field label="Instruction">
        <TextArea
          value={typeof typed.instruction === 'string' ? typed.instruction : ''}
          rows={4}
          placeholder="Describe what to extract"
          onChange={(e) => onChange({ ...data, instruction: e.target.value })}
          className="!text-xs"
        />
      </Field>
      <Field label="Model">
        <TextInput
          value={typeof model.name === 'string' ? model.name : ''}
          placeholder="gpt-4o"
          onChange={(e) => onChange({ ...data, model: { ...model, name: e.target.value } })}
          className="h-7.5 font-mono !text-xs"
        />
      </Field>

      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Parameters</div>
        <div className="space-y-1.5">
          {parameters.map((row, index) => {
            const type = typeof row.type === 'string' && row.type !== '' ? row.type : 'string';
            const typeOptions = PARAMETER_TYPES.includes(type)
              ? PARAMETER_TYPES
              : [...PARAMETER_TYPES, type];
            return (
              <ListRow key={index} onRemove={() => commit(parameters.filter((_, i) => i !== index))}>
                <div className="flex items-center gap-1.5">
                  <TextInput
                    value={typeof row.name === 'string' ? row.name : ''}
                    placeholder="name"
                    onChange={(e) => patch(index, { name: e.target.value })}
                    className="h-7.5 flex-1 font-mono !text-xs"
                  />
                  <div className="w-28 shrink-0">
                    <Select
                      value={type}
                      onChange={(e) => patch(index, { type: e.target.value })}
                      className="h-7.5 !text-xs"
                    >
                      {typeOptions.map((t) => (
                        <option key={t} value={t}>
                          {t}
                        </option>
                      ))}
                    </Select>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <TextInput
                    value={typeof row.description === 'string' ? row.description : ''}
                    placeholder="Description"
                    onChange={(e) => patch(index, { description: e.target.value })}
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
              commit([...parameters, { name: '', type: 'string', description: '', required: false }])
            }
          >
            Add parameter
          </AddButton>
        </div>
      </div>
    </div>
  );
}
