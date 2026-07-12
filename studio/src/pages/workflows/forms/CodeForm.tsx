/** Property form for `code` nodes: language, code, input variables, outputs. */

import { Field, Select, TextArea, TextInput } from '../../../components';
import type { CodeNodeData, CodeVariable } from '../../../workflow/types';
import { AddButton, FormHint, ListRow, ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

function outputTypeLabel(value: unknown): string {
  if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
    const t = (value as Record<string, unknown>)['type'];
    if (typeof t === 'string') return `type: ${t}`;
  }
  return 'object';
}

export function CodeForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as CodeNodeData;

  const language =
    typeof typed.code_language === 'string' && typed.code_language !== ''
      ? typed.code_language
      : 'python3';
  const languageOptions = language === 'python3' ? ['python3'] : ['python3', language];

  /* ---------------------------- variables ----------------------------- */

  const variables = Array.isArray(typed.variables) ? typed.variables : [];
  const commitVariables = (next: CodeVariable[]) => onChange({ ...data, variables: next });
  const patchVariable = (index: number, changes: Partial<CodeVariable>) =>
    commitVariables(variables.map((row, i) => (i === index ? { ...row, ...changes } : row)));

  /* ----------------------------- outputs ------------------------------ */

  const outputs =
    typeof typed.outputs === 'object' && typed.outputs !== null && !Array.isArray(typed.outputs)
      ? typed.outputs
      : {};
  const outputEntries = Object.entries(outputs);

  const renameOutput = (index: number, nextKey: string) => {
    const next: Record<string, unknown> = {};
    outputEntries.forEach(([key, value], i) => {
      next[i === index ? nextKey : key] = value;
    });
    onChange({ ...data, outputs: next });
  };
  const removeOutput = (key: string) => {
    const next = { ...outputs };
    delete next[key];
    onChange({ ...data, outputs: next });
  };
  const addOutput = () => {
    let n = 1;
    while (Object.prototype.hasOwnProperty.call(outputs, `output_${n}`)) n += 1;
    onChange({ ...data, outputs: { ...outputs, [`output_${n}`]: {} } });
  };

  return (
    <div className="space-y-3">
      <Field label="Language">
        <Select
          value={language}
          onChange={(e) => onChange({ ...data, code_language: e.target.value })}
          className="h-7.5 !text-xs"
        >
          {languageOptions.map((lang) => (
            <option key={lang} value={lang}>
              {lang}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="Code">
        <TextArea
          value={typeof typed.code === 'string' ? typed.code : ''}
          rows={14}
          spellCheck={false}
          placeholder={'def main():\n    return {"result": ...}'}
          onChange={(e) => onChange({ ...data, code: e.target.value })}
          className="font-mono !text-xs"
        />
      </Field>

      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Input variables</div>
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

      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Outputs</div>
        <div className="space-y-1.5">
          {outputEntries.map(([key, value], index) => (
            <ListRow key={index} onRemove={() => removeOutput(key)}>
              <div className="flex items-center gap-1.5">
                <TextInput
                  value={key}
                  placeholder="output key"
                  onChange={(e) => renameOutput(index, e.target.value)}
                  className="h-7.5 flex-1 font-mono !text-xs"
                />
                <span className="shrink-0 font-mono text-[10px] text-zinc-600">
                  {outputTypeLabel(value)}
                </span>
              </div>
            </ListRow>
          ))}
          <AddButton onClick={addOutput}>Add output</AddButton>
        </div>
        <div className="mt-1.5">
          <FormHint>Output keys must match the dict returned by main().</FormHint>
        </div>
      </div>
    </div>
  );
}
