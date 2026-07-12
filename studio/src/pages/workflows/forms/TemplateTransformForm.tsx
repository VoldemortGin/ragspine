/** Property form for `template-transform` nodes: Jinja2 template + variables. */

import { Field, TextInput } from '../../../components';
import type { CodeVariable, TemplateTransformNodeData } from '../../../workflow/types';
import { VariableTextArea } from '../VariableTextArea';
import { AddButton, FormHint, ListRow, ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

export function TemplateTransformForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as TemplateTransformNodeData;
  const variables = Array.isArray(typed.variables) ? typed.variables : [];

  const commitVariables = (next: CodeVariable[]) => onChange({ ...data, variables: next });
  const patchVariable = (index: number, changes: Partial<CodeVariable>) =>
    commitVariables(variables.map((row, i) => (i === index ? { ...row, ...changes } : row)));

  return (
    <div className="space-y-3">
      <Field label="Template">
        <VariableTextArea
          value={typeof typed.template === 'string' ? typed.template : ''}
          rows={8}
          spellCheck={false}
          placeholder="{{ variable }}"
          available={available}
          onChange={(template) => onChange({ ...data, template })}
          className="font-mono !text-xs"
        />
      </Field>
      <FormHint>Jinja2 template. The variables below are available by name.</FormHint>

      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Variables</div>
        <div className="space-y-1.5">
          {variables.map((row, index) => (
            <ListRow
              key={index}
              onRemove={() => commitVariables(variables.filter((_, i) => i !== index))}
            >
              <TextInput
                value={typeof row.variable === 'string' ? row.variable : ''}
                placeholder="variable"
                onChange={(e) => patchVariable(index, { variable: e.target.value })}
                className="h-7.5 font-mono !text-xs"
              />
              <ValueSelectorInput
                value={row.value_selector}
                listId={listId}
                available={available}
                onChange={(selector) => patchVariable(index, { value_selector: selector })}
              />
            </ListRow>
          ))}
          <AddButton
            onClick={() => commitVariables([...variables, { variable: '', value_selector: [] }])}
          >
            Add variable
          </AddButton>
        </div>
      </div>
    </div>
  );
}
