/** Property form for `iteration` nodes: iterator/output selectors, parallelism. */

import { Checkbox, Field, TextInput } from '../../../components';
import type { IterationNodeData } from '../../../workflow/types';
import { FormHint, ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

export function IterationForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as IterationNodeData;
  const isParallel = typed.is_parallel === true;
  const parallelNums =
    typeof typed.parallel_nums === 'number' && Number.isFinite(typed.parallel_nums)
      ? typed.parallel_nums
      : 1;

  const commitParallelNums = (raw: string) => {
    if (raw.trim() === '') {
      const next = { ...data };
      delete next['parallel_nums'];
      onChange(next);
      return;
    }
    const n = Number(raw);
    if (!Number.isFinite(n)) return;
    onChange({ ...data, parallel_nums: n });
  };

  return (
    <div className="space-y-3">
      <Field label="Iterator" hint="List to iterate over.">
        <ValueSelectorInput
          value={typed.iterator_selector}
          listId={listId}
          available={available}
          onChange={(selector) => onChange({ ...data, iterator_selector: selector })}
        />
      </Field>
      <Field label="Output" hint="Child-node output collected per iteration.">
        {/* References a CHILD node inside the container, so the upstream
            suggestions do not apply here; free-form input. */}
        <ValueSelectorInput
          value={typed.output_selector}
          onChange={(selector) => onChange({ ...data, output_selector: selector })}
        />
      </Field>
      <Checkbox
        checked={isParallel}
        onChange={(e) => onChange({ ...data, is_parallel: e.target.checked })}
        label="Run iterations in parallel"
        className="!text-xs"
      />
      <Field label="Parallel workers">
        <TextInput
          type="number"
          value={String(parallelNums)}
          disabled={!isParallel}
          onChange={(e) => commitParallelNums(e.target.value)}
          className="h-7.5 !text-xs"
        />
      </Field>
      <FormHint>
        Child nodes live inside the container on the canvas; drop nodes into it to build the loop
        body.
      </FormHint>
    </div>
  );
}
