/** Property form for `answer` nodes: the answer template. */

import { Field } from '../../../components';
import type { AnswerNodeData } from '../../../workflow/types';
import { VariableTextArea } from '../VariableTextArea';
import { FormHint } from '../shared';
import type { NodeFormProps } from './types';

export function AnswerForm({ data, available, onChange }: NodeFormProps) {
  const typed = data as AnswerNodeData;
  return (
    <div className="space-y-3">
      <Field label="Answer">
        <VariableTextArea
          value={typeof typed.answer === 'string' ? typed.answer : ''}
          rows={6}
          placeholder="Answer template"
          available={available}
          onChange={(answer) => onChange({ ...data, answer })}
          className="font-mono !text-xs"
        />
      </Field>
      <FormHint>
        Interpolate upstream outputs with{' '}
        <span className="font-mono text-zinc-400">{'{{#nodeId.field#}}'}</span>, e.g.{' '}
        <span className="font-mono text-zinc-400">{'{{#llm_1.text#}}'}</span>. Type{' '}
        <span className="font-mono text-zinc-400">{'{{#'}</span> or{' '}
        <span className="font-mono text-zinc-400">/</span> to pick a variable.
      </FormHint>
    </div>
  );
}
