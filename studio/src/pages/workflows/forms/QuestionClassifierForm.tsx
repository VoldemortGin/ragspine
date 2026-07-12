/** Property form for `question-classifier` nodes: classification classes. */

import { TextInput } from '../../../components';
import type { ClassifierClass, QuestionClassifierNodeData } from '../../../workflow/types';
import { AddButton, FormHint, ListRow } from '../shared';
import type { NodeFormProps } from './types';

export function QuestionClassifierForm({ data, onChange }: NodeFormProps) {
  const typed = data as QuestionClassifierNodeData;
  const classes = Array.isArray(typed.classes) ? typed.classes : [];

  const commit = (next: ClassifierClass[]) => onChange({ ...data, classes: next });
  const patch = (index: number, changes: Partial<ClassifierClass>) =>
    commit(classes.map((row, i) => (i === index ? { ...row, ...changes } : row)));

  const addClass = () => {
    let max = 0;
    for (const row of classes) {
      const n = Number(row.id);
      if (Number.isInteger(n) && n > max) max = n;
    }
    commit([...classes, { id: String(max + 1), name: '' }]);
  };

  return (
    <div className="space-y-3">
      <div>
        <div className="mb-1.5 text-xs font-medium text-zinc-400">Classes</div>
        <div className="space-y-1.5">
          {classes.map((row, index) => (
            <ListRow key={index} onRemove={() => commit(classes.filter((_, i) => i !== index))}>
              <div className="flex items-center gap-1.5">
                <div className="w-14 shrink-0">
                  <TextInput
                    value={typeof row.id === 'string' ? row.id : String(row.id ?? '')}
                    placeholder="id"
                    onChange={(e) => patch(index, { id: e.target.value })}
                    className="h-7.5 font-mono !text-xs"
                  />
                </div>
                <TextInput
                  value={typeof row.name === 'string' ? row.name : ''}
                  placeholder="Class name"
                  onChange={(e) => patch(index, { name: e.target.value })}
                  className="h-7.5 flex-1 !text-xs"
                />
              </div>
            </ListRow>
          ))}
          <AddButton onClick={addClass}>Add class</AddButton>
        </div>
      </div>
      <FormHint>Each class is a branch output handle on the canvas.</FormHint>
    </div>
  );
}
