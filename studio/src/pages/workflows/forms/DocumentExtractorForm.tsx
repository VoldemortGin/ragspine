/** Property form for `document-extractor` nodes: file input selector. */

import { Checkbox, Field } from '../../../components';
import type { DocumentExtractorNodeData } from '../../../workflow/types';
import { FormHint, ValueSelectorInput } from '../shared';
import type { NodeFormProps } from './types';

export function DocumentExtractorForm({ data, listId, available, onChange }: NodeFormProps) {
  const typed = data as DocumentExtractorNodeData;
  return (
    <div className="space-y-3">
      <Field label="Input" hint="A file (or array of files) variable to extract text from.">
        <ValueSelectorInput
          value={typed.variable_selector}
          listId={listId}
          available={available}
          onChange={(selector) => onChange({ ...data, variable_selector: selector })}
        />
      </Field>
      <Checkbox
        checked={typed.is_array_file === true}
        onChange={(e) => onChange({ ...data, is_array_file: e.target.checked })}
        label="Input is an array of files"
        className="!text-xs"
      />
      <FormHint>Exposes the extracted plain text as the node&apos;s `text` output.</FormHint>
    </div>
  );
}
